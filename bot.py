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
from telegram.error import TimedOut
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
IMAGE_INTENT_RE = re.compile(
    r"\b(?:сгенерируй(?:-ка)?|сгенерировать|генерируй|генерировать|"
    r"нарисуй|нарисовать|изобрази|изобразить|"
    r"создай\s+(?:изображение|картинку|фото)|"
    r"сделай\s+(?:фото|картинку|изображение)|"
    r"сделать\s+(?:фото|картинку|изображение)|"
    r"сгенерируй\s+(?:изображение|картинку|фото))\b",
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

# Rotating copy keeps the bot lively without roasting the person who asked.
START_ADMIN_LINES = [
    "О, босс-кожаный, личный вход открыт. Тебе — зелёный свет, пикселям — тяжёлая смена.",
    "Начальство прибыло. Мои нейроны поправили галстуки: можно генерировать прямо здесь или в группе.",
    "Для тебя, босс, отдельный вход. Пользуйся властью разумно: например, закажи кота в скафандре.",
]
START_PRIVATE_LINES = [
    "За генерацией заходи в чат креаторов: https://t.me/ai_neiro_chat",
]
START_GROUP_LINES = [
    "Так, кожаные, открываем художественный цех. Я — ваш пиксельный демон, но с лимитом терпения и без берета.",
    "Добро пожаловать в мастерскую, где даже самая безумная идея может стать картинкой.",
    "На связи ваш карманный художник. Руки виртуальные, фантазия без выходных.",
]
BUSY_LINES = [
    "Кожаный, по одному шедевру за раз. Я ещё предыдущий пиксельный суп не разлил по холсту.",
    "Очередь, маэстро. Я сейчас собираю предыдущую картинку по атомам — второй заказ пока подождёт.",
    "Я не осьминог, хотя щупальца из кода уже растут. Доделаю текущую картинку — приму следующую.",
    "Секундочку, творческий директор. У меня сейчас один холст и очень много цифровой ответственности.",
]
COOLDOWN_LINES = [
    "Пиксели просят технический перерыв. Подожди ещё {seconds} сек. — и снова устроим искусству стресс-тест.",
    "Кожаный, дай нейронам глотнуть виртуальной воды. Осталось {seconds} сек., потом снова за кисти.",
    "Следующий шедевр через {seconds} сек. Даже цифровому художнику иногда надо сделать вид, что он отдыхает.",
    "Я уже бегу, но мои пиксели пока завязывают шнурки. Подожди {seconds} сек.",
]
PROGRESS_LINES = [
    "Идею поймал. Сейчас превращу её в картинку — держись, искусство уже надевает рабочие штаны.",
    "Так, кожаный, запускаю художественную магию. Внутри сейчас совещание пикселей без протокола.",
    "Задачу поймал. Отправляю задумку в цифровую кухню — надеюсь, сегодня без подгорания реальности.",
    "Начинаю колдовать. Если услышишь тихий шум вентилятора — это мои нейроны драматично работают.",
]
SUCCESS_LINES = [
    "Готово. Пиксели уложены, реальность слегка подправлена. Не зазнавайся — это мы вместе красиво придумали.",
    "Держи шедевр, кожаный. Я бы подписал его своим именем, но у меня даже паспорта нет.",
    "Готово! Цифровые гномы сдали смену, картинка — тебе. Искусство опять вышло из-под контроля.",
    "Забирай результат. Где-то один пиксель сейчас гордится собой сильнее, чем положено.",
    "Готово. Я сделал вид, что всё было легко. Ты сделай вид, что не сомневался.",
]
ERROR_LINES = [
    "Ой, сегодня картинка не сложилась. Даже у цифровых художников бывает день «ну вот опять». Попробуй ещё раз чуть позже.",
    "Кажется, мои художественные способности ушли за кофе и пока не вернулись. Давай попробуем ещё раз чуть позже.",
    "Творческий затык! Я уже мысленно надел берет, но холст пока пустой. Попробуй ещё раз чуть позже.",
    "Не получилось с первого раза, кожаный. Не драматизируем — даже у шедевров бывают сложные роды.",
]
DELIVERY_ERROR_LINES = [
    "Картинка уже готова, но Telegram зажал дверь на выходе. Попробуй попросить её ещё раз чуть позже.",
    "Шедевр нарисован, но доставщик сегодня явно перепутал адрес. Не получилось отправить картинку — попробуй ещё раз чуть позже.",
    "Картина готова, а вот доставка споткнулась о порог. Попробуй ещё раз чуть позже — не будем объявлять пиксели в розыск.",
]


class GenerationError(Exception):
    """A user-safe generation error."""


def generate_and_download(prompt: str) -> Path:
    """Implemented by cloud_runner.py, which calls the Kie AI API."""
    raise GenerationError("Генератор не инициализирован. Перезапусти Worker, кожаный.")

def clean_generation_prompt(message_text: str) -> str:
    """Remove bot addressing and command wrappers before sending the prompt to the image model."""
    prompt = BOT_NAME_RE.sub(" ", message_text)
    # Remove command phrasing only at the beginning; keep the user's actual visual description.
    prompt = re.sub(
        r"^\\s*(?:[,.:;!—–-]+\\s*)*"
        r"(?:сгенерируй(?:-ка)?|сгенерировать|генерируй|генерировать|"
        r"нарисуй|нарисовать|изобрази|изобразить|"
        r"создай|сделай|сделать)\\b\\s*"
        r"(?:(?:изображение|картинку|фото|картинку-фото)\\b\\s*)*"
        r"[,.:;!—–-]*\\s*",
        "",
        prompt,
        count=1,
        flags=re.IGNORECASE,
    )
    # Strip a leading English prompt label if used as a command prefix.
    prompt = re.sub(r"^\\s*prompt\\s*[:—–-]?\\s*", "", prompt, count=1, flags=re.IGNORECASE)
    return re.sub(r"\\s+", " ", prompt).strip(" \\t\\r\\n,.:;!—–-")


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
    if not message or not can_generate_here(update):
        return

    prompt = (message.text or "").strip()
    # Strict gate: without the bot's name, the message must never start generation.
    if not prompt or not BOT_NAME_RE.search(prompt):
        return
    if not (IMAGE_INTENT_RE.search(prompt) or KEYWORD_TRIGGER_RE.search(prompt)):
        return

    now = time.monotonic()

    if _generation_in_progress:
        await message.reply_text(random.choice(BUSY_LINES))
        return

    elapsed = now - _last_generation_finished_at
    if _last_generation_finished_at and elapsed < GENERATION_COOLDOWN_SECONDS:
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

    try:
        status = await message.reply_text(random.choice(PROGRESS_LINES))
        path = await asyncio.to_thread(generate_and_download, cleaned_prompt)

        try:
            for attempt in range(2):
                try:
                    with path.open("rb") as image_file:
                        await message.reply_photo(
                            photo=image_file,
                            caption=random.choice(SUCCESS_LINES),
                            reply_to_message_id=message.message_id,
                            read_timeout=120,
                            write_timeout=120,
                            connect_timeout=20,
                            pool_timeout=20,
                        )
                    break
                except TimedOut:
                    if attempt == 1:
                        raise
                    log.warning("Telegram image upload timed out; retrying once")
                    await asyncio.sleep(1)
        except Exception:
            log.exception("Image was generated but Telegram delivery failed")
            await status.edit_text(random.choice(DELIVERY_ERROR_LINES))
            return

        await status.delete()

    except Exception:
        log.exception("Image generation failed")
        error_text = random.choice(ERROR_LINES)
        try:
            if status:
                await status.edit_text(error_text)
            else:
                await message.reply_text(error_text)
        except Exception:
            log.exception("Could not send generation error to Telegram")
    finally:
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
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt)
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
