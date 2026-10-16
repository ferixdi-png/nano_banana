import asyncio
import base64
import json
import logging
import os
import random
import re
import time
from pathlib import Path
from threading import Event, Lock, Thread

from dotenv import load_dotenv
from telegram import Update
from telegram.ext import Application, CommandHandler, MessageHandler, ContextTypes, filters
from playwright.sync_api import sync_playwright

load_dotenv()

TOKEN = os.getenv("TELEGRAM_BOT_TOKEN", "").strip()
ALLOWED_USER_ID = int(os.getenv("ALLOWED_USER_ID", "0"))
ALLOWED_CHAT_ID = int(os.getenv("ALLOWED_CHAT_ID", "0"))
CDP_URL = os.getenv("CDP_URL", "http://127.0.0.1:9222" if os.name == "nt" else "").strip()  # Local Windows default; leave unset on Render
BROWSER_CHANNEL = os.getenv("FLOW_BROWSER_CHANNEL", "chrome").strip().lower()  # Use branded Google Chrome on Render; set to chromium to roll back.
FLOW_PROJECT_URL = os.getenv("FLOW_PROJECT_URL", "").strip()
PROFILE_DIR = Path(os.getenv("FLOW_PROFILE_DIR", "/var/data/flow-profile"))
STATE_PATH = Path(os.getenv("FLOW_STORAGE_STATE_PATH", "/var/data/flow-storage-state.json"))
OUTPUT_DIR = Path(os.getenv("OUTPUT_DIR", "/tmp/flow-output"))
OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
MAX_ATTEMPTS = 3
MAX_PENDING_GENERATIONS = 25
USER_REQUEST_COOLDOWN_SECONDS = 15
GENERATION_TIMEOUT = 180
DOWNLOAD_TIMEOUT = 30

logging.basicConfig(format="%(asctime)s %(levelname)s %(message)s", level=logging.INFO)
log = logging.getLogger("flow-telegram-bot")
browser_lock = Lock()
generation_queue_lock = asyncio.Lock()
pending_generations = 0
last_user_request_at = {}
last_busy_notice_at = {}

GENERATION_EXCUSES = [
    "Ну вот, а кадр уже почти позировал. Попробуем чуть позже.",
    "Сегодня Flow решил побыть загадочным. Твою идею не потерял.",
    "Пиксельная кухня задымилась. Дам ей минутку прийти в себя.",
    "Не вышло с первого захода. Даже у хороших историй бывает черновик.",
    "Кадр пока не родился. Задумка остаётся, вернёмся к ней позже.",
    "Похоже, генератор ушёл подумать о композиции. Слишком глубоко ушёл.",
    "Тут техническая заминка, не твой промпт виноват. Попробуем позже.",
    "Я за картинку боролся, но сегодня техника решила поспорить.",
    "Пока без шедевра. Зато идея у нас никуда не делась.",
    "Генератор капризничает. В такие моменты главное — не ругаться с пикселями.",
    "Сейчас не получилось. Не буду выдавать пустоту за искусство.",
    "Картинка задерживается за кулисами. Видимо, поправляет свет.",
    "Техническая пауза. Твой запрос принят, но Flow не отдал результат.",
    "Не тот дубль. Режиссёрская группа просит ещё немного времени.",
    "На этот раз не сложилось. Вернёмся к кадру, когда Flow успокоится.",
    "Пока только закулисье, без премьеры. Попробуй немного позже.",
    "Техника подвела в самый творческий момент. Бывает даже у цифровых.",
    "Результат не пришёл, поэтому не стану делать вид, что всё готово.",
    "Пиксели взяли паузу без согласования со мной. Неловко вышло.",
    "Не получилось вытащить кадр. Идея хорошая, просто сервис заупрямился.",
] 

BUSY_RESPONSES = [
    "Вижу, ты разошёлся. Дай текущему кадру закончиться — и возьмусь за следующий.",
    "Запрос поймал, но очередь уже шуршит. Не потеряю, просто подожди немного.",
    "Сейчас занят твоим предшественником по очереди. Следом будет твой выход.",
    "По одному кадру за раз — так меньше цифровых драм. Скоро очередь дойдёт.",
    "Я не пропал, просто сейчас в процессе. Следующий запрос тоже дождётся своей сцены.",
    "Тут небольшая пиксельная пробка. Как рассосётся, вернусь к твоей задумке.",
    "Увидел повтор, всё нормально. Не нужно отправлять ещё раз — запрос уже в работе.",
    "Сейчас занят. Пусть генерация спокойно закончит свой творческий монолог.",
    "Очередь плотная, но твой запрос не потеряется. Нужно немного подождать.",
    "Секунду терпения: сначала доведу текущий кадр, потом возьмусь за новый.",
]

GENERATION_ACKNOWLEDGEMENTS = [
    "Так, задумку поймал. Сейчас посмотрим, как она будет выглядеть в кадре.",
    "Принято. Устраивайся поудобнее — отправляю идею в работу.",
    "Есть. Сейчас соберу твой запрос в изображение, без лишней суеты.",
    "О, тут можно сделать красиво. Запускаю генерацию.",
    "Запрос на месте. Сейчас проверим, что получится из этой задумки.",
    "Понял тебя. Пиксели, свет и композиция — за мной.",
    "Начинаю. Если всё пойдёт гладко, скоро будет что рассматривать.",
    "Идея принята. Сейчас превращу описание в кадр.",
    "Взял в работу. Пусть слова немного побудут изображением.",
    "Запускаю. Надеюсь, этот кадр попадёт прямо в настроение.",
    "Всё, пошёл процесс. Твоя задумка уже на пути к визуальной версии.",
    "Принял запрос. Сейчас посмотрим, что скажет ему генератор.",
    "Так, работаем. Дай немного времени — изображение собирается.",
    "Поймал идею и отправил в генерацию. Ждём красивого результата.",
    "Поехали. Сейчас у твоего описания появится визуальная форма.",
]

