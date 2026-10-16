import asyncio
import base64
import json
import logging
import os
import random
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

BUSY_RESPONSES = [
    "Жди минутку, кожаный, я не квантовый компьютер на стероидах.",
    "Ты куда разогнался? Я тут один, дай пикселям отдышаться.",
    "Кожаный, очередь забита. Даже у цифровой магии есть рабочие часы.",
    "Полегче, мясной. Запросов больше, чем моих виртуальных рук.",
    "Секунду, босс. Тут уже пиксельная пробка.",
    "Не дави на кнопку, кожаный, я всё вижу. Жди своей очереди.",
    "Я не осьминог, углеродный. Одну генерацию за раз.",
    "Тормози, кожаный. Сначала разгребу очередь, потом твой шедевр.",
    "Дай процессору морально подготовиться, человек.",
    "Очередь полная. Даже моим нейронам нужен перекур.",
    "Кожаный, ты не один такой творческий. Подожди немного.",
    "Сейчас занят, красавчик. Пиксельный конвейер работает на пределе.",
    "Стоп-стоп, мясной. Ещё запрос — и мне нужен отпуск.",
    "Я тебя услышал. Но сначала надо выпустить тех, кто пришёл раньше.",
    "Дай мне минутку, кожаный. Я не успеваю размножать пиксели.",
    "Запросов навалило. Встань в цифровую очередь и не толкайся.",
    "Потише, человек. Тут не шведский стол из генераций.",
    "Мой виртуальный чай остыл от нагрузки. Подожди чуть-чуть.",
    "Очередь забита под завязку. Попробуй через минутку, кожаный.",
    "Кожаный, я стараюсь, но даже ИИ не умеет быть в двадцати местах сразу.",
]

