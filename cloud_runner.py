"""Kie AI image generation runner for the Render Background Worker."""
import json
import base64
import logging
import mimetypes
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bot

API_KEY = os.getenv("KIE_API_KEY", "").strip()
API_BASE_URL = os.getenv("KIE_API_BASE_URL", "https://api.kie.ai").strip().rstrip("/")
MODEL = "gpt-image-2-5-sunburst-text-to-image"
IMAGE_EDIT_MODEL = "gpt-image-2-5-sunburst-image-to-image"
IMAGE_UPLOAD_URL = "https://kieai.redpandaai.co/api/file-base64-upload"
MAX_INPUT_IMAGE_BYTES = 10 * 1024 * 1024
DEFAULT_ASPECT_RATIO = "9:16"
ASPECT_RATIOS = ("27:16", "16:27", "21:9", "16:9", "9:16", "3:2", "2:3", "4:3", "3:4", "1:1", "9:8", "8:9")
RESOLUTION = "1K"
BACKGROUND = "auto"
POLL_INTERVAL_SECONDS = 2
TASK_TIMEOUT_SECONDS = 360
HTTP_TIMEOUT_SECONDS = 60

log = logging.getLogger("kie-image-runner")


def _detect_aspect_ratio(prompt: str) -> str:
    """Honor an explicitly requested supported ratio; otherwise default to portrait."""
    normalized = prompt.lower().replace("×", "x")

    # Explicit ratio notation, allowing spaces around the colon.
    for ratio in ASPECT_RATIOS:
        width, height = ratio.split(":")
        pattern = rf"(?<!\d){width}\s*:\s*{height}(?!\d)"
        if re.search(pattern, normalized):
            return ratio

    # Common explicit dimension notation, converted to the nearest supported ratio.
    dimensions = re.search(r"(?<!\d)(\d{3,5})\s*x\s*(\d{3,5})(?!\d)", normalized)
    if dimensions:
        width, height = int(dimensions.group(1)), int(dimensions.group(2))
        if width > 0 and height > 0:
            requested = width / height
            return min(
                ASPECT_RATIOS,
                key=lambda ratio: abs(requested - int(ratio.split(":")[0]) / int(ratio.split(":")[1])),
            )

    if re.search(r"\b(square|квадрат(?:ный|ное|ная)?|1:1)\b", normalized):
        return "1:1"
    if re.search(r"\b(landscape|горизонтальн\w*)\b", normalized):
        return "16:9"
    if re.search(r"\b(portrait|вертикальн\w*)\b", normalized):
        return "9:16"

    return DEFAULT_ASPECT_RATIO


def _request_json(url: str, method: str = "GET", payload: dict | None = None) -> dict:
    headers = {
        "Authorization": f"Bearer {API_KEY}",
        "Accept": "application/json",
        "User-Agent": "FerixdiBot/1.0",
    }
    data = None
    if payload is not None:
        headers["Content-Type"] = "application/json"
        data = json.dumps(payload, ensure_ascii=False).encode("utf-8")

    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            raw = response.read()
    except urllib.error.HTTPError as exc:
        body = exc.read().decode("utf-8", errors="replace")
        try:
            details = json.loads(body)
            message = details.get("msg") or details.get("message") or body
        except Exception:
            message = body or str(exc)
        if exc.code in (401, 403):
            raise bot.GenerationError(
                "Kie AI отклонил API-ключ. Проверь KIE_API_KEY в Environment Worker."
            ) from exc
        raise bot.GenerationError(
            f"Kie AI вернул HTTP {exc.code}: {str(message)[:500]}"
        ) from exc
    except (urllib.error.URLError, TimeoutError) as exc:
        raise bot.GenerationError(
            f"Не удалось связаться с Kie AI: {str(exc)[:300]}"
        ) from exc

    try:
        result = json.loads(raw.decode("utf-8"))
    except Exception as exc:
        raise bot.GenerationError("Kie AI вернул ответ не в формате JSON.") from exc

    if not isinstance(result, dict):
        raise bot.GenerationError("Kie AI вернул неожиданный формат ответа.")

    code = result.get("code")
    if code is not None and str(code) not in ("200", "0"):
        message = result.get("msg") or result.get("message") or "неизвестная ошибка"
        raise bot.GenerationError(f"Kie AI: {str(message)[:500]} (код {code}).")
    return result