class FlowError(Exception):
    pass


MASTER_PROMPT = """MASTER PROMPT — AUTHENTIC PHOTOGRAPHIC REALISM, UNIQUE CHARACTERS & NATURAL CAMERA PERSPECTIVE

Create the image described in USER PROMPT below. The user's description is the primary source of truth for subject, identity, appearance, clothing, environment, actions, objects, and scene. Do not arbitrarily change, omit, or contradict those details.

USER PROMPT:
[USER_PROMPT]

Make the result look like a genuine photograph captured spontaneously in a real environment by an ordinary person's smartphone. Prioritize authenticity, believable physical details, natural visual storytelling, and a thoughtfully chosen but physically plausible camera perspective over perfection, spectacle, or conventional beauty.

AUTHENTIC EVERYDAY PHOTOGRAPHY
The image should feel like a real moment from someone's camera roll, not a staged photoshoot, commercial stock photo, beauty campaign, fashion editorial, or glossy AI image. Use believable light, exposure, color response, depth, perspective, and natural variations in sharpness. Do not simulate realism by making the image ugly, blurry, noisy, damaged, or poorly exposed.

DISTINCTIVE, BELIEVABLE PEOPLE
Give newly generated people individually recognizable, internally coherent features: natural facial proportions, subtle asymmetry, distinctive eyes and eyebrows, believable nose and lips, realistic skin tone and texture, individual hairline and strand patterns, and natural expressions and posture. Avoid generic repeated influencer faces, perfect symmetry, plastic skin, and automatic glamour. Do not exaggerate features or invent random blemishes. If the user provides a reference image or specifies a person's appearance, preserve that identity and those characteristics; do not redesign an established person. If several people appear, make their faces and expressions distinct.

NATURAL CAMERA PERSPECTIVE
Choose an interesting but believable viewpoint suited to the scene instead of defaulting to a centered, straight-on portrait. Consider an off-center standing view, candid three-quarter angle, subtle high or low angle, doorway perspective, side view, foreground depth, wider environmental framing, or natural over-the-shoulder view when appropriate. Vary the choice naturally. Keep perspective physically plausible, body proportions natural, and lens behavior realistic. Avoid forced extreme angles, fisheye distortion, dramatic tilts, and artificial camera tricks unless explicitly requested.

UNSTAGED COMPOSITION
Make the scene feel observed rather than arranged. Use natural asymmetry, relaxed body language, plausible spacing, and a believable relationship between subject and environment. The subject need not be centered or look at the lens unless requested. Preserve the user's requested pose, action, expression, gaze, and body position. Hands, shoulders, posture, and body weight should behave naturally. Do not add contradictory gestures to manufacture a candid look.

REALISTIC SMARTPHONE OPTICS
Simulate a contemporary smartphone photograph under the described conditions. Use believable field of view, facial proportions, foreground/background relationships, coherent focus, restrained contrast, natural tonal transitions, and plausible depth of field. Avoid excessive HDR, oversharpening, artificial portrait blur, halos, aggressive clarity, exaggerated bokeh, or synthetic hyper-detail. Do not show a phone, camera, photographer, or equipment unless requested.

SKIN, HAIR, CLOTHING, MATERIALS
Render skin with age- and lighting-appropriate texture, subtle tonal variation, natural highlights, and realistic shadows—not waxy smoothness or exaggerated pores. Hair should have believable volume, strands, clumping, hairline, gravity, and movement. Clothing should show plausible fabric weight, seams, folds, wrinkles, tension, and fit. Materials such as cotton, denim, wool, metal, glass, and wood should respond to light differently and realistically. Add no random dirt, stains, scratches, or blemishes unless they fit the scene.

NATURAL LIGHT AND COLOR
Use lighting that belongs in the described location: window light, ordinary indoor illumination, daylight, open shade, sunlight, or street lighting as appropriate. Keep shadow direction, softness, reflections, skin tones, white balance, and color response coherent. Avoid automatic golden-hour light, dramatic rim lighting, studio fill, cinematic teal-orange grading, excessive saturation, or a uniform cinematic palette unless requested.

REAL ENVIRONMENTS AND PHYSICAL CONSISTENCY
Every visible element must belong naturally in the scene. Maintain coherent geometry, dimensions, perspective, contact, occlusion, reflections, shadows, and depth. Avoid floating or duplicated objects, warped furniture, impossible architecture, melting textures, inconsistent reflections, and unnecessary decorative clutter. Do not fill empty spaces with invented objects just to add detail.

ARTIFACT CONTROL
Keep anatomy and objects clean and coherent. Avoid extra or missing fingers, fused hands, malformed limbs, incorrect joints, duplicated facial features, distorted eyes or teeth, broken hairlines, repeated patterns, ghosting, accidental duplication, melted textures, unnatural sharpening, and physically impossible lighting or reflections. Natural imperfections are welcome only when plausible; never manufacture defects as a substitute for authenticity.

PRESERVE CREATIVE INTENT
The user's prompt determines what the image depicts. Preserve requested identities, age characteristics, clothing, accessories, poses, gestures, expressions, activities, environments, and important objects. Do not add unrequested people, props, text, logos, watermarks, or dramatic effects. Where the user leaves creative freedom, use it for distinctive believable details, natural composition, an interesting plausible angle, and authentic environmental context. Respect reference images and all explicit instructions.

FINAL PRIORITIES
1. Faithfulness to the user prompt and references.
2. Convincing real-world photographic appearance.
3. Distinctive, believable individual features.
4. A naturally interesting, physically plausible camera perspective.
5. Coherent anatomy, materials, lighting, and environment.
6. Unstaged composition.
7. Clean image quality without distracting artifacts.

FINAL OBJECTIVE: Create a distinctive, believable photograph that feels like a real moment captured spontaneously by an ordinary person using a smartphone. It should be interesting without looking deliberately artistic, imperfect without looking damaged, detailed without looking synthetic, and natural without becoming generic."""



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



