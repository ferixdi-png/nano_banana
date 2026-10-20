import asyncio
import logging
import os
import random
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
from telegram.request import HTTPXRequest
from telegram.error import BadRequest, TimedOut
from telegram.ext import (
    Application,
    CommandHandler,
    ContextTypes,
    MessageHandler,
    filters,
)

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
# The configured admin may generate in private chat; everyone else must use the group.
ALLOWED_USER_ID_RAW = os.getenv("ALLOWED_USER_ID", "").strip()
ALLOWED_USER_ID = int(ALLOWED_USER_ID_RAW) if ALLOWED_USER_ID_RAW else None
ALLOWED_CHAT_ID_RAW = os.getenv("ALLOWED_CHAT_ID", "").strip()
ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW) if ALLOWED_CHAT_ID_RAW else None

GENERATION_COOLDOWN_SECONDS = 25
# A request must address the bot by name before any generation trigger is considered.
BOT_NAME_RE = re.compile(r"(?<![\w])(?:ferixdi|фериксди)(?![\w])", re.IGNORECASE)
# Only explicit image-creation requests trigger generation.
# Conversational command wording is accepted; wrapper text is stripped before generation.
IMAGE_INTENT_RE = re.compile(
    r"\b(?:сгенерируй(?:-ка)?|сгенерировать|генерируй|генерировать|"
    r"нарисуй(?:-ка)?|нарисовать|изобрази|изобразить|"
    r"создай|создать|сделай(?:-ка)?|сделать|запили)\b",
    re.IGNORECASE,
)
# These keywords also indicate an image-generation prompt in the creators chat.
KEYWORD_TRIGGER_RE = re.compile(
    r"\b(?:prompt|realistic|face|style|промпт)\b",
    re.IGNORECASE,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
# HTTP client INFO logs include full Telegram API URLs; keep bot tokens out of logs.
logging.getLogger("httpx").setLevel(logging.WARNING)
log = logging.getLogger("ferixdi-image-bot")

_generation_in_progress = False
_last_generation_finished_at = 0.0
PENDING_IMAGE_TTL_SECONDS = 10 * 60
# Allows a user to send the photo first and the editing instruction as a separate message.
_pending_images: dict[tuple[int, int], tuple[Path, float]] = {}

# Rotating copy keeps the bot lively without roasting the person who asked.
START_ADMIN_LINES = [
    "О, босс-кожаный, личный вход открыт. Тебе — зелёный свет, пикселям — тяжёлая смена.",
    "Начальство прибыло. Мои нейроны поправили галстуки: можно генерировать прямо здесь или в группе.",
    "Для тебя, босс, отдельный вход. Пользуйся властью разумно: например, закажи кота в скафандре.",
]
START_PRIVATE_LINES = [
    "Залетай в чат креаторов: https://t.me/ai_neiro_chat\nГенерируй фото бесплатно. Просто напиши, что хочешь создать, и обязательно упомяни Ferixdi в запросе, чтобы бот запустил генерацию.",
]
START_GROUP_LINES = [
    "Так, кожаные, открываем художественный цех. Я — ваш пиксельный демон, но с лимитом терпения и без берета.",
    "Добро пожаловать в мастерскую, где даже самая безумная идея может стать картинкой.",
    "На связи ваш карманный художник. Руки виртуальные, фантазия без выходных.",
]
BUSY_LINES = [
    "Предыдущая генерация ещё выполняется. Подожди немного.",
]
COOLDOWN_LINES = [
    "Следующий запрос можно отправить через {seconds} сек.",
]
PROGRESS_LINES = [
    "Генерирую изображение…",
]
SUCCESS_LINES = [
    "Изображение готово.",
]
ERROR_LINES = [
    "Не удалось сгенерировать изображение. Попробуй ещё раз позже.",
]
DELIVERY_ERROR_LINES = [
    "Изображение создано, но не удалось отправить его в Telegram.",
]


class GenerationError(Exception):
    """A user-safe generation error."""


def generate_and_download(prompt: str, input_image_path: Path | None = None) -> Path:
    """Implemented by cloud_runner.py, which calls the Kie AI API."""
    raise GenerationError("Генератор не инициализирован. Перезапусти Worker, кожаный.")

def clean_generation_prompt(message_text: str) -> str:
    """Keep the visual request and remove bot addressing plus conversational wrappers."""
    prompt = BOT_NAME_RE.sub(" ", message_text)
    prompt = re.sub(r"\s+", " ", prompt).strip()

    # Remove an optional leading English prompt label.
    prompt = re.sub(r"^prompt\s*[:—–-]?\s*", "", prompt, count=1, flags=re.IGNORECASE)

    # Strip command words and polite/friendly filler from the start, iteratively.
    command_re = re.compile(
        r"^(?:сгенерируй(?:-ка)?|сгенерировать|генерируй|генерировать|"
        r"нарисуй(?:-ка)?|нарисовать|изобрази|изобразить|"
        r"создай|создать|сделай(?:-ка)?|сделать|запили)\b"
        r"[\s,.:;!—–-]*",
        re.IGNORECASE,
    )
    filler_re = re.compile(
        r"^(?:(?:по[- ]братски|и\s+мне|для\s+меня|пожалуйста|плиз|"
        r"братски|бро|брат|дружище|мне|можешь|можно|давай|ну|слушай|эй|ка|же)"
        r"\b[\s,.:;!—–-]*)+",
        re.IGNORECASE,
    )

    previous = None
    while prompt and prompt != previous:
        previous = prompt
        prompt = command_re.sub("", prompt, count=1)
        prompt = filler_re.sub("", prompt, count=1)
        prompt = re.sub(r"^[,.:;!—–-]+\s*", "", prompt)

    return re.sub(r"\s+", " ", prompt).strip(" \t\r\n,.:;!—–-")


def can_generate_here(update: Update) -> bool:
    """Everyone can generate in the configured group; admin can also use private chat."""
    chat = update.effective_chat
    user = update.effective_user
    if not chat:
        return False
    in_allowed_group = (
        ALLOWED_CHAT_ID is not None
        and chat.id == ALLOWED_CHAT_ID
        and chat.type in ("group", "supergroup")
    )
    is_admin_private = (
        ALLOWED_USER_ID is not None
        and user is not None
        and user.id == ALLOWED_USER_ID
        and chat.type == "private"
    )
    return bool(in_allowed_group or is_admin_private)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not message:
        return

    chat = update.effective_chat
    user = update.effective_user
    example = (
        "Пример: «Ferixdi, сгенерируй изображение: лысый енот в дорогом костюме "
        "на заседании совета директоров»."
    )
    settings = "Формат — 9:16, разрешение — 1K."
    if chat and chat.type == "private":
        if ALLOWED_USER_ID is not None and user and user.id == ALLOWED_USER_ID:
            await message.reply_text(
                f"{random.choice(START_ADMIN_LINES)}\n\n{example}\n{settings}"
            )
        else:
            await message.reply_text(random.choice(START_PRIVATE_LINES))
        return

    if not can_generate_here(update):
        return

    await message.reply_text(
        f"{random.choice(START_GROUP_LINES)}\n\n"
        "Обязательно обращайся ко мне по имени: «Ferixdi, сгенерируй изображение ...», "
        "«Фериксди, нарисуй ...» или «Ferixdi, prompt realistic face style ...».\n"
        f"{example}\n{settings}"
    )


async def handle_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global _generation_in_progress, _last_generation_finished_at

    message = update.effective_message
    if not message:
        return

    # Ignore every message that does not explicitly address Ferixdi.
    # This check must happen before private-chat replies or handling attachments,
    # so ordinary photos never trigger a response or get saved as pending uploads.
    prompt = (message.text or message.caption or "").strip()
    if not BOT_NAME_RE.search(prompt):
        return

    chat = update.effective_chat
    user = update.effective_user
    is_admin_private = (
        chat is not None
        and chat.type == "private"
        and ALLOWED_USER_ID is not None
        and user is not None
        and user.id == ALLOWED_USER_ID
    )
    if chat is not None and chat.type == "private" and not is_admin_private:
        await message.reply_text(random.choice(START_PRIVATE_LINES))
        return

    if not can_generate_here(update):
        return

    prompt = (message.text or message.caption or "").strip()
    has_photo = bool(message.photo)
    image_document = bool(
        message.document
        and (message.document.mime_type or "").lower().startswith("image/")
    )
    has_attached_image = has_photo or image_document
    user_id = update.effective_user.id if update.effective_user else 0
    pending_key = (message.chat_id, user_id)
    now = time.monotonic()

    # Remove stale pending uploads so old photos are never edited accidentally.
    for key, (saved_path, saved_at) in list(_pending_images.items()):
        if now - saved_at > PENDING_IMAGE_TTL_SECONDS:
            _pending_images.pop(key, None)
            try:
                saved_path.unlink(missing_ok=True)
            except Exception:
                log.warning("Could not remove expired pending image: %s", saved_path, exc_info=True)

    pending_image_path = None
    if not has_attached_image and prompt and BOT_NAME_RE.search(prompt):
        pending = _pending_images.pop(pending_key, None)
        if pending:
            saved_path, saved_at = pending
            if now - saved_at <= PENDING_IMAGE_TTL_SECONDS and saved_path.exists():
                pending_image_path = saved_path
                log.info("Using separately uploaded pending image for chat_id=%s user_id=%s", message.chat_id, user_id)
            else:
                try:
                    saved_path.unlink(missing_ok=True)
                except Exception:
                    log.warning("Could not remove stale pending image: %s", saved_path, exc_info=True)

    has_input_image = has_attached_image or pending_image_path is not None

    # Photo first, instruction second is supported too. Keep the photo for 10 minutes.
    if has_attached_image and (not prompt or not BOT_NAME_RE.search(prompt)):
        file_id = message.photo[-1].file_id if has_photo else message.document.file_id
        telegram_file = await context.bot.get_file(file_id)
        suffix = ".jpg"
        if image_document and message.document.file_name:
            candidate_suffix = Path(message.document.file_name).suffix.lower()
            if candidate_suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                suffix = candidate_suffix
        saved_path = OUTPUT_DIR / f"pending_{message.chat_id}_{user_id}_{message.message_id}{suffix}"
        await telegram_file.download_to_drive(custom_path=str(saved_path))
        if not saved_path.exists() or saved_path.stat().st_size == 0:
            raise GenerationError("Не удалось сохранить исходное фото из Telegram.")
        if saved_path.stat().st_size > 10 * 1024 * 1024:
            saved_path.unlink(missing_ok=True)
            await message.reply_text("Фото больше 10 МБ. Отправь изображение поменьше.")
            return
        previous = _pending_images.pop(pending_key, None)
        if previous:
            try:
                previous[0].unlink(missing_ok=True)
            except Exception:
                log.warning("Could not remove replaced pending image: %s", previous[0], exc_info=True)
        _pending_images[pending_key] = (saved_path, now)
        await message.reply_text("Фото поймал. Теперь напиши: Ferixdi, добавь ей очки")
        log.info("Pending image saved: chat_id=%s user_id=%s bytes=%s", message.chat_id, user_id, saved_path.stat().st_size)
        return

    # The bot name is required. With an attached or pending image, ordinary edit
    # instructions such as "Ferixdi, add glasses" do not need generation keywords.
    if not prompt or not BOT_NAME_RE.search(prompt):
        return
    # The user's rule: addressing Ferixdi is sufficient to start generation.
    # Text-only requests use text-to-image; requests with an image use image-to-image.

    now = time.monotonic()

    if _generation_in_progress:
        if pending_image_path is not None:
            _pending_images[pending_key] = (pending_image_path, now)
            pending_image_path = None
        await message.reply_text(random.choice(BUSY_LINES))
        return

    elapsed = now - _last_generation_finished_at
    if _last_generation_finished_at and elapsed < GENERATION_COOLDOWN_SECONDS:
        if pending_image_path is not None:
            _pending_images[pending_key] = (pending_image_path, now)
            pending_image_path = None
        wait_seconds = max(1, int(GENERATION_COOLDOWN_SECONDS - elapsed))
        await message.reply_text(
            random.choice(COOLDOWN_LINES).format(seconds=wait_seconds)
        )
        return

    cleaned_prompt = clean_generation_prompt(prompt)
    if not cleaned_prompt:
        return

    _generation_in_progress = True
    status = None
    input_image_path = pending_image_path

    try:
        if has_attached_image:
            file_id = message.photo[-1].file_id if has_photo else message.document.file_id
            telegram_file = await context.bot.get_file(file_id)
            suffix = ".jpg"
            if image_document and message.document.file_name:
                candidate_suffix = Path(message.document.file_name).suffix.lower()
                if candidate_suffix in {".jpg", ".jpeg", ".png", ".webp"}:
                    suffix = candidate_suffix
            input_image_path = OUTPUT_DIR / f"input_{message.chat_id}_{message.message_id}{suffix}"
            await telegram_file.download_to_drive(custom_path=str(input_image_path))
            if not input_image_path.exists() or input_image_path.stat().st_size == 0:
                raise GenerationError("Не удалось скачать исходное фото из Telegram.")
            if input_image_path.stat().st_size > 10 * 1024 * 1024:
                raise GenerationError("Фото больше 10 МБ. Отправь изображение поменьше.")
            log.info(
                "Input image received: chat_id=%s message_id=%s bytes=%s type=%s",
                message.chat_id, message.message_id, input_image_path.stat().st_size,
                "photo" if has_photo else "image_document"
            )

        status = await message.reply_text(random.choice(PROGRESS_LINES))
        if input_image_path is not None:
            path = await asyncio.to_thread(
                generate_and_download, cleaned_prompt, input_image_path
            )
        else:
            path = await asyncio.to_thread(generate_and_download, cleaned_prompt)

        # Send directly to the chat, not as a reply to the source message.
        # Retry photo upload, then fall back to a document so Telegram constraints
        # or a transient upload failure do not silently lose a completed image.
        caption = random.choice(SUCCESS_LINES)
        delivery_error = None

        try:
            with path.open("rb") as image_file:
                await context.bot.send_photo(
                    chat_id=message.chat_id,
                    photo=image_file,
                    caption=caption,
                    read_timeout=120,
                    write_timeout=120,
                    connect_timeout=20,
                    pool_timeout=20,
                )
            log.info(
                "Image delivered successfully to Telegram chat_id=%s file=%s bytes=%s",
                message.chat_id, path.name, path.stat().st_size
            )
        except Exception as photo_error:
            delivery_error = photo_error
            log.warning(
                "Photo upload failed; trying document fallback. chat_id=%s error=%r",
                message.chat_id, photo_error, exc_info=True
            )
            try:
                with path.open("rb") as image_file:
                    await context.bot.send_document(
                        chat_id=message.chat_id,
                        document=image_file,
                        caption=caption,
                        read_timeout=120,
                        write_timeout=120,
                        connect_timeout=20,
                        pool_timeout=20,
                    )
                log.info(
                    "Image delivered as document to Telegram chat_id=%s file=%s bytes=%s",
                    message.chat_id, path.name, path.stat().st_size
                )
                delivery_error = None
            except Exception as document_error:
                log.warning(
                    "Document fallback failed; retrying photo once. chat_id=%s error=%r",
                    message.chat_id, document_error, exc_info=True
                )
                delivery_error = document_error
                try:
                    with path.open("rb") as image_file:
                        await context.bot.send_photo(
                            chat_id=message.chat_id,
                            photo=image_file,
                            caption=caption,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=20,
                            pool_timeout=20,
                        )
                    log.info(
                        "Image delivered after final photo retry to Telegram chat_id=%s file=%s",
                        message.chat_id, path.name
                    )
                    delivery_error = None
                except Exception as retry_error:
                    delivery_error = retry_error
                    log.exception(
                        "Image generated but every Telegram delivery attempt failed; "
                        "chat_id=%s file=%s original_error=%r",
                        message.chat_id, path.name, photo_error
                    )

        if delivery_error is not None:
            try:
                await status.edit_text(random.choice(DELIVERY_ERROR_LINES))
            except Exception:
                log.exception("Could not edit progress message; sending delivery error as a new message")
                try:
                    await message.reply_text(random.choice(DELIVERY_ERROR_LINES))
                except Exception:
                    log.exception("Could not notify chat about image delivery failure")
            return

        try:
            await status.delete()
        except Exception:
            log.warning("Could not delete generation progress message", exc_info=True)

    except Exception:
        log.exception("Image generation failed")
        error_text = random.choice(ERROR_LINES)
        try:
            if status:
                await status.edit_text(error_text)
            else:
                await message.reply_text(error_text)
        except Exception:
            log.exception("Could not edit status message; sending generation error as a new message")
            try:
                await message.reply_text(error_text)
            except Exception:
                log.exception("Could not send generation error to Telegram")
    finally:
        if input_image_path is not None:
            try:
                input_image_path.unlink(missing_ok=True)
            except Exception:
                log.warning("Could not remove temporary input image: %s", input_image_path, exc_info=True)
        _generation_in_progress = False
        _last_generation_finished_at = time.monotonic()


def main():
    if not TOKEN:
        raise RuntimeError("В Render Environment не задан TELEGRAM_BOT_TOKEN.")
    if ALLOWED_CHAT_ID is None:
        raise RuntimeError(
            "В Render Environment не задан ALLOWED_CHAT_ID. "
            "Укажи числовой ID целевой Telegram-группы, не личный ID пользователя."
        )

    telegram_request = HTTPXRequest(
        connection_pool_size=16,
        connect_timeout=20,
        read_timeout=120,
        write_timeout=120,
        media_write_timeout=120,
        pool_timeout=20,
    )
    polling_request = HTTPXRequest(
        connection_pool_size=4,
        connect_timeout=20,
        read_timeout=45,
        write_timeout=20,
        pool_timeout=20,
    )
    app = (
        Application.builder()
        .token(TOKEN)
        .request(telegram_request)
        .get_updates_request(polling_request)
        .build()
    )
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            (filters.TEXT & ~filters.COMMAND) | filters.PHOTO | filters.Document.IMAGE,
            handle_prompt,
        )
    )

    log.info(
        "Telegram bot started; provider: Kie AI / GPT Image 2.5; "
        "group access enabled for chat_id=%s; admin private access for user_id=%s; "
        "cooldown=%ss; bot name required for every generation.",
        ALLOWED_CHAT_ID,
        ALLOWED_USER_ID,
        GENERATION_COOLDOWN_SECONDS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
