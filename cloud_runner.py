"""Cloud runner for Render: headless Chromium + saved Google Flow session."""
import base64
import hashlib
import hmac
import json
import logging
import os
import struct
import time
from pathlib import Path
from urllib.parse import urlparse

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError

import bot

# Keep request URLs out of logs: Telegram API URLs contain the bot token.
logging.getLogger("httpx").setLevel(logging.WARNING)
logging.getLogger("httpcore").setLevel(logging.WARNING)
logging.getLogger("telegram").setLevel(logging.INFO)

FLOW_URL = os.getenv("FLOW_PROJECT_URL", "").strip()
STATE_B64 = os.getenv("FLOW_STORAGE_STATE_B64", "").strip()
GOOGLE_EMAIL = os.getenv("FLOW_GOOGLE_EMAIL", "").strip()
GOOGLE_PASSWORD = os.getenv("FLOW_GOOGLE_PASSWORD", "")
GOOGLE_TOTP_SECRET = os.getenv("FLOW_GOOGLE_TOTP_SECRET", "").replace(" ", "").strip()

DATA_DIR = Path("/var/data") if Path("/var/data").exists() else Path("/tmp/flow-data")
DATA_DIR.mkdir(parents=True, exist_ok=True)
STATE_PATH = DATA_DIR / "flow-storage-state.json"


def prepare_storage_state():
    if STATE_B64:
        try:
            raw = base64.b64decode(STATE_B64, validate=True)
            parsed = json.loads(raw.decode("utf-8"))
            if not isinstance(parsed, dict) or not isinstance(parsed.get("cookies"), list):
                raise ValueError("Storage state JSON does not look like Playwright storage state")
            STATE_PATH.write_text(json.dumps(parsed), encoding="utf-8")
            logging.info("Google Flow saved session loaded from Render Environment.")
        except Exception as exc:
            raise RuntimeError("FLOW_STORAGE_STATE_B64 задан, но не удалось прочитать Playwright storage state.") from exc
    elif not STATE_PATH.exists():
        logging.warning("FLOW_STORAGE_STATE_B64 is not configured; Flow may require sign-in.")


