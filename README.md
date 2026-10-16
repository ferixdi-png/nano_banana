# Ferixdi Telegram Image Bot — Kie AI

Telegram bot that submits image-generation prompts to Kie AI and returns the generated image. The Render Background Worker runs the bot and polls Kie AI until the task succeeds or fails.

## Image generation settings

- Provider: Kie AI
- Model: `gpt-image-2-5-sunburst-image-to-image`
- Aspect ratio: `9:16`
- Resolution: `1K`
- Background: `auto`
- API endpoint: `POST /api/v1/jobs/createTask`
- Task status: `GET /api/v1/jobs/recordInfo?taskId=...`

## Render Background Worker environment

Required:

- `TELEGRAM_BOT_TOKEN`: Telegram bot token from BotFather.
- `ALLOWED_USER_ID`: numeric Telegram user ID allowed to use the bot.
- `KIE_API_KEY`: API key from Kie AI.

Optional:

- `KIE_API_BASE_URL`: defaults to `https://api.kie.ai`.

Add the Kie key to the **Background Worker** service named `nano_banana` in Render → Environment. Do not put API keys in GitHub, source files, chat messages, or the web service unless that service independently needs the key.

## Runtime flow

1. The bot accepts a text prompt from the authorized Telegram user.
2. The worker creates one Kie AI task using the configured model, `9:16`, `1K`, and `auto` background.
3. The worker polls task status and reads the returned result URL.
4. The worker downloads the image and sends it back to Telegram.
5. On failure, the bot reports the error and does not automatically create another paid task, avoiding accidental duplicate charges.

The bot uses a single active generation path; Google Flow / Chromium is not used by this worker.

## Local run

Install dependencies with `python -m pip install -r requirements.txt`, set the required environment variables, then run `python cloud_runner.py`.
