"""Kie AI image generation runner for the Render Background Worker."""
import json
import logging
import os
import time
import urllib.error
import urllib.parse
import urllib.request
from pathlib import Path

import bot

API_KEY = os.getenv("KIE_API_KEY", "").strip()
API_BASE_URL = os.getenv("KIE_API_BASE_URL", "https://api.kie.ai").strip().rstrip("/")
MODEL = "gpt-image-2-5-sunburst-image-to-image"
ASPECT_RATIO = "9:16"
RESOLUTION = "1K"
BACKGROUND = "auto"
POLL_INTERVAL_SECONDS = 2
TASK_TIMEOUT_SECONDS = 360
HTTP_TIMEOUT_SECONDS = 60

log = logging.getLogger("kie-image-runner")


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
    url_keys = {"resulturls", "imageurls", "images", "imageurl", "outputurl", "url"}

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
    # Preserve order and avoid duplicate downloads.
    return list(dict.fromkeys(urls))


def _task_data(response: dict) -> dict:
    data = response.get("data")
    if isinstance(data, dict):
        return data
    return {}


def _create_task(prompt: str) -> str:
    payload = {
        "model": MODEL,
        "input": {
            "prompt": prompt,
            "aspect_ratio": ASPECT_RATIO,
            "resolution": RESOLUTION,
            "background": BACKGROUND,
        },
    }
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
    deadline = time.monotonic() + TASK_TIMEOUT_SECONDS
    last_state = "unknown"

    while time.monotonic() < deadline:
        response = _get_task(task_id)
        data = _task_data(response)
        state = str(data.get("state") or data.get("status") or "").strip().lower()
        if state:
            last_state = state

        if state in {"success", "succeeded", "completed", "complete", "finished"}:
            urls = _extract_urls(data.get("resultJson", data))
            if not urls:
                urls = _extract_urls(data)
            if urls:
                return urls[0]
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

        time.sleep(POLL_INTERVAL_SECONDS)

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


def kie_generate_and_download(prompt: str) -> Path:
    if not API_KEY:
        raise bot.GenerationError(
            "В Render Environment ещё не задан KIE_API_KEY. "
            "Добавь ключ в Background Worker → Environment и перезапусти сервис."
        )
    if not prompt.strip():
        raise bot.GenerationError("Промпт пустой.")

    task_id = _create_task(prompt.strip())
    result_url = _wait_for_result(task_id)
    return _download_result(result_url)


def main():
    bot.generate_and_download = kie_generate_and_download
    bot.main()


if __name__ == "__main__":
    main()
