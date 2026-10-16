import asyncio
import logging
import os
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

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("ferixdi-image-bot")


class GenerationError(Exception):
    """A user-safe generation error."""


def generate_and_download(prompt: str) -> Path:
    """Implemented by cloud_runner.py, which calls the Kie AI API."""
    raise GenerationError("Генератор не инициализирован. Перезапусти Worker.")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    user = update.effective_user
    if not user or user.id != ALLOWED_USER_ID:
        return

    await update.effective_message.reply_text(
        "На связи. Пришли промпт текстом — сгенерирую изображение "
        "через GPT Image 2.5 в формате 9:16."
    )


async def handle_prompt(
    update: Update,
    context: ContextTypes.DEFAULT_TYPE,
):
    message = update.effective_message
    user = update.effective_user

    if not user or user.id != ALLOWED_USER_ID:
        return

    prompt = (message.text or "").strip()
    if not prompt:
        return

    status = await message.reply_text(
        "Промпт принял. Передаю в GPT Image 2.5 — это может занять немного времени."
    )

    try:
        # The API polling is blocking I/O, so keep it outside the asyncio event loop.
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
        try:
            await status.edit_text(
                "Не получилось завершить генерацию. "
                "Новая задача автоматически не запускалась, чтобы не списывать "
                "кредиты повторно.\n\n"
                f"Что произошло: {safe_message}"
            )
        except Exception:
            await message.reply_text(
                f"Не получилось завершить генерацию: {safe_message}"
            )


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

    log.info("Telegram bot started; image provider: Kie AI / GPT Image 2.5.")
    app.run_polling()


if __name__ == "__main__":
    main()