def totp(secret):
    alphabet = "ABCDEFGHIJKLMNOPQRSTUVWXYZ234567"
    cleaned = "".join(c for c in secret.upper() if c in alphabet)
    bits = "".join(f"{alphabet.index(c):05b}" for c in cleaned)
    key = int(bits[:len(bits) // 8 * 8], 2).to_bytes(len(bits) // 8, "big")
    counter = int(time.time()) // 30
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    code = (struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7fffffff) % 1_000_000
    return f"{code:06d}"


def click_google_next(page):
    candidates = [
        page.get_by_role("button", name="Next"),
        page.get_by_role("button", name="Далее"),
        page.locator("#identifierNext"),
        page.locator("#passwordNext"),
    ]
    for candidate in candidates:
        try:
            if candidate.count() and candidate.first.is_visible():
                candidate.first.click(timeout=5000)
                page.wait_for_timeout(1200)
                return True
        except Exception:
            continue
    return False


def attempt_normal_google_login(page):
    host = urlparse(page.url).netloc.lower()
    if "accounts.google.com" not in host:
        return
    if not GOOGLE_EMAIL or not GOOGLE_PASSWORD:
        raise bot.FlowError(
            "Google Flow не восстановил авторизацию. Для автоматического входа "
            "задай FLOW_GOOGLE_EMAIL и FLOW_GOOGLE_PASSWORD в Environment Worker "
            "или обнови FLOW_STORAGE_STATE_B64 из уже авторизованного Chrome."
        )

    email_input = page.locator('input[type="email"]')
    if email_input.count():
        email_input.first.fill(GOOGLE_EMAIL)
        if not click_google_next(page):
            raise bot.FlowError("Не удалось продолжить стандартный вход Google после ввода email.")

    password_input = page.locator('input[type="password"]')
    if password_input.count():
        password_input.first.fill(GOOGLE_PASSWORD)
        if not click_google_next(page):
            raise bot.FlowError("Не удалось продолжить стандартный вход Google после ввода пароля.")

    page.wait_for_timeout(1500)
    host = urlparse(page.url).netloc.lower()
    if "accounts.google.com" in host and GOOGLE_TOTP_SECRET:
        otp_fields = [
            page.locator('input[name="totpPin"]'),
            page.locator('input[autocomplete="one-time-code"]'),
            page.locator('input[type="tel"]'),
        ]
        for field in otp_fields:
            try:
                if field.count() and field.first.is_visible():
                    field.first.fill(totp(GOOGLE_TOTP_SECRET))
                    if not click_google_next(page):
                        raise bot.FlowError("Не удалось подтвердить одноразовый код Google.")
                    break
            except bot.FlowError:
                raise
            except Exception:
                continue

    page.wait_for_timeout(2500)
    if "accounts.google.com" in urlparse(page.url).netloc.lower():
        raise bot.FlowError(
            "Google запросил дополнительную проверку или другой шаг входа. "
            "Автоматически обходить защиту нельзя; открой Google Flow на своём "
            "компьютере, заверши проверку и заново экспортируй storage state."
        )


def open_flow_page(browser):
    if not FLOW_URL:
        raise bot.FlowError("В Environment Worker не задан FLOW_PROJECT_URL.")
    context = browser.new_context(
        storage_state=str(STATE_PATH) if STATE_PATH.exists() else None,
        accept_downloads=True,
        viewport={"width": 1440, "height": 1000},
    )
    page = context.new_page()
    page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(1800)
    attempt_normal_google_login(page)

    if "/project/" not in page.url:
        # Sometimes Flow redirects to /about before the auth redirect fully settles.
        page.goto(FLOW_URL, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(1800)
        attempt_normal_google_login(page)

    if "/project/" not in page.url:
        # Log only the final host/path and title; never log cookies, query strings,
        # page contents, or authentication values.
        final = urlparse(page.url)
        safe_location = f"{final.scheme}://{final.netloc}{final.path}"
        try:
            page_title = page.title()[:120]
        except Exception:
            page_title = "(title unavailable)"
        logging.error(
            "Flow project redirect diagnostic: location=%s title=%r",
            safe_location,
            page_title,
        )
        raise bot.FlowError(
            "Google Flow не открыл проект. В логах есть безопасная диагностика "
            "конечного адреса и заголовка страницы; проверь доступ аккаунта и "
            "состояние авторизации."
        )

    try:
        context.storage_state(path=str(STATE_PATH))
    except Exception:
        pass
    return context, page


def cloud_generate_and_download(prompt):
    with bot.browser_lock:
        with sync_playwright() as p:
            browser = p.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-dev-shm-usage"],
            )
            context = None
            try:
                context, page = open_flow_page(browser)
                bot.set_generation_settings(page)

                prompt_box = page.get_by_role("textbox", name="What do you want to create?")
                prompt_box.fill(prompt)
                old_sources = {
                    item.get("src")
                    for item in bot.image_snapshots(page)
                    if item.get("src")
                }
                page.get_by_role("button", name="Start generation").click()
                new_src = bot.wait_for_new_image(page, old_sources)

                image_index = page.locator('img[alt^="Tile displaying"]').evaluate_all(
                    """(imgs, target) => {
                        const found = imgs.find(img => (img.currentSrc || img.src) === target);
                        return found ? imgs.indexOf(found) : -1;
                    }""",
                    new_src,
                )
                if image_index < 0:
                    raise bot.FlowError("Новое изображение найдено, но карточка пропала.")

                page.locator('img[alt^="Tile displaying"]').nth(image_index).click()
                page.wait_for_url("**/edit/**", timeout=20000)
                page.get_by_role("button", name="Download").click()
                page.wait_for_timeout(300)

                with page.expect_download(timeout=bot.DOWNLOAD_TIMEOUT * 1000) as info:
                    page.get_by_role("menuitem", name="1K Original size").click()

                download = info.value
                filename = Path(download.suggested_filename).name or f"flow_{int(time.time())}.jpg"
                destination = bot.OUTPUT_DIR / filename
                download.save_as(str(destination))
                if not destination.exists() or destination.stat().st_size == 0:
                    raise bot.FlowError("Flow не сохранил файл.")

                try:
                    context.storage_state(path=str(STATE_PATH))
                except Exception:
                    pass
                bot.log.info("Изображение сохранено: %s", destination)
                return destination
            finally:
                if context is not None:
                    try:
                        context.close()
                    except Exception:
                        pass
                browser.close()


def main():
    prepare_storage_state()
    bot.generate_and_download = cloud_generate_and_download
    bot.main()


if __name__ == "__main__":
    main()
