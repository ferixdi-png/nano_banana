import asyncio
import logging
import os
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
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_CHAT_ID_RAW = os.getenv("ALLOWED_CHAT_ID", "").strip()
ALLOWED_CHAT_ID = int(ALLOWED_CHAT_ID_RAW) if ALLOWED_CHAT_ID_RAW else None

# Avoid parallel paid tasks and rapid accidental repeat requests.
GENERATION_COOLDOWN_SECONDS = 25

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ferixdi-image-bot")

_generation_in_progress = False
_last_generation_started_at = 0.0


class GenerationError(Exception):
    """A user-safe generation error."""


def generate_and_download(prompt: str) -> Path:
    """Implemented by cloud_runner.py, which calls the Kie AI API."""
    raise GenerationError("Генератор не инициализирован. Перезапусти Worker.")


def is_authorized(update: Update) -> bool:
    user = update.effective_user
    chat = update.effective_chat
    if not user or user.id != ALLOWED_USER_ID:
        return False
    if ALLOWED_CHAT_ID is not None and (not chat or chat.id != ALLOWED_CHAT_ID):
        return False
    return True


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if not is_authorized(update):
        return

    await update.effective_message.reply_text(
        "На связи. Пришли промпт текстом — сгенерирую изображение "
        "через GPT Image 2.5 в формате 9:16. Только давай без очереди из "
        "двадцати задач подряд, я всё-таки тоже стараюсь."
    )


async def handle_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    global _generation_in_progress, _last_generation_started_at

    message = update.effective_message
    if not message or not is_authorized(update):
        return

    prompt = (message.text or "").strip()
    if not prompt:
        return

    now = time.monotonic()

    if _generation_in_progress:
        await message.reply_text(
            "Так, погоди 😅 Я ещё предыдущую картинку делаю. "
            "Дай мне её закончить, не устраивай очередь на заводе."
        )
        return

    elapsed = now - _last_generation_started_at
    if _last_generation_started_at and elapsed < GENERATION_COOLDOWN_SECONDS:
        wait_seconds = max(1, int(GENERATION_COOLDOWN_SECONDS - elapsed))
        await message.reply_text(
            f"Погоди немного, я от тебя уже устал 🙃 "
            f"Дай передохнуть ещё {wait_seconds} сек. и снова в бой."
        )
        return

    # Set the guard before the first await so simultaneous updates cannot
    # start multiple paid tasks at the same time.
    _generation_in_progress = True
    _last_generation_started_at = now
    status = None

    try:
        status = await message.reply_text(
            "Промпт принял. Передаю в GPT Image 2.5 — это может занять немного времени."
        )
        path = await asyncio.to_thread(generate_and_download, prompt)

        with path.open("rb") as image_file:
            await message.reply_photo(
                photo=image_file,
                caption="Готово · GPT Image 2.5 · 1K · 9:16",
            )
        await status.delete()

    except Exception as exc:
        log.exception("Image generation failed")
        safe_message = str(exc).strip()[:900] or "Неизвестная ошибка."
        error_text = (
            "Не получилось завершить генерацию. "
            "Я не запускал повторную платную задачу автоматически, чтобы "
            "не списывать кредиты второй раз.\n\n"
            f"Что произошло: {safe_message}"
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


def main():
    if not TOKEN:
        raise RuntimeError("В Render Environment не задан TELEGRAM_BOT_TOKEN.")
    if not ALLOWED_USER_ID:
        raise RuntimeError("В Render Environment не задан ALLOWED_USER_ID.")

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt)
    )

    log.info(
        "Telegram bot started; provider: Kie AI / GPT Image 2.5; "
        "user and optional chat restrictions enabled; cooldown=%ss.",
        GENERATION_COOLDOWN_SECONDS,
    )
    app.run_polling()


if __name__ == "__main__":
    main()