def _extract_urls(value):
    """Extract result URLs from the common Kie task-result response shapes."""
    urls = []
    url_keys = {"resulturls", "imageurls", "images", "imageurl", "outputurl", "downloadurl", "fileurl", "url"}

    def walk(node):
        if isinstance(node, dict):
            for key, child in node.items():
                normalized = str(key).replace("_", "").lower()
                if normalized in url_keys:
                    if isinstance(child, str) and child.startswith(("https://", "http://")):
                        urls.append(child)
                    elif isinstance(child, list):
                        for item in child:
                            if isinstance(item, str) and item.startswith(("https://", "http://")):
                                urls.append(item)
                            else:
                                walk(item)
                    else:
                        walk(child)
                else:
                    walk(child)
        elif isinstance(node, list):
            for child in node:
                walk(child)
        elif isinstance(node, str) and node.strip().startswith("{"):
            try:
                walk(json.loads(node))
            except Exception:
                pass

    walk(value)
    return list(dict.fromkeys(urls))


def _task_data(response: dict) -> dict:
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _upload_input_image(image_path: Path) -> str:
    """Upload a Telegram image to Kie and return its temporary HTTPS URL."""
    if not image_path.exists() or not image_path.is_file():
        raise bot.GenerationError("Исходное фото не найдено на сервере.")
    image_size = image_path.stat().st_size
    if image_size <= 0:
        raise bot.GenerationError("Исходное фото пустое.")
    if image_size > MAX_INPUT_IMAGE_BYTES:
        raise bot.GenerationError("Фото больше 10 МБ. Отправь изображение поменьше.")

    mime_type = mimetypes.guess_type(image_path.name)[0] or "image/jpeg"
    encoded = base64.b64encode(image_path.read_bytes()).decode("ascii")
    payload = {
        "base64Data": f"data:{mime_type};base64,{encoded}",
        "uploadPath": "images/base64",
        "fileName": image_path.name,
    }
    response = _request_json(IMAGE_UPLOAD_URL, method="POST", payload=payload)
    data = response.get("data") if isinstance(response.get("data"), dict) else {}
    url = data.get("downloadUrl") or data.get("download_url") or data.get("url")
    if not isinstance(url, str) or not url.startswith("https://"):
        raise bot.GenerationError(
            "Kie AI не вернул HTTPS-ссылку на загруженное фото. "
            f"Ответ: {json.dumps(response, ensure_ascii=False)[:300]}"
        )
    log.info("Input image uploaded to Kie successfully (%s bytes)", image_size)
    return url


def _create_task(prompt: str, input_image_url: str | None = None) -> str:
    if input_image_url:
        model = IMAGE_EDIT_MODEL
        input_data = {
            "prompt": prompt,
            "input_urls": [input_image_url],
            "aspect_ratio": "auto",
            "resolution": RESOLUTION,
            "background": "auto",
        }
        log.info("Creating Kie image-to-image task; preserving source image ratio")
    else:
        model = MODEL
        aspect_ratio = _detect_aspect_ratio(prompt)
        input_data = {
            "prompt": prompt,
            "aspect_ratio": aspect_ratio,
            "resolution": RESOLUTION,
            "background": BACKGROUND,
        }
        log.info("Using text-to-image aspect ratio: %s", aspect_ratio)

    payload = {"model": model, "input": input_data}
    response = _request_json(
        f"{API_BASE_URL}/api/v1/jobs/createTask",
        method="POST",
        payload=payload,
    )
    data = _task_data(response)
    task_id = data.get("taskId") or data.get("task_id")
    if not task_id:
        raise bot.GenerationError(
            "Kie AI принял запрос, но не вернул taskId. "
            f"Ответ: {json.dumps(response, ensure_ascii=False)[:500]}"
        )
    log.info("Kie AI task created: %s", task_id)
    return str(task_id)


def _get_task(task_id: str) -> dict:
    query = urllib.parse.urlencode({"taskId": task_id})
    return _request_json(f"{API_BASE_URL}/api/v1/jobs/recordInfo?{query}")


