import asyncio
import logging
import os
import random
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

# Rotating copy keeps the bot lively without roasting the person who asked.
START_ADMIN_LINES = [
    "О, босс-кожаный, личный вход открыт. Тебе — зелёный свет, пикселям — тяжёлая смена.",
    "Начальство прибыло. Мои нейроны поправили галстуки: можно генерировать прямо здесь или в группе.",
    "Для тебя, босс, отдельный вход. Пользуйся властью разумно: например, закажи кота в скафандре.",
]
START_PRIVATE_LINES = [
    "В личке я пока только красиво стою. За генерацией заходи в нашу группу — там мой художественный цех.",
    "Кожаный, мой мольберт находится в группе. Здесь я могу поболтать, но пиксели на работу не выходят.",
    "Тут у меня режим «поболтать у кофемашины». Для картинок загляни в группу — там начинается пиксельный цирк.",
]
START_GROUP_LINES = [
    "Так, кожаные, открываем художественный цех. Я — ваш пиксельный демон, но с лимитом терпения и без берета.",
    "Добро пожаловать в мастерскую, где слова превращаются в картинки, а GPU иногда смотрит на нас с укором.",
    "На связи ваш карманный художник GPT Image 2.5. Руки у меня виртуальные, зато амбиции в разрешении 1K.",
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
    "Промпт принят. GPT Image 2.5 уже раскладывает твою идею на пиксели. Не мешай магии выглядеть как вычисления.",
    "Так, кожаный, запускаю GPT Image 2.5 в 1K. Внутри сервера сейчас совещание пикселей без протокола.",
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
    "Пиксельный форс-мажор: генерация не завершилась. Я не стану запускать её повторно автоматически, чтобы не сжечь твои кредиты.",
    "Тут серверный сюрприз, и, увы, не тот, который можно повесить на стену. Генерация не завершилась; повторно платную задачу сам не запускаю.",
    "Мои цифровые гномы уронили гаечный ключ. Картинка не готова, но я не буду повторять платный запуск без команды.",
    "Неудачный раунд, кожаный. Генерация споткнулась на серверной стороне; автоматически тратить кредиты второй раз не стану.",
]


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
    example = (
        "Пример: «Сгенерируй изображение: лысый енот в дорогом костюме "
        "на заседании совета директоров»."
    )
    settings = "Формат — 9:16, разрешение — 1K."
    if chat and chat.type == "private":
        if ALLOWED_USER_ID is not None and user and user.id == ALLOWED_USER_ID:
            await message.reply_text(
                f"{random.choice(START_ADMIN_LINES)}\n\n{example}\n{settings}"
            )
        else:
            await message.reply_text(
                f"{random.choice(START_PRIVATE_LINES)}\n\n{example}\n{settings}"
            )
        return

    if not can_generate_here(update):
        return

    await message.reply_text(
        f"{random.choice(START_GROUP_LINES)}\n\n"
        "Команды: «сгенерируй изображение ...», «нарисуй ...», "
        "«создай фото ...» или «сделай фото ...».\n"
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
    if not prompt or not IMAGE_INTENT_RE.search(prompt):
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

    _generation_in_progress = True
    status = None

    try:
        status = await message.reply_text(random.choice(PROGRESS_LINES))
        path = await asyncio.to_thread(generate_and_download, prompt)

        with path.open("rb") as image_file:
            await message.reply_photo(
                photo=image_file,
                caption=(
                    f"{random.choice(SUCCESS_LINES)}\n\n"
                    "GPT Image 2.5 · 1K · 9:16"
                ),
                reply_to_message_id=message.message_id,
            )
        await status.delete()

    except Exception:
        log.exception("Image generation failed")
        error_text = (
            f"{random.choice(ERROR_LINES)}\n\n"
            "Попробуй ещё раз чуть позже. Если сбой повторится, лучше проверить логи — "
            "не будем лечить сервер шаманством и повторными списаниями."
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
