import asyncio
import logging
import os
import re
import time
from pathlib import Path

from dotenv import load_dotenv
from telegram import Update
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
# Only explicit image-creation requests trigger a paid generation.
IMAGE_INTENT_RE = re.compile(
    r"\b(?:сгенерируй(?:-ка)?|сгенерировать|генерируй|генерировать|"
    r"нарисуй|нарисовать|изобрази|изобразить|"
    r"создай\s+(?:изображение|картинку|фото)|"
    r"сделай\s+(?:фото|картинку|изображение)|"
    r"сделать\s+(?:фото|картинку|изображение)|"
    r"сгенерируй\s+(?:изображение|картинку|фото))\b",
    re.IGNORECASE,
)

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ferixdi-image-bot")

_generation_in_progress = False
_last_generation_finished_at = 0.0


class GenerationError(Exception):
    """A user-safe generation error."""


def generate_and_download(prompt: str) -> Path:
    """Implemented by cloud_runner.py, which calls the Kie AI API."""
    raise GenerationError("Генератор не инициализирован. Перезапусти Worker, кожаный.")


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
    if chat and chat.type == "private":
        if ALLOWED_USER_ID is not None and user and user.id == ALLOWED_USER_ID:
            await message.reply_text(
                "О, босс-кожаный, для тебя личный вход открыт. Можешь генерировать прямо здесь "
                "или в группе. Пиши одним сообщением, например: «Сгенерируй изображение: "
                "рыжий кот в космическом скафандре». Разрешение строго 1K, формат 9:16."
            )
        else:
            await message.reply_text(
                "Кожаный, в личке генератор не работает. Заходи в нашу группу и напиши "
                "одним сообщением, например: «Сгенерируй изображение: рыжий кот в космическом "
                "скафандре». Там и запустится магия. Разрешение строго 1K, формат 9:16."
            )
        return

    if not can_generate_here(update):
        return

    await message.reply_text(
        "О, кожаные собрались. Я ваш карманный пиксельный демон на GPT Image 2.5. "
        "Напиши в группе одним сообщением команду и промпт: «сгенерируй изображение ...», "
        "«нарисуй ...», «создай фото ...» или «сделай фото ...». "
        "Каждая картинка — строго 1K, формат 9:16. Пиксели сами себя не нарисуют."
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
    if not prompt or not IMAGE_INTENT_RE.search(prompt):
        return

    now = time.monotonic()

    if _generation_in_progress:
        await message.reply_text(
            "Кожаный, притормози 😏 Я ещё предыдущую картинку собираю. "
            "Это генератор изображений, а не шаурма на вокзале — по одной задаче."
        )
        return

    elapsed = now - _last_generation_finished_at
    if _last_generation_finished_at and elapsed < GENERATION_COOLDOWN_SECONDS:
        wait_seconds = max(1, int(GENERATION_COOLDOWN_SECONDS - elapsed))
        await message.reply_text(
            f"Кожаный, я только что вытащил пиксели из цифрового болота. "
            f"Дай отдышаться ещё {wait_seconds} сек. — и снова в бой."
        )
        return

    _generation_in_progress = True
    status = None

    try:
        status = await message.reply_text(
            "Так, кожаный, промпт принят. Запускаю GPT Image 2.5 в разрешении 1K — "
            "сейчас пиксельные гномы разберут твою задумку по слоям."
        )
        path = await asyncio.to_thread(generate_and_download, prompt)

        with path.open("rb") as image_file:
            await message.reply_photo(
                photo=image_file,
                caption="Кожаный, держи результат. Пиксели уложены, магия случилась. · GPT Image 2.5 · 1K · 9:16",
                reply_to_message_id=message.message_id,
            )
        await status.delete()

    except Exception as exc:
        log.exception("Image generation failed")
        safe_message = str(exc).strip()[:900] or "Неизвестная ошибка."
        error_text = (
            "Кожаный, у нас пиксельный форс-мажор: генерация не завершилась. "
            "Повторно платную задачу автоматически не запускаю, чтобы не сжечь кредиты.\n\n"
            f"Что случилось: {safe_message}"
        )
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

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt)
    )

    log.info(
        "Telegram bot started; provider: Kie AI / GPT Image 2.5; "
        "group access enabled for chat_id=%s; admin private access for user_id=%s; "
        "cooldown=%ss after each task; explicit image intent required; resolution=1K.",
        ALLOWED_CHAT_ID,
        ALLOWED_USER_ID,
        GENERATION_COOLDOWN_SECONDS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
