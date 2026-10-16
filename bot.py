import asyncio
import base64
import json
import logging
import os
import re
import time
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from playwright.sync_api import sync_playwright

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222" if os.name == "nt" else "").strip()  # Local Windows default; leave unset on Render
FLOW_PROJECT_URL = os.getenv("FLOW_PROJECT_URL", "").strip()
PROFILE_DIR = Path(os.getenv("FLOW_PROFILE_DIR", "/var/data/flow-profile"))
STATE_PATH = Path(os.getenv("FLOW_STORAGE_STATE_PATH", "/var/data/flow-storage-state.json"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/flow-output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_ATTEMPTS = 3
GENERATION_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 30

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("flow-telegram-bot")
browser_lock = Lock()



class FlowError(Exception):
    pass


def prepare_storage_state():
    """Materialize a session exported from the user's logged-in local Flow tab."""
    encoded = os.getenv("FLOW_STORAGE_STATE_B64", "").strip()
    if encoded and not STATE_PATH.exists():
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        try:
            STATE_PATH.write_bytes(base64.b64decode(encoded))
            json.loads(STATE_PATH.read_text(encoding="utf-8"))
        except Exception as exc:
            STATE_PATH.unlink(missing_ok=True)
            raise FlowError("FLOW_STORAGE_STATE_B64 is not valid base64-encoded Playwright storage state.") from exc


def get_project_page(context):
    pages = context.pages
    page = next((p for p in pages if "/project/" in p.url), None)

    if page is None:
        page = pages[0] if pages else context.new_page()
        target = FLOW_PROJECT_URL or "https://flow.google.com/"
        page.goto(target, wait_until="domcontentloaded", timeout=60000)

    page.wait_for_timeout(1200)
    if "/project/" not in page.url:
        raise FlowError(
            "Не найдена страница проекта Google Flow. Проверь FLOW_PROJECT_URL "
            "и авторизацию. Если Google просит войти, обнови сессию и экспортируй её заново."
        )

    project_match = re.search(r"/project/([^/]+)", page.url)
    if not project_match:
        raise FlowError("Не удалось определить проект Flow.")

    if "/edit/" in page.url:
        back = page.get_by_role("button", name='Кнопка "Назад" для возврата к предыдущей странице')
        if back.count():
            back.click()
            try:
                page.wait_for_url(lambda url: "/edit/" not in str(url), timeout=10000)
            except Exception:
                pass
    if "/edit/" in page.url:
        page.goto(f"https://flow.google.com/project/{project_match.group(1)}", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1000)
    return page


def choose_setting(page, option):
    page.get_by_role("button", name="Триггер настроек").click()
    page.wait_for_timeout(250)
    page.get_by_text(option, exact=True).click()
    page.wait_for_timeout(350)


def set_generation_settings(page):
    trigger = page.get_by_role("button", name="Триггер настроек")
    if "Nano Banana 2.1" not in trigger.inner_text():
        raise FlowError("В Flow выбрана не Nano Banana 2.1. Выбери её в настройках и повтори.")
    choose_setting(page, "Изображение")
    choose_setting(page, "9:16")
    choose_setting(page, "x1")


def image_snapshots(page):
    return page.locator('img[alt="Карточка с изображением пользователя"]').evaluate_all(
        """imgs => imgs.map(img => ({
            src: img.currentSrc || img.src,
            width: img.naturalWidth,
            height: img.naturalHeight
        }))"""
    )


def wait_for_new_image(page, old_sources):
    deadline = time.monotonic() + GENERATION_TIMEOUT
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        for image in image_snapshots(page):
            src = image.get("src", "")
            if src and src not in old_sources and image.get("width", 0) > 32 and image.get("height", 0) > 32:
                return src
        body = page.locator("body").inner_text().lower()
        if any(marker in body for marker in ("sign in", "войдите в аккаунт", "insufficient credits", "недостаточно кредитов")):
            raise FlowError("Flow сообщает о проблеме с авторизацией или доступными кредитами.")
    raise TimeoutError("Новое изображение не появилось за отведённое время.")


def persist_session(context):
    try:
        STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
        context.storage_state(path=str(STATE_PATH))
    except Exception:
        log.exception("Не удалось сохранить состояние браузерной сессии")


def generate_and_download(prompt):
    with browser_lock:
        prepare_storage_state()
        with sync_playwright() as p:
            browser = None
            context = None
            close_browser = False
            try:
                if CDP_URL:
                    browser = p.chromium.connect_over_cdp(CDP_URL)
                    if not browser.contexts:
                        raise FlowError("В подключённом Chrome нет браузерного контекста.")
                    context = browser.contexts[0]
                else:
                    browser = p.chromium.launch(headless=True, args=["--no-sandbox", "--disable-dev-shm-usage"])
                    close_browser = True
                    if STATE_PATH.exists():
                        context = browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
                    else:
                        raise FlowError(
                            "Не найдена авторизованная сессия Google Flow. "
                            "Нужно экспортировать сессию из локального Chrome и добавить FLOW_STORAGE_STATE_B64 в Render."
                        )

                page = get_project_page(context)
                set_generation_settings(page)
                prompt_box = page.get_by_role("textbox", name="Что вы хотите создать?")
                prompt_box.fill(prompt)
                old_sources = {item.get("src") for item in image_snapshots(page) if item.get("src")}
                page.get_by_role("button", name="Начать генерацию").click()
                new_src = wait_for_new_image(page, old_sources)

                image_index = page.locator('img[alt="Карточка с изображением пользователя"]').evaluate_all(
                    """(imgs, target) => {
                        const found = imgs.find(img => (img.currentSrc || img.src) === target);
                        return found ? imgs.indexOf(found) : -1;
                    }""",
                    new_src,
                )
                if image_index < 0:
                    raise FlowError("Новое изображение найдено, но карточка пропала.")
                page.locator('img[alt="Карточка с изображением пользователя"]').nth(image_index).click()
                page.wait_for_url("**/edit/**", timeout=20000)
                page.wait_for_timeout(500)
                page.get_by_role("button", name="Скачать медиаконтент").click()
                page.wait_for_timeout(250)
                with page.expect_download(timeout=DOWNLOAD_TIMEOUT * 1000) as info:
                    page.get_by_role("menuitem", name="1K Исходный размер").click()
                download = info.value
                filename = Path(download.suggested_filename).name or f"flow_{int(time.time())}.jpg"
                destination = OUTPUT_DIR / filename
                download.save_as(str(destination))
                if not destination.exists() or destination.stat().st_size == 0:
                    raise FlowError("Flow не сохранил файл.")
                persist_session(context)
                log.info("Изображение сохранено: %s", destination)
                return destination
            finally:
                if context is not None and not CDP_URL:
                    persist_session(context)
                    context.close()
                if browser is not None and close_browser:
                    browser.close()


def is_allowed(update: Update) -> bool:
    """Allow the configured Telegram group, plus the configured owner in private chat."""
    message = update.effective_message
    user = update.effective_user
    chat = update.effective_chat
    if not message or not user or not chat:
        return False
    if chat.type in ("group", "supergroup"):
        return bool(ALLOWED_CHAT_ID and chat.id == ALLOWED_CHAT_ID)
    return bool(ALLOWED_USER_ID and user.id == ALLOWED_USER_ID and chat.type == "private")


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if is_allowed(update):
        await update.effective_message.reply_text(
            "Я на связи. Для генерации напиши: «сгенерируй изображение …» или /img и опиши результат."
        )


async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    if not is_allowed(update) or not message:
        return
    prompt = (message.text or "").strip()
    if not prompt:
        return
    is_group = update.effective_chat.type in ("group", "supergroup")
    if is_group:
        # In group chats, generate only when explicitly requested to avoid turning every message into a paid generation.
        command_prompt = re.match(r"^/img(?:@\\w+)?\\s*(.*)$", prompt, flags=re.IGNORECASE | re.DOTALL)
        explicit_request = re.search(
            r"\\b(сгенерируй|сгенерировать|нарисуй|создай изображение|сделай картинку|генерируй)\\b",
            prompt,
            flags=re.IGNORECASE,
        )
        if command_prompt:
            prompt = command_prompt.group(1).strip()
        elif explicit_request:
            prompt = re.sub(
                r"^.*?\\b(сгенерируй|сгенерировать|нарисуй|создай изображение|сделай картинку|генерируй)\\b[:,!\\s-]*",
                "",
                prompt,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        else:
            return
        if not prompt:
            await message.reply_text("Опиши, какое изображение нужно создать.")
            return
    status = await message.reply_text("Принял промпт. Запускаю генерацию в Flow…")
    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if attempt > 1:
                await status.edit_text(f"Повторная попытка {attempt}/{MAX_ATTEMPTS}…")
            path = await asyncio.to_thread(generate_and_download, prompt)
            with path.open("rb") as photo:
                await message.reply_photo(photo=photo, caption="Готово · Nano Banana 2.1 · 1K · 9:16")
            await status.delete()
            return
        except Exception as exc:
            log.exception("Попытка %s завершилась ошибкой", attempt)
            if attempt >= MAX_ATTEMPTS:
                await status.edit_text(f"Не удалось получить изображение после {MAX_ATTEMPTS} попыток.\n\nПричина: {str(exc)[:700]}")
                return
            await status.edit_text(f"Попытка {attempt} не удалась. Жду 10 секунд и повторяю…")
            await asyncio.sleep(10)


def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN.")
    if not ALLOWED_USER_ID and not ALLOWED_CHAT_ID:
        raise RuntimeError("Задай ALLOWED_USER_ID и/или ALLOWED_CHAT_ID.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt))
    log.info("Telegram-бот запускается.")
    external_url = os.getenv("RENDER_EXTERNAL_URL", "").strip().rstrip("/")
    port = int(os.getenv("PORT", "10000"))
    if external_url:
        # Render Web Service: webhook keeps inbound Telegram updates tied to the public HTTP service.
        webhook_path = "telegram-webhook"
        webhook_url = f"{external_url}/{webhook_path}"
        import secrets
        secret_token = secrets.token_urlsafe(32)
        log.info("Starting Telegram webhook on port %s", port)
        app.run_webhook(
            listen="0.0.0.0",
            port=port,
            url_path=webhook_path,
            webhook_url=webhook_url,
            secret_token=secret_token,
            drop_pending_updates=False,
        )
    else:
        log.info("Starting Telegram polling mode")
        app.run_polling()


if __name__ == "__main__":
    main()
