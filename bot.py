import asyncio
import logging
import os
import re
import time
from pathlib import Path
from threading import Lock

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import (
    Application,
    CommandHandler,
    MessageHandler,
    ContextTypes,
    filters,
)
from playwright.sync_api import sync_playwright

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))

BASE_DIR = Path(__file__).resolve().parent
OUTPUT_DIR = BASE_DIR / "output"
OUTPUT_DIR.mkdir(exist_ok=True)

CDP_URL = "http://127.0.0.1:9222"
MAX_ATTEMPTS = 3
GENERATION_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 30

logging.basicConfig(
    format="%(asctime)s %(levelname)s %(message)s",
    level=logging.INFO,
)
log = logging.getLogger("flow-telegram-bot")
browser_lock = Lock()


class FlowError(Exception):
    pass


def get_project_page(browser):
    context = browser.contexts[0]
    pages = context.pages

    page = next(
        (p for p in pages if "/project/" in p.url),
        None,
    )

    if page is None:
        raise FlowError(
            "Не найдена вкладка Google Flow. "
            "Открой Flow в специальном Chrome."
        )

    match = re.search(r"/project/([^/]+)", page.url)
    if not match:
        raise FlowError("Не удалось определить проект Flow.")

    project_id = match.group(1)

    # Если открыт просмотр уже готовой картинки,
    # возвращаемся в основной проект.
    if "/edit/" in page.url:
        back = page.get_by_role(
            "button",
            name='Кнопка "Назад" для возврата к предыдущей странице',
        )
        if back.count():
            back.click()
            try:
                page.wait_for_url(
                    lambda url: "/edit/" not in str(url),
                    timeout=10000,
                )
            except Exception:
                pass

    if "/edit/" in page.url:
        page.goto(
            f"https://flow.google.com/project/{project_id}",
            wait_until="domcontentloaded",
            timeout=60000,
        )

    page.wait_for_timeout(1000)
    return page


def choose_setting(page, label, option):
    trigger = page.get_by_role("button", name="Settings trigger")

    def find_option():
        buttons = page.get_by_role("button")
        for i in range(buttons.count()):
            item = buttons.nth(i)
            try:
                if not item.is_visible():
                    continue
                if item.get_attribute("aria-label") == "Settings trigger":
                    continue
                lines = [
                    line.strip()
                    for line in item.inner_text().splitlines()
                ]
                if option in lines:
                    return item
            except Exception:
                continue
        return None

    target = find_option()
    if target is None:
        trigger.click()
        page.wait_for_timeout(500)
        target = find_option()

    if target is None:
        raise FlowError(f"Flow setting not found: {option}")

    target.click(timeout=10000)
    page.wait_for_timeout(500)



def set_generation_settings(page):
    trigger = page.get_by_role("button", name="Settings trigger")
    current = trigger.inner_text()

    if "Nano Banana 2.1" not in current:
        raise FlowError(
            "? Flow ??????? ?? Nano Banana 2.1. "
            "?????? ?? ??????? ? ?????????? ? ???????."
        )

    choose_setting(page, "Settings trigger", "Image")
    choose_setting(page, "Settings trigger", "9:16")
    choose_setting(page, "Settings trigger", "x1")