def _wait_for_result(task_id: str) -> str:
    """Poll Kie by the exact taskId; tolerate transient API/network failures."""
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    last_state = "unknown"
    consecutive_errors = 0
    logged_state = None

    while time.monotonic() < deadline:
        try:
            response = _get_task(task_id)
            consecutive_errors = 0
        except bot.GenerationError as exc:
            message = str(exc)
            transient = (
                "Не удалось связаться с Kie AI" in message
                or "HTTP 429" in message
                or any(f"HTTP {code}" in message for code in (500, 502, 503, 504))
                or "(код 429)" in message
                or "(код 500)" in message
                or "(код 502)" in message
                or "(код 503)" in message
                or "(код 504)" in message
            )
            if not transient:
                raise
            consecutive_errors += 1
            delay = min(15, max(POLL_INTERVAL_SECONDS, 2 ** min(consecutive_errors, 4)))
            log.warning(
                "Temporary Kie status-check error for task_id=%s; retry=%s in %ss: %s",
                task_id, consecutive_errors, delay, message[:220]
            )
            time.sleep(min(delay, max(0, deadline - time.monotonic())))
            continue

        data = _task_data(response)
        returned_task_id = str(data.get("taskId") or data.get("task_id") or "").strip()
        if returned_task_id and returned_task_id != task_id:
            raise bot.GenerationError(
                "Kie AI вернул данные другой задачи. Результат не будет отправлен, "
                f"чтобы не перепутать изображения. Ожидался taskId={task_id}, "
                f"получен taskId={returned_task_id}."
            )

        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if state:
            last_state = state
        if state != logged_state:
            log.info("Kie task status: task_id=%s state=%s", task_id, state or "missing")
            logged_state = state

        if state in {"success", "succeeded", "completed", "complete", "finished"}:
            result_payload = data.get("resultJson")
            urls = _extract_urls(result_payload) if result_payload is not None else []
            if not urls:
                urls = _extract_urls(data)
            if urls:
                log.info("Kie task completed with result URL: task_id=%s", task_id)
                return urls[0]
            # Kie may publish state before resultJson becomes readable; re-query briefly.
            consecutive_errors += 1
            if consecutive_errors <= 5 and time.monotonic() < deadline:
                log.warning(
                    "Kie task is successful but result URL is not present yet; "
                    "rechecking task_id=%s attempt=%s",
                    task_id, consecutive_errors
                )
                time.sleep(min(2, max(0, deadline - time.monotonic())))
                continue
            raise bot.GenerationError(
                "Kie AI отметил задачу успешной, но не вернул ссылку на изображение. "
                f"Task ID: {task_id}"
            )

        if state in {"fail", "failed", "error", "cancelled", "canceled"}:
            reason = (
                data.get("failMsg")
                or data.get("errorMessage")
                or data.get("message")
                or data.get("error")
                or "причина не указана"
            )
            raise bot.GenerationError(
                f"Kie AI не смог сгенерировать изображение: {str(reason)[:500]}. "
                f"Task ID: {task_id}"
            )

        time.sleep(min(POLL_INTERVAL_SECONDS, max(0, deadline - time.monotonic())))

    raise bot.GenerationError(
        f"Kie AI не завершил генерацию за {TASK_TIMEOUT_SECONDS} секунд. "
        f"Последний статус: {last_state}. Task ID: {task_id}"
    )


def _download_result(url: str) -> Path:
    request = urllib.request.Request(
        url,
        headers={"User-Agent": "FerixdiBot/1.0"},
        method="GET",
    )
    try:
        with urllib.request.urlopen(request, timeout=HTTP_TIMEOUT_SECONDS) as response:
            content_type = response.headers.get("Content-Type", "").lower()
            image_bytes = response.read()
    except Exception as exc:
        raise bot.GenerationError(
            f"Задача готова, но скачать изображение не удалось: {str(exc)[:300]}"
        ) from exc

    if not image_bytes:
        raise bot.GenerationError("Kie AI вернул пустой файл изображения.")

    if "png" in content_type:
        extension = ".png"
    elif "webp" in content_type:
        extension = ".webp"
    else:
        extension = ".jpg"

    destination = bot.OUTPUT_DIR / f"gpt_image_25_{int(time.time())}{extension}"
    destination.write_bytes(image_bytes)
    if not destination.exists() or destination.stat().st_size == 0:
        raise bot.GenerationError("Не удалось сохранить скачанное изображение.")

    log.info("Image downloaded (%s bytes): %s", destination.stat().st_size, destination.name)
    return destination


def kie_generate_and_download(
    prompt: str, input_image_path: Path | None = None
) -> Path:
    if not API_KEY:
        raise bot.GenerationError(
            "В Render Environment ещё не задан KIE_API_KEY. "
            "Добавь ключ в Background Worker → Environment и перезапусти сервис."
        )
    if not prompt.strip():
        raise bot.GenerationError("Промпт пустой.")

    input_image_url = _upload_input_image(input_image_path) if input_image_path else None
    task_id = _create_task(prompt.strip(), input_image_url=input_image_url)
    result_url = _wait_for_result(task_id)
    return _download_result(result_url)


def main():
    bot.generate_and_download = kie_generate_and_download
    bot.main()


if __name__ == "__main__":
    main()