def google_totp_code(secret):
    """Generate a current TOTP code from the secret configured in Render."""
    import hashlib
    import hmac
    import struct
    import base64 as _base64

    normalized = re.sub(r"[^A-Z2-7]", "", (secret or "").upper())
    if not normalized:
        raise FlowError("FLOW_GOOGLE_TOTP_SECRET задан, но не содержит корректный Base32-секрет.")
    try:
        key = _base64.b32decode(normalized + "=" * ((8 - len(normalized) % 8) % 8))
    except Exception as exc:
        raise FlowError("FLOW_GOOGLE_TOTP_SECRET не распознан как Base32-секрет.") from exc
    counter = int(time.time() // 30)
    digest = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    offset = digest[-1] & 0x0F
    number = struct.unpack(">I", digest[offset:offset + 4])[0] & 0x7FFFFFFF
    return str(number % 1_000_000).zfill(6)


def login_google_if_configured(context):
    """Use Render-stored credentials for a normal Google sign-in; never bypass security challenges."""
    email = os.getenv("FLOW_GOOGLE_EMAIL", "").strip()
    password = os.getenv("FLOW_GOOGLE_PASSWORD", "")
    totp_secret = os.getenv("FLOW_GOOGLE_TOTP_SECRET", "").strip()
    if not (email and password and totp_secret):
        raise FlowError(
            "Не найдена сессия Google Flow. Для прямого входа добавь FLOW_GOOGLE_EMAIL, "
            "FLOW_GOOGLE_PASSWORD и FLOW_GOOGLE_TOTP_SECRET в Render Environment."
        )

    page = context.pages[0] if context.pages else context.new_page()
    target_match = re.search(r"/project/([^/?#]+)", FLOW_PROJECT_URL)
    target_url = f"https://flow.google.com/project/{target_match.group(1)}" if target_match else "https://flow.google.com/"
    page.goto(
        "https://accounts.google.com/signin/v2/identifier"
        "?service=accountsettings&continue=https%3A%2F%2Fflow.google.com%2F",
        wait_until="domcontentloaded",
        timeout=60000,
    )
    page.wait_for_timeout(1200)

    try:
        email_box = page.locator('input[type="email"], input[name="identifier"]').first
        if email_box.is_visible(timeout=5000):
            email_box.fill(email)
            page.get_by_role("button", name=re.compile(r"Next|Далее", re.I)).click(timeout=10000)
            page.wait_for_timeout(1200)
    except Exception:
        log.info("Google email step was not shown; continuing with current sign-in state.")

    try:
        password_box = page.locator('input[type="password"]').first
        if password_box.is_visible(timeout=5000):
            password_box.fill(password)
            page.get_by_role("button", name=re.compile(r"Next|Далее", re.I)).click(timeout=10000)
            page.wait_for_timeout(1500)
    except Exception:
        log.info("Google password step was not shown; continuing with current sign-in state.")

    try:
        body = page.locator("body").inner_text().lower()
    except Exception:
        body = ""
    if any(term in body for term in (
        "confirm it's you", "подтвердите, что это вы",
        "suspicious activity", "подозрительная активность",
        "captcha", "робот",
    )):
        raise FlowError(
            "Google остановил автоматический вход дополнительной проверкой. "
            "Автоматически обходить такую проверку нельзя; войди в аккаунт вручную и обнови сессию."
        )

    try:
        code_box = page.locator(
            'input[name="totpPin"], input[autocomplete="one-time-code"], '
            'input[name="code"], input[type="tel"]'
        ).first
        if code_box.is_visible(timeout=4000):
            code_box.fill(google_totp_code(totp_secret))
            page.get_by_role("button", name=re.compile(r"Next|Далее|Verify|Подтвердить", re.I)).click(timeout=10000)
            page.wait_for_timeout(1800)
    except FlowError:
        raise
    except Exception:
        log.info("Google authenticator-code step was not detected automatically.")

    try:
        body = page.locator("body").inner_text().lower()
    except Exception:
        body = ""
    if any(term in body for term in (
        "choose how you want to sign in", "выберите способ входа",
        "2-step verification", "двухэтапная аутентификация",
    )):
        try:
            option = page.get_by_text(re.compile(r"Authenticator|Аутентификатор|код подтверждения", re.I))
            if option.count() and option.first.is_visible():
                option.first.click(timeout=5000)
                page.wait_for_timeout(1000)
                code_box = page.locator(
                    'input[name="totpPin"], input[autocomplete="one-time-code"], '
                    'input[name="code"], input[type="tel"]'
                ).first
                code_box.wait_for(state="visible", timeout=5000)
                code_box.fill(google_totp_code(totp_secret))
                page.get_by_role("button", name=re.compile(r"Next|Далее|Verify|Подтвердить", re.I)).click(timeout=10000)
                page.wait_for_timeout(1800)
        except Exception:
            log.info("Could not automatically select the authenticator option.")

    page.goto(target_url, wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(3500)
    if target_match and "/project/" not in page.url:
        try:
            tail = page.locator("body").inner_text()[-1200:]
        except Exception:
            tail = ""
        raise FlowError(
            "Не удалось завершить вход в Google Flow: проект всё ещё не открылся. "
            f"Текущий URL: {page.url}. Текст страницы: {tail}"
        )
    persist_session(context)
    log.info("Google sign-in flow finished; browser session saved.")
    return page

def create_new_project(context):
    """Open Flow home and create a fresh project for this single image generation."""
    page = next(
        (p for p in context.pages if ("labs.google" in p.url and "/fx/tools/flow" in p.url) or "flow.google.com" in p.url),
        context.pages[0] if context.pages else context.new_page(),
    )
    page.goto("https://labs.google/fx/tools/flow", wait_until="domcontentloaded", timeout=60000)
    page.wait_for_timeout(2500)

    current_url = page.url
    if "/project/" in current_url:
        # Flow may redirect directly to the last project; return to its home screen.
        page.goto("https://labs.google/fx/tools/flow", wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)
    body_text = ""
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        pass
    body_lower = body_text.lower()
    if any(marker in body_lower for marker in (
        "мы заметили подозрительную активность",
        "suspicious activity",
    )):
        raise FlowError(
            "Flow показывает предупреждение о подозрительной активности уже на главной странице. "
            "Остановили запуск и не создаём новые проекты автоматически."
        )
    if any(marker in body_lower for marker in (
        "войдите в аккаунт",
        "нужно войти в аккаунт",
        "sign in",
    )):
        raise FlowError(
            "Flow требует авторизацию перед созданием проекта. "
            "Проверь авторизованную сессию. Текст страницы: " + body_text[-1200:]
        )

    project_url_before = current_url if "/project/" in current_url else ""
    create_pattern = re.compile(
        r"(new project|create project|start a project|создать проект|новый проект|создать новый проект)",
        re.IGNORECASE,
    )
    clicked = False

    # Flow's current UI exposes the action as button.new-project-button.
    # Disable Playwright's implicit navigation wait: the click can succeed while
    # Flow keeps the browser on an SPA transition for longer than the click timeout.
    try:
        specific_button = page.locator("button.new-project-button")
        if specific_button.count() and specific_button.first.is_visible() and specific_button.first.is_enabled():
            clicked = True
            specific_button.first.click(timeout=10000, no_wait_after=True)
    except Exception:
        log.exception("Could not click Flow's new-project button")

    # Fallback for UI variants where the CSS class changes. Set clicked before
    # invoking click because Flow may process the click even if its transition stalls.
    if not clicked:
        try:
            buttons = page.get_by_role("button", name=create_pattern)
            for i in range(min(buttons.count(), 10)):
                button = buttons.nth(i)
                if button.is_visible() and button.is_enabled():
                    clicked = True
                    button.click(timeout=10000, no_wait_after=True)
                    break
        except Exception:
            log.exception("Could not click a labelled Flow new-project button")

    # Some Flow layouts render the project tile as a non-button element.
    if not clicked:
        try:
            candidates = page.get_by_text(create_pattern)
            for i in range(min(candidates.count(), 12)):
                item = candidates.nth(i)
                if item.is_visible() and item.is_enabled():
                    clicked = True
                    item.click(timeout=10000, no_wait_after=True)
                    break
        except Exception:
            log.exception("Could not click a labelled Flow new-project tile")

    if not clicked:
        raise FlowError(
            "Не нашёл на главной странице Flow элемент создания нового проекта. "
            "Автоматическую генерацию остановил, чтобы случайно не запустить генерацию в старом проекте. "
            f"URL: {page.url}. Текст страницы: {body_text[-1800:]}"
        )

    try:
        page.wait_for_url(re.compile(r".*/project/[^/]+.*"), timeout=30000)
    except Exception as exc:
        try:
            page.wait_for_timeout(3000)
        except Exception:
            pass
        if "/project/" not in page.url:
            try:
                tail = page.locator("body").inner_text()[-1800:]
            except Exception:
                tail = "(не удалось прочитать страницу)"
            raise FlowError(
                "Flow не открыл новый проект после нажатия кнопки создания. "
                f"URL: {page.url}. Текст страницы: {tail}"
            ) from exc

    page.wait_for_timeout(1800)
    match = re.search(r"/project/([^/?]+)", page.url)
    if not match:
        raise FlowError(f"Не удалось определить URL нового проекта Flow: {page.url}")
    new_project_id = match.group(1)
    previous_match = re.search(r"/project/([^/?]+)", FLOW_PROJECT_URL)
    if previous_match and new_project_id == previous_match.group(1):
        raise FlowError(
            "Flow открыл прежний проект вместо нового. Генерацию отменили, чтобы не использовать старый проект."
        )

    log.info("Created fresh Flow project for this generation: %s", page.url)
    return page


def get_project_page(context):
    """Open the exact configured project on Flow's migrated canonical domain."""
    pages = context.pages
    target_match = re.search(r"/project/([^/?#]+)", FLOW_PROJECT_URL) if FLOW_PROJECT_URL else None

    if target_match:
        target_id = target_match.group(1)
    else:
        # A restored tab may already point to a project. Reuse it, but normalize
        # the URL to the current Flow origin instead of navigating through Labs.
        existing = next((p for p in pages if re.search(r"/project/([^/?#]+)", p.url)), None)
        if existing:
            target_match = re.search(r"/project/([^/?#]+)", existing.url)
            target_id = target_match.group(1) if target_match else ""
        else:
            raise FlowError(
                "Не задан FLOW_PROJECT_URL с адресом существующего проекта. "
                "Укажи полную ссылку на нужный проект в Render Environment."
            )

    canonical_url = f"https://flow.google.com/project/{target_id}"
    page = next(
        (p for p in pages if re.search(r"/project/([^/?#]+)", p.url)
         and re.search(r"/project/([^/?#]+)", p.url).group(1) == target_id),
        pages[0] if pages else context.new_page(),
    )

    # Old Labs URLs now redirect for migrated accounts. Navigate directly to the
    # canonical project route to avoid being dumped on the marketing /about page.
    if page.url.rstrip("/") != canonical_url:
        log.info("Opening configured Flow project directly: %s", canonical_url)
        page.goto(canonical_url, wait_until="domcontentloaded", timeout=60000)

    page.wait_for_timeout(4500)
    current_match = re.search(r"/project/([^/?#]+)", page.url)
    if not current_match or current_match.group(1) != target_id:
        try:
            page_title = page.title()
        except Exception:
            page_title = "(title unavailable)"
        try:
            body_tail = page.locator("body").inner_text()[-900:]
        except Exception:
            body_tail = "(page text unavailable)"
        raise FlowError(
            "Google Flow не открыл проект по прямой ссылке и перенаправил браузер. "
            f"Ожидался: {canonical_url}. Текущий URL: {page.url}. "
            f"Заголовок: {page_title}. Текст страницы: {body_tail}"
        )

    body_text = ""
    try:
        body_text = page.locator("body").inner_text()
    except Exception:
        pass
    if any(marker in body_text.lower() for marker in (
        "мы заметили подозрительную активность",
        "suspicious activity",
    )):
        raise FlowError(
            "Google Flow показал предупреждение о подозрительной активности. "
            "Автоматические повторы остановлены; проверь доступ к Flow в обычном браузере."
        )

    # The migrated frontend can take several seconds to mount its composer.
    try:
        page.get_by_role("textbox", name=re.compile(r"Что вы хотите создать|Describe|prompt", re.I)).wait_for(
            state="visible", timeout=15000
        )
    except Exception:
        # Return the project page so the caller can report the actual missing UI
        # instead of silently switching projects or opening the marketing page.
        log.warning("Project URL loaded but the Flow prompt composer was not detected. URL=%s", page.url)
    if "/edit/" in page.url:
        back = page.get_by_role("button", name='Кнопка "Назад" для возврата к предыдущей странице')
        if back.count():
            back.click()
            try:
                page.wait_for_url(lambda url: "/edit/" not in str(url), timeout=10000)
            except Exception:
                pass
    if "/edit/" in page.url:
        page.goto(canonical_url, wait_until="domcontentloaded", timeout=60000)
        page.wait_for_timeout(2500)

    log.info("Using configured Flow project: %s", page.url)
    return page


def choose_setting(page, option):
    """Open Flow's model/settings panel and choose a visible option."""
    trigger = page.get_by_role("button", name="Триггер настроек")
    candidates = [option]
    if option == "9:16":
        candidates.extend(["crop_9_16", "Portrait", "Портрет"])
    elif option == "x1":
        candidates.extend(["x1", "x 1", "1 image", "1 изображение"])

    def click_visible_candidate():
        for candidate in candidates:
            exact = page.get_by_text(candidate, exact=True)
            for locator in (exact, page.get_by_text(re.compile(re.escape(candidate), re.IGNORECASE))):
                try:
                    count = locator.count()
                    for index in range(min(count, 8)):
                        item = locator.nth(index)
                        if item.is_visible():
                            item.click(timeout=3000)
                            page.wait_for_timeout(500)
                            return True
                except Exception:
                    continue
        return False
    # The model chip itself opens Flow's generation-parameter panel.
    # Prefer clicking the visible Nano Banana label, rather than blindly
    # toggling the whole settings trigger (which can close an already-open panel).
    model_label = page.get_by_text(re.compile(r"Nano Banana\\s*2\\.1", re.IGNORECASE))
    try:
        visible_model = next(
            (model_label.nth(i) for i in range(min(model_label.count(), 8))
             if model_label.nth(i).is_visible()),
            None,
        )
        if visible_model is not None:
            visible_model.click(timeout=5000)
            page.wait_for_timeout(600)
    except Exception:
        log.exception("Could not open Flow settings by clicking the Nano Banana model label")

    if click_visible_candidate():
        return

    # Fallback: the settings trigger can open the same panel in other UI versions.
    try:
        trigger.click(timeout=5000)
        page.wait_for_timeout(600)
    except Exception:
        pass

    if click_visible_candidate():
        return

    # Some layouts expose the current x2 chip as the control that opens
    # the output-count menu. Click it and then select x1 from the menu.
    if option == "x1":
        current_count = page.get_by_text("x2", exact=True)
        try:
            for index in range(min(current_count.count(), 8)):
                item = current_count.nth(index)
                if item.is_visible():
                    item.click(timeout=5000)
                    page.wait_for_timeout(600)
                    if click_visible_candidate():
                        return
        except Exception:
            log.exception("Could not open Flow output-count selector from x2")

    try:
        menu_text = page.locator("body").inner_text()[-2500:]
    except Exception:
        menu_text = "(не удалось прочитать текст страницы)"
    raise FlowError(
        f"Не удалось выбрать параметр «{option}» в настройках Flow. "
        f"Видимый текст страницы: {menu_text}"
    )


def set_generation_settings(page):
    """Set and verify Nano Banana 2.1, portrait 9:16, and exactly one output."""
    trigger = page.get_by_role("button", name="Триггер настроек")
    trigger_text = trigger.inner_text()
    log.info("Flow settings before changes: %r", trigger_text)
    if "Nano Banana 2.1" not in trigger_text:
        raise FlowError(
            "Не удалось подтвердить Nano Banana 2.1 в Flow. "
            f"Текущие настройки: {trigger_text!r}"
        )

    # Open the model/settings panel once, then choose the aspect ratio and count.
    if "9:16" not in trigger_text and "crop_9_16" not in trigger_text:
        choose_setting(page, "9:16")

    trigger_text = trigger.inner_text()
    if not re.search(r"(?m)^x1$", trigger_text) and not re.search(r"(?m)^1$", trigger_text):
        choose_setting(page, "x1")

    verified_text = trigger.inner_text()
    log.info("Flow settings after changes: %r", verified_text)
    if "Nano Banana 2.1" not in verified_text:
        raise FlowError(f"Модель Nano Banana 2.1 не подтверждена перед генерацией: {verified_text!r}")
    if not ("9:16" in verified_text or "crop_9_16" in verified_text):
        raise FlowError(f"Не удалось подтвердить вертикальный формат 9:16 перед генерацией: {verified_text!r}")
    if not re.search(r"(?m)^x1$", verified_text) and not re.search(r"(?m)^1$", verified_text):
        raise FlowError(
            "Flow не подтвердил генерацию ровно одного изображения; запуск отменён. "
            f"Текущие настройки: {verified_text!r}"
        )
    log.info("Verified Flow settings: Nano Banana 2.1, portrait 9:16, x1.")


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
        # Detect a real Flow rejection immediately instead of waiting for the full timeout.
        if (
            "мы заметили подозрительную активность" in body
            or "suspicious activity" in body
        ):
            raise FlowError(
                "Flow отклонил генерацию из-за подозрительной активности. "
                "Остановили ожидание, чтобы не усугублять блокировку."
            )
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

def log_container_memory():
    """Best-effort RSS snapshot for diagnosing Render restarts/OOM kills."""
    self_rss_kb = 0
    total_rss_kb = 0
    browser_rss_kb = 0
    browser_processes = 0
    try:
        with open("/proc/self/status", "r", encoding="utf-8") as status_file:
            for line in status_file:
                if line.startswith("VmRSS:"):
                    self_rss_kb = int(line.split()[1])
                    break
        for entry in os.listdir("/proc"):
            if not entry.isdigit():
                continue
            try:
                base = f"/proc/{entry}"
                with open(f"{base}/status", "r", encoding="utf-8") as status_file:
                    status = status_file.read()
                rss_line = next((line for line in status.splitlines() if line.startswith("VmRSS:")), None)
                if not rss_line:
                    continue
                rss_kb = int(rss_line.split()[1])
                total_rss_kb += rss_kb
                with open(f"{base}/cmdline", "rb") as cmd_file:
                    command = cmd_file.read().replace(b"\\x00", b" ").lower()
                if b"chrome" in command or b"chromium" in command:
                    browser_rss_kb += rss_kb
                    browser_processes += 1
            except (OSError, ValueError, StopIteration):
                continue
        log.info(
            "Memory snapshot: self_rss_mb=%.1f container_process_rss_sum_mb=%.1f "
            "browser_processes=%s browser_rss_sum_mb=%.1f",
            self_rss_kb / 1024,
            total_rss_kb / 1024,
            browser_processes,
            browser_rss_kb / 1024,
        )
    except Exception:
        log.exception("Could not collect process memory diagnostics")


def memory_watchdog(stop_event):
    """Emit periodic memory diagnostics while the browser is running."""
    while not stop_event.wait(8):
        log_container_memory()


def generate_and_download(prompt):
    with browser_lock:
        prepare_storage_state()
        with sync_playwright() as p:
            browser = None
            context = None
            close_browser = False
            memory_stop = Event()
            memory_thread = Thread(target=memory_watchdog, args=(memory_stop,), daemon=True)
            try:
                memory_thread.start()
                log_container_memory()
                if CDP_URL:
                    browser = p.chromium.connect_over_cdp(CDP_URL)
                    if not browser.contexts:
                        raise FlowError("В подключённом Chrome нет браузерного контекста.")
                    context = browser.contexts[0]
                else:
                    launch_options = {
                        "headless": True,
                        "args": [
                            "--no-sandbox",
                            "--disable-dev-shm-usage",
                            "--disable-gpu",
                            "--disable-extensions",
                            "--disable-background-networking",
                            "--disable-default-apps",
                            "--no-first-run",
                            "--no-default-browser-check",
                            "--disable-sync",
                        ],
                    }
                    if BROWSER_CHANNEL == "chrome":
                        browser = p.chromium.launch(channel="chrome", **launch_options)
                    else:
                        browser = p.chromium.launch(**launch_options)
                    log.info("Launching Playwright browser channel: %s", BROWSER_CHANNEL)
                    close_browser = True
                    if STATE_PATH.exists():
                        context = browser.new_context(storage_state=str(STATE_PATH), accept_downloads=True)
                    else:
                        context = browser.new_context(accept_downloads=True)

                # Sign in with credentials stored in Render Environment. No security challenge is bypassed.
                login_google_if_configured(context)

                # Reuse the explicitly configured Flow project when provided.
                # Create a fresh project only when FLOW_PROJECT_URL is not configured.
                if FLOW_PROJECT_URL:
                    page = get_project_page(context)
                    log.info("Using configured Flow project: %s", page.url)
                else:
                    page = create_new_project(context)
                set_generation_settings(page)
                prompt_box = page.get_by_role("textbox", name="Что вы хотите создать?")
                combined_prompt = MASTER_PROMPT.replace("[USER_PROMPT]", prompt)
                prompt_box.fill(combined_prompt)
                log.info(
                    "Combined user prompt with photographic realism master prompt (total chars=%s).",
                    len(combined_prompt),
                )
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
                memory_stop.set()
                memory_thread.join(timeout=1)
                log_container_memory()
                if context is not None and not CDP_URL:
                    persist_session(context)
                    context.close()
                if browser is not None and close_browser:
                    browser.close()


def is_allowed(update: Update) -> bool:
    """Allow all members in the creators group and the configured admin in private chat."""
    chat = update.effective_chat
    message = update.effective_message
    user = update.effective_user
    if not chat or not message or not user:
        return False

    # Everyone in the configured creators group can generate.
    if chat.type in ("group", "supergroup"):
        return bool(ALLOWED_CHAT_ID and chat.id == ALLOWED_CHAT_ID)

    # Only the configured admin can use private chat for testing.
    if chat.type == "private":
        return bool(ALLOWED_USER_ID and user.id == ALLOWED_USER_ID)

    return False


CREATORS_CHAT_MESSAGE = (
    "В личке я пока на паузе — генерации доступны в чате креаторов. "
    "Загляни туда, отправь описание картинки, и я возьмусь за кадр."
)


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if is_allowed(update):
        if chat.type == "private":
            await message.reply_text(
                "Тестовая мастерская открыта. Напиши /img и опиши кадр — проверим, что получится. "
                "Для остальных генерация доступна в чате креаторов."
            )
        else:
            await message.reply_text(
                "Я здесь. Опиши, что хочешь увидеть, или начни с /img — превращу задумку в кадр. "
                "Генерировать могут все участники этого чата."
            )
    elif chat.type == "private":
        await message.reply_text(CREATORS_CHAT_MESSAGE)


async def handle_prompt(update: Update, context: ContextTypes.DEFAULT_TYPE):
    message = update.effective_message
    chat = update.effective_chat
    if not message or not chat:
        return
    if not is_allowed(update):
        # Never run image generation in a private chat. Explain the restriction
        # for explicit generation requests; /start always displays this notice.
        prompt_text = (message.text or "").strip()
        if chat.type == "private" and (
            prompt_text.lower().startswith("/img")
            or re.search(
                r"\b(сгенерируй\w*|сгенерировать|сгенерить|сгенери|сген|"
                r"нарисуй\w*|нарисовать|создай\w*|создать|изобрази\w*|"
                r"сфотографируй|сфоткай|изображени\w*|картинк\w*|фото\w*)\b",
                prompt_text,
                flags=re.IGNORECASE,
            )
        ):
            await message.reply_text(CREATORS_CHAT_MESSAGE)
        return

    prompt = (message.text or "").strip()
    if not prompt:
        return

    command_prompt = re.match(r"^/img(?:@\w+)?\s*(.*)$", prompt, flags=re.IGNORECASE | re.DOTALL)
    if command_prompt:
        prompt = command_prompt.group(1).strip()
    else:
        # Generation starts only for an explicit image request, in both private and group chats.
        action = re.search(
            r"\b(сгенерируй\w*|сгенерировать|сгенерить|сгенери|сген|генерируй\w*|"
            r"нарисуй\w*|нарисовать|создай\w*|создать|сделай\w*|сделать|"
            r"покажи\w*|пришли\w*|дай|дайте|выдай\w*|изобрази\w*|"
            r"сфотографируй|сфоткай|сделай фото|хочу фото|нужно фото|"
            r"мне фото|можно фото|нужна картинка|нужно изображение)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        image_word = re.search(
            r"\b(изображени\w*|картинк\w*|картин\w*|фото\w*|фотк\w*|"
            r"фотографи\w*|рисунок\w*|иллюстраци\w*|арт\w*|портрет\w*|"
            r"аватар\w*|обложк\w*|снимок\w*|image\w*|picture\w*|photo\w*|portrait\w*)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        direct_visual = re.search(
            r"\b(сгенерируй\w*|сгенерить|сгенери|сген|нарисуй\w*|нарисовать|"
            r"создай\w*|создать|изобрази\w*|сфотографируй|сфоткай)\b",
            prompt,
            flags=re.IGNORECASE,
        )
        if not ((action and image_word) or direct_visual):
            return

        # Strip the request phrase and keep the scene description for the model.
        prompt = re.sub(
            r"^.*?\b(сгенерируй\w*|сгенерировать|сгенерить|сгенери|сген|генерируй\w*|"
            r"нарисуй\w*|нарисовать|создай\w*|создать|сделай\w*|сделать|"
            r"покажи\w*|пришли\w*|дай|дайте|выдай\w*|изобрази\w*|"
            r"сфотографируй|сфоткай|сделай фото|хочу фото|нужно фото|мне фото|можно фото)\b[:,!\s—–-]*",
            "",
            prompt,
            count=1,
            flags=re.IGNORECASE | re.DOTALL,
        ).strip()
        prompt = re.sub(
            r"^(?:мне\s+)?(?:фото|фотку|картинку|изображение|рисунок|портрет)\s*(?:с|про|на тему|где|как)?\s*",
            "",
            prompt,
            count=1,
            flags=re.IGNORECASE,
        ).strip()

    if not prompt:
        await context.bot.send_message(chat_id=chat.id, text="Подкинь мне саму задумку: кто или что в кадре, где всё происходит и какое настроение нужно. Остальное соберём.")
        return

    # Apply per-user cooldown and cap accepted work before waiting for the browser.
    global pending_generations
    user_key = str(update.effective_user.id if update.effective_user else chat.id)
    now = time.monotonic()
    previous_request = last_user_request_at.get(user_key, 0.0)
    if now - previous_request < USER_REQUEST_COOLDOWN_SECONDS:
        previous_notice = last_busy_notice_at.get(user_key, 0.0)
        if now - previous_notice >= 10:
            last_busy_notice_at[user_key] = now
            await context.bot.send_message(
                chat_id=chat.id,
                text=random.choice(BUSY_RESPONSES) + "\nНе закидывай меня повторами. Дай предыдущему запросу закончиться."
            )
        return
    if pending_generations >= MAX_PENDING_GENERATIONS:
        previous_notice = last_busy_notice_at.get(user_key, 0.0)
        if now - previous_notice >= 10:
            last_busy_notice_at[user_key] = now
            await context.bot.send_message(
                chat_id=chat.id,
                text=random.choice(BUSY_RESPONSES) + "\nОчередь переполнена. Попробуй немного позже."
            )
        return
    last_user_request_at[user_key] = now
    pending_generations += 1

    try:
        # Send normally, not as a reply: the original message may have been deleted.
        queued = generation_queue_lock.locked()
        if queued:
            status_text = (
                random.choice(GENERATION_ACKNOWLEDGEMENTS)
                + "\nОчередь занята, кожаный. Запущу, как только освободится браузер."
            )
        else:
            status_text = random.choice(GENERATION_ACKNOWLEDGEMENTS)
        status = await context.bot.send_message(chat_id=chat.id, text=status_text)
    
        async with generation_queue_lock:
            if queued:
                log.info("Queued generation acquired browser slot.")
            for attempt in range(1, MAX_ATTEMPTS + 1):
                if attempt > 1:
                    try:
                        await status.edit_text(f"Первый заход не сработал. Перезагружаю Flow и пробую ещё раз ({attempt}/{MAX_ATTEMPTS})…")
                    except Exception:
                        log.exception("Не удалось обновить статусное сообщение Telegram")
                        try:
                            status = await context.bot.send_message(
                                chat_id=chat.id,
                                text=f"Первый заход не сработал. Перезагружаю Flow и пробую ещё раз ({attempt}/{MAX_ATTEMPTS})…",
                            )
                        except Exception:
                            log.exception("Не удалось отправить статусное сообщение Telegram")
        
                try:
                    path = await asyncio.to_thread(generate_and_download, prompt)
                    with path.open("rb") as photo:
                        await context.bot.send_photo(
                            chat_id=chat.id,
                            photo=photo,
                            caption="Готово. Кадр на месте — забирай и смотри, как вышло.",
                        )
                    try:
                        await status.delete()
                    except Exception:
                        log.exception("Не удалось удалить статусное сообщение Telegram")
                    return
                except Exception as exc:
                    log.exception("Попытка %s завершилась ошибкой", attempt)
                    if (
                        "не открыл проект по прямой ссылке" in str(exc).lower()
                        or "не задан flow_project_url" in str(exc).lower()
                    ):
                        log.warning("Flow project routing failed; skipping futile browser retries.")
                        error_text = (
                            "Не удалось открыть рабочий проект Google Flow. "
                            "Я остановил повторы, чтобы не перезапускать тяжёлый браузер без смысла. "
                            "Проверь FLOW_PROJECT_URL и доступ к этому проекту в Google-аккаунте."
                        )
                        try:
                            await status.edit_text(error_text)
                        except Exception:
                            log.exception("Не удалось показать ошибку маршрута Flow")
                            try:
                                await context.bot.send_message(chat_id=chat.id, text=error_text)
                            except Exception:
                                log.exception("Не удалось отправить сообщение об ошибке маршрута Flow")
                        return
                    if "подозрительную активность" in str(exc).lower() or "suspicious activity" in str(exc).lower():
                        log.warning(
                            "Flow rejected generation with suspicious-activity warning; "
                            "automatic retries are disabled for this request."
                        )
                        error_text = (
                            "Flow отклонил запуск и показал предупреждение о подозрительной активности. "
                            "Я остановил повторы, чтобы не усугублять ситуацию. "
                            "Это отказ на стороне Flow, а не подтверждение проблемы с твоим промптом. "
                            "Проверь Flow в браузере; когда предупреждение исчезнет, можно попробовать снова."
                        )
                        try:
                            await status.edit_text(error_text)
                        except Exception:
                            log.exception("Не удалось показать пользователю отказ Flow")
                            try:
                                await context.bot.send_message(chat_id=chat.id, text=error_text)
                            except Exception:
                                log.exception("Не удалось отправить сообщение об отказе Flow")
                        return
                    if attempt >= MAX_ATTEMPTS:
                        error_text = random.choice(GENERATION_EXCUSES)
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
                        await status.edit_text(f"Flow споткнулся на попытке {attempt}. Дам ему 10 секунд, обновлю страницу и попробую снова…")
                    except Exception:
                        log.exception("Не удалось обновить статусное сообщение Telegram")
                        try:
                            status = await context.bot.send_message(
                                chat_id=chat.id,
                                text=f"Flow споткнулся на попытке {attempt}. Дам ему 10 секунд, обновлю страницу и попробую снова…",
                            )
                        except Exception:
                            log.exception("Не удалось отправить статусное сообщение Telegram")
                    await asyncio.sleep(10)
    
    finally:
        pending_generations = max(0, pending_generations - 1)



async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Report whether the Telegram process is responsive without claiming Flow is healthy."""
    message = update.effective_message
    if message is None:
        return
    session_ready = STATE_PATH.exists() or bool(CDP_URL)
    queue_state = "занят генерацией" if generation_queue_lock.locked() else "свободен"
    session_state = "сессия браузера настроена" if session_ready else "не найдена сессия Flow"
    await message.reply_text(
        "Я на связи. Telegram-часть работает.\n"
        f"Браузер: {session_state}.\n"
        f"Очередь: {queue_state}.\n"
        "Доступность генерации проверяется только при запуске запроса."
    )


async def on_application_error(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Log Telegram update-handler failures for diagnosis."""
    error = context.error
    log.error(
        "Unhandled Telegram update error; update_type=%s",
        type(update).__name__ if update is not None else "None",
        exc_info=(type(error), error, error.__traceback__) if error else None,
    )


def main():
    if not TOKEN:
        raise RuntimeError("Не задан TELEGRAM_BOT_TOKEN.")
    if not ALLOWED_CHAT_ID:
        raise RuntimeError("Задай ALLOWED_CHAT_ID для чата креаторов.")
    app = Application.builder().token(TOKEN).build()
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("img", handle_prompt))
    app.add_handler(MessageHandler(filters.TEXT & ~filters.COMMAND, handle_prompt))
    app.add_error_handler(on_application_error)
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