def image_snapshots(page):
    return page.locator(
        'img[alt^="Tile displaying"]'
    ).evaluate_all(
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
            if (
                src
                and src not in old_sources
                and image.get("width", 0) > 32
                and image.get("height", 0) > 32
            ):
                return src

        body = page.locator("body").inner_text().lower()

        # Ошибки авторизации/квот не нужно бесконечно повторять.
        if any(
            marker in body
            for marker in (
                "sign in",
                "войдите в аккаунт",
                "insufficient credits",
                "недостаточно кредитов",
            )
        ):
            raise FlowError(
                "Flow сообщает о проблеме с аккаунтом "
                "или доступными кредитами. Проверь страницу в Chrome."
            )

    raise TimeoutError(
        "Новое изображение не появилось за отведённое время."
    )


def generate_and_download(prompt):
    with browser_lock:
        with sync_playwright() as p:
            browser = p.chromium.connect_over_cdp(CDP_URL)
            page = get_project_page(browser)

            # ????????? ??????? ????????? Flow: Nano Banana 2.1, 9:16, x1.

            prompt_box = page.get_by_role(
                "textbox",
                name="What do you want to create?",
            )
            prompt_box.fill(prompt)

            old_sources = {
                item.get("src")
                for item in image_snapshots(page)
                if item.get("src")
            }

            generate_button = page.get_by_role(
                "button",
                name="Start generation",
            )
            generate_button.click()

            new_src = wait_for_new_image(page, old_sources)

            # Открываем карточку нового результата.
            image = page.locator(
                'img[alt^="Tile displaying"]'
            ).filter(has=page.locator("does-not-exist"))

            # Выбираем элемент по точному src нового результата.
            image = page.locator(
                'img[alt^="Tile displaying"]'
            ).evaluate_all(
                """(imgs, target) => {
                    const found = imgs.find(
                        img => (img.currentSrc || img.src) === target
                    );
                    return found ? imgs.indexOf(found) : -1;
                }""",
                new_src,
            )

            if image < 0:
                raise FlowError("Новое изображение найдено, но карточка пропала.")

            page.locator(
                'img[alt^="Tile displaying"]'
            ).nth(image).click()

            page.wait_for_url("**/edit/**", timeout=20000)
            page.wait_for_timeout(500)

            # Кнопка скачивания открывает выбор 1K / 2K / 4K.
            download_button = page.get_by_role(
                "button",
                name="Download",
            )
            download_button.click()
            page.wait_for_timeout(250)

            # Выбираем исходное разрешение 1K.
            with page.expect_download(timeout=DOWNLOAD_TIMEOUT * 1000) as info:
                page.get_by_role(
                    "menuitem",
                    name="1K Original size",
                ).click()

            download = info.value
            filename = Path(download.suggested_filename).name
            if not filename:
                filename = f"flow_{int(time.time())}.jpg"

            destination = OUTPUT_DIR / filename
            download.save_as(str(destination))

            if not destination.exists() or destination.stat().st_size == 0:
                raise FlowError("Flow не сохранил файл.")

            log.info("Изображение сохранено: %s", destination)
            return destination


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    if update.effective_user.id != ALLOWED_USER_ID:
        return

    await update.effective_message.reply_text(
        "Отправь промпт текстом — сгенерирую изображение "
        "в Nano Banana 2.1, 9:16, x1 и скачаю в 1K."
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
        "Принял промпт. Запускаю генерацию в Flow…"
    )

    for attempt in range(1, MAX_ATTEMPTS + 1):
        try:
            if attempt > 1:
                await status.edit_text(
                    f"Повторная попытка {attempt}/{MAX_ATTEMPTS}…"
                )

            # Playwright синхронный, поэтому выполняем вне event loop.
            path = await asyncio.to_thread(
                generate_and_download,
                prompt,
            )

            with path.open("rb") as photo:
                await message.reply_photo(
                    photo=photo,
                    caption="Готово · Nano Banana 2.1 · 1K · 9:16",
                )

            await status.delete()
            return

        except Exception as exc:
            log.exception("Попытка %s завершилась ошибкой", attempt)

            if attempt >= MAX_ATTEMPTS:
                await status.edit_text(
                    "Не удалось получить изображение после "
                    f"{MAX_ATTEMPTS} попыток.\n\n"
                    f"Причина: {str(exc)[:700]}\n\n"
                    "Проверь, что Chrome открыт, Flow авторизован "
                    "и проект доступен."
                )
                return

            await status.edit_text(
                f"Попытка {attempt} не удалась. "
                "Жду 10 секунд и повторяю…"
            )
            await asyncio.sleep(10)


def main():
    if not TOKEN:
        raise RuntimeError(
            "В .env не задан TELEGRAM_BOT_TOKEN."
        )

    if not ALLOWED_USER_ID:
        raise RuntimeError(
            "В .env не задан ALLOWED_USER_ID."
        )

    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(
        MessageHandler(
            filters.TEXT & ~filters.COMMAND,
            handle_prompt,
        )
    )

    log.info("Telegram-бот запущен.")
    app.run_polling()


if __name__ == "__main__":
    main()