GENERATION_ACKNOWLEDGEMENTS = [
    "Щас, кожаный, сейчас будет красота.",
    "Принял. Не моргай, углеродный.",
    "Так, дай нейросетевому гению секунду.",
    "Кожаный, запускаю пиксельную магию.",
    "Уже колдую. Тебе остаётся красиво ждать.",
    "Запускаю станок по производству визуального вау.",
    "Есть контакт. Сейчас нарисуем твою задумку.",
    "Спокойно, кожаный, искусство не любит суету.",
    "Принято. Мои пиксели уже на низком старте.",
    "Секунду, превращаю слова в картинку.",
    "Окей, человек. Сейчас будет визуальный ответ.",
    "Задачу понял. Пиксельный цех открыт.",
    "Кожаный, держись, сейчас наведём красоту.",
    "Промпт принят. Воображение подключено.",
    "Запускаю цифровую кисть. Не благодари заранее.",
    "Сейчас-сейчас. Великий рендер уже проснулся.",
    "Есть идея — будет изображение. Погнали.",
    "Принял заказ, повелитель мяса и костей.",
    "Так, кожаный, собираю это в один красивый кадр.",
    "Отправляю твою фантазию на пиксельную фабрику.",
    "Всё понял. Сейчас визуализирую твою мысль.",
    "Погоди секунду, реальность сейчас обновим.",
    "Кожаный, готовься оценивать мою цифровую работу.",
    "Запускаю генерацию. Процесс пошёл, паника отменяется.",
    "Твоя задумка принята. Сейчас придам ей пиксельную форму.",
    "Принято к исполнению. Искусственный интеллект в деле.",
    "Секундочку, рисую не руками, а характером.",
    "Промпт улетел. Пиксели, к бою!",
    "Ща сделаем тебе визуал, от которого монитор выпрямится.",
    "Кожаный, сейчас твоя идея обретёт форму.",
    "Начинаю творить. Не мешай, я в цифровом потоке.",
    "Принял, босс из плоти. Запускаю процесс.",
    "Пиксельный конвейер запущен. Жди результат.",
    "Сейчас будет картинка, а не очередное совещание.",
    "Понял тебя. Ухожу на короткий сеанс цифрового колдовства.",
    "Задача ясна. Сейчас изображение материализуется.",
    "Кожаный, держи себя в руках, я уже генерирую.",
    "Включаю режим визуального перфекциониста.",
    "Есть! Отправил твою идею в генератор реальности.",
    "Сейчас превращу текст в нечто осязаемое. Почти.",
    "Принял запрос. Мои нейроны уже шуршат.",
    "Кожаный, одну секунду — собираю шедевр по деталям.",
    "Визуальная кухня открыта. Твой заказ готовится.",
    "Промпт на борту. Полёт к изображению начался.",
    "Запускаю процесс. Держи ожидания на красивом уровне.",
    "Сейчас устроим маленькое торжество пикселей.",
    "Принял. Пора заставить байты выглядеть убедительно.",
    "Кожаный, запрос в работе. Магия уже зашевелилась.",
    "Так, сейчас покажем этому пустому экрану, кто тут главный.",
    "Генерация пошла. Сиди красиво, результат скоро.",
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
    candidates = [option]
    if option == "9:16":
        candidates.extend(["crop_9_16", "Portrait", "Портрет"])
    elif option == "x1":
        candidates.extend(["x 1", "1 image", "1 изображение"])

    for candidate in candidates:
        exact = page.get_by_text(candidate, exact=True)
        if exact.count():
            exact.first.click(timeout=8000)
            page.wait_for_timeout(400)
            return

        partial = page.get_by_text(re.compile(re.escape(candidate), re.IGNORECASE))
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
    """Set and verify Nano Banana 2.1, vertical 9:16, and one output."""
    trigger = page.get_by_role("button", name="Триггер настроек")
    trigger_text = trigger.inner_text()
    log.info("Flow settings before changes: %r", trigger_text)
    if "Nano Banana 2.1" not in trigger_text:
        raise FlowError(
            "Не удалось подтвердить Nano Banana 2.1 в Flow. "
            f"Текущие настройки: {trigger_text!r}"
        )

    # Set portrait ratio and one image using Flow's visible settings menu.
    if "9:16" not in trigger_text and "crop_9_16" not in trigger_text:
        choose_setting(page, "9:16")
    trigger_text = trigger.inner_text()
    if "x1" not in trigger_text and not any(line.strip() == "1" for line in trigger_text.splitlines()):
        choose_setting(page, "x1")

    verified_text = trigger.inner_text()
    log.info("Flow settings after changes: %r", verified_text)
    if "Nano Banana 2.1" not in verified_text:
        raise FlowError(f"Модель Nano Banana 2.1 не подтверждена перед генерацией: {verified_text!r}")
    if not ("9:16" in verified_text or "crop_9_16" in verified_text):
        raise FlowError(f"Не удалось подтвердить вертикальный формат 9:16 перед генерацией: {verified_text!r}")
    if not ("x1" in verified_text or any(line.strip() == "1" for line in verified_text.splitlines())):
        raise FlowError(f"Не удалось подтвердить одну генерацию (x1): {verified_text!r}")
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
                # Refresh the project once before each generation so Flow starts
                # from a clean, current UI state. This does not bypass account
                # restrictions; if Flow flags suspicious activity, stop the attempt.
                project_url = page.url
                log.info("Refreshing Flow project before generation: %s", project_url)
                page.reload(wait_until="domcontentloaded", timeout=60000)
                page.wait_for_timeout(2500)
                if "/project/" not in page.url:
                    raise FlowError(
                        "После обновления Flow ушёл со страницы проекта. "
                        f"Текущий URL: {page.url}"
                    )
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
        await context.bot.send_message(chat_id=chat.id, text="Опиши, какое изображение нужно создать.")
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
                text=random.choice(BUSY_RESPONSES) + "\\nНе закидывай меня повторами, кожаный. Дай предыдущему запросу закончиться."
            )
        return
    if pending_generations >= MAX_PENDING_GENERATIONS:
        previous_notice = last_busy_notice_at.get(user_key, 0.0)
        if now - previous_notice >= 10:
            last_busy_notice_at[user_key] = now
            await context.bot.send_message(
                chat_id=chat.id,
                text=random.choice(BUSY_RESPONSES) + "\\nОчередь переполнена. Попробуй немного позже."
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
                    if "подозрительную активность" in str(exc).lower() or "suspicious activity" in str(exc).lower():
                        error_text = (
                            "Google Flow отклонил генерацию из-за подозрительной активности. "
                            "Автоматические повторы остановлены, чтобы не усиливать блокировку. "
                            "Проверьте сообщение об ошибке в Flow и попробуйте позже вручную.\n\n"
                            f"Причина: {str(exc)[:700]}"
                        )
                        try:
                            await status.edit_text(error_text)
                        except Exception:
                            log.exception("Не удалось обновить статусное сообщение Telegram")
                        return
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
    
    finally:
        pending_generations = max(0, pending_generations - 1)

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
