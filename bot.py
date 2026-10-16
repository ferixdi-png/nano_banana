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
    """Choose a Flow setting even if its menu label contains extra descriptive text."""
    trigger = page.get_by_role("button", name="Триггер настроек")
    trigger.click()
    page.wait_for_timeout(500)

    # Flow's labels can vary slightly between UI versions/locales.
    exact = page.get_by_text(option, exact=True)
    if exact.count():
        exact.first.click(timeout=8000)
        page.wait_for_timeout(400)
        return

    partial = page.get_by_text(re.compile(re.escape(option), re.IGNORECASE))
    if partial.count():
        partial.first.click(timeout=8000)
        page.wait_for_timeout(400)
        return

    # Include a short DOM snapshot in the error so the next failure tells us
    # which options Flow actually rendered instead of only timing out.
    try:
        menu_text = page.locator("body").inner_text()[-3500:]
    except Exception:
        menu_text = "(не удалось прочитать текст страницы)"
    raise FlowError(
        f"В меню настроек Flow не найден вариант «{option}». "
        f"Текст страницы в конце меню: {menu_text}"
    )


def set_generation_settings(page):
    """First prove Flow can generate at all; do not block on fragile aspect/quantity menus."""
    trigger = page.get_by_role("button", name="Триггер настроек")
    trigger_text = trigger.inner_text()
    log.info("Flow settings trigger: %r", trigger_text)
    if "Nano Banana 2.1" not in trigger_text:
        raise FlowError(
            "Не удалось подтвердить Nano Banana 2.1 в Flow. "
            f"Текущие настройки: {trigger_text!r}"
        )
    # Deliberately keep the UI's current image mode, aspect ratio and quantity.
    # The first goal is to test whether an actual generation can be started.
    log.info("Flow model verified; keeping current settings for diagnostic generation.")


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
    last_body = ""
    while time.monotonic() < deadline:
        page.wait_for_timeout(1500)
        for image in image_snapshots(page):
            src = image.get("src", "")
            if src and src not in old_sources and image.get("width", 0) > 32 and image.get("height", 0) > 32:
                log.info("Flow produced a new image (%sx%s).", image.get("width"), image.get("height"))
                return src
        last_body = page.locator("body").inner_text()
        body = last_body.lower()
        # Informational Google Flow bonus banners do not block zero-credit Nano Banana generations.
        if any(marker in body for marker in ("sign in", "войдите в аккаунт", "нужно войти в аккаунт")):
            raise FlowError(
                "Flow требует авторизацию. "
                f"Текст страницы: {last_body[-1800:]}"
            )
        if int(time.monotonic()) % 10 < 2:
            log.info("Flow generation still pending; URL=%s; page_tail=%r", page.url, last_body[-900:])
    raise TimeoutError(
        "Flow не показал новое изображение за отведённое время после нажатия генерации. "
        f"Текст страницы в конце ожидания: {last_body[-2200:]}"
    )


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
                log.info("Prompt filled. Clicking Flow generation button now.")
                page.get_by_role("button", name="Начать генерацию").click()
                log.info("Flow generation button clicked; waiting up to %s seconds for a new image.", GENERATION_TIMEOUT)
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
    chat = update.effective_chat
    if not is_allowed(update) or not message or not chat:
        return

    prompt = (message.text or "").strip()
    if not prompt:
        return

    is_group = chat.type in ("group", "supergroup")
    command_prompt = re.match(r"^/img(?:@\w+)?\s*(.*)$", prompt, flags=re.IGNORECASE | re.DOTALL)

    if is_group:
        # In group chats, generate only when explicitly requested to avoid accidental generations.
        explicit_request = re.search(
            r"\b(сгенерируй|сгенерировать|нарисуй|создай изображение|сделай картинку|генерируй)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        if command_prompt:
            prompt = command_prompt.group(1).strip()
        elif explicit_request:
            prompt = re.sub(
                r"^.*?\b(сгенерируй|сгенерировать|нарисуй|создай изображение|сделай картинку|генерируй)\b[:,!\s-]*",
                "",
                prompt,
                count=1,
                flags=re.IGNORECASE | re.DOTALL,
            ).strip()
        else:
            return
    elif command_prompt:
        # CommandHandler passes the original message text, so remove /img in private chats too.
        prompt = command_prompt.group(1).strip()

    if not prompt:
        await context.bot.send_message(chat_id=chat.id, text="Опиши, какое изображение нужно создать.")
        return

    # Send normally, not as a reply: the original message may have been deleted.
    status = await context.bot.send_message(chat_id=chat.id, text="Принял промпт. Запускаю генерацию в Flow…")

    for attempt in range(1, MAX_ATTEMPTS + 1):
        if attempt > 1:
            try:
                await status.edit_text(f"Повторная попытка {attempt}/{MAX_ATTEMPTS}…")
            except Exception:
                log.exception("Не удалось обновить статусное сообщение Telegram")
                try:
                    status = await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"Повторная попытка {attempt}/{MAX_ATTEMPTS}…",
                    )
                except Exception:
                    log.exception("Не удалось отправить статусное сообщение Telegram")

        try:
            path = await asyncio.to_thread(generate_and_download, prompt)
            with path.open("rb") as photo:
                await context.bot.send_photo(
                    chat_id=chat.id,
                    photo=photo,
                    caption="Готово · Nano Banana 2.1",
                )
            try:
                await status.delete()
            except Exception:
                log.exception("Не удалось удалить статусное сообщение Telegram")
            return
        except Exception as exc:
            log.exception("Попытка %s завершилась ошибкой", attempt)
            if attempt >= MAX_ATTEMPTS:
                error_text = f"Не удалось получить изображение после {MAX_ATTEMPTS} попыток.\n\nПричина: {str(exc)[:700]}"
                try:
                    await status.edit_text(error_text)
                except Exception:
                    log.exception("Не удалось обновить итоговый статус Telegram")
                    try:
                        await context.bot.send_message(chat_id=chat.id, text=error_text)
                    except Exception:
                        log.exception("Не удалось отправить итоговую ошибку Telegram")
                return

            try:
                await status.edit_text(f"Попытка {attempt} не удалась. Жду 10 секунд и повторяю…")
            except Exception:
                log.exception("Не удалось обновить статусное сообщение Telegram")
                try:
                    status = await context.bot.send_message(
                        chat_id=chat.id,
                        text=f"Попытка {attempt} не удалась. Повторяю через 10 секунд…",
                    )
                except Exception:
                    log.exception("Не удалось отправить статусное сообщение Telegram")
            await asyncio.sleep(10)


def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN.")
    if not ALLOWED_USER_ID and not ALLOWED_CHAT_ID:
        raise RuntimeError("Задай ALLOWED_USER_ID и/или ALLOWED_CHAT_ID.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("img", handle_prompt))
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
