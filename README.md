# Mistral Messenger Assistant

A small standalone Telegram assistant powered only by the Mistral API.

## Capabilities

- persistent Mistral Agent conversations
- text chat
- image/screenshot analysis with Mistral Vision
- PDF/DOC/DOCX/PPT/PPTX/ODT/RTF/EPUB processing with Mistral OCR
- text, Markdown, JSON, logs, CSV and source-code files
- Telegram voice/audio transcription with Voxtral
- optional Mistral web search
- optional MCP connector support
- owner pairing and allowlisted group support
- first-run setup API and web page

TriForce, OpenRouter and other AI providers are not required.

## First run

```bash
docker compose up -d --build
```

Open:

```text
http://YOUR_SERVER:8080/setup
```

Enter a Mistral API key and Telegram Bot token. You can either provide an existing Mistral Agent ID or leave it blank and let the assistant create and configure an Agent through the Mistral API.

For Telegram webhooks, expose the service through HTTPS and enter the public base URL in setup.

## Empty image / persistent setup

The published Docker image contains no API keys, Telegram token or Agent ID. Runtime configuration is stored in the Docker volume under `/app/data`. Rebuilding or replacing the container keeps the configuration.

Environment variables remain supported for immutable deployments, but the default Compose file deliberately does not load `.env`.

## API

- `GET /setup/status` - safe first-run status
- `GET /setup` - minimal setup UI
- `POST /setup/configure` - validate Telegram/Mistral and create or attach an Agent
- `GET /health` - health and capabilities
- `POST /admin/pair` - create owner pairing link (admin token required)
- `POST /admin/chat` - direct Agent test (admin token required)
- `GET /admin/settings` - redacted runtime settings (admin token required)
- `POST /telegram/webhook` - Telegram webhook

After setup is complete, reconfiguration requires the admin token.

## Optional MCP

MCP is disabled by default. Set it during setup only when desired. A normal installation needs only Mistral and Telegram.

## Telegram commands

- `/new` or `/restart` - start a new Mistral conversation
- `/status` - show basic assistant status

## Security notes

- no credentials are shipped in the image
- secret values are not returned by `/admin/settings`
- setup becomes admin-token protected once configured
- Telegram webhook requests require the configured webhook secret
- uploaded files are size-limited and transient Mistral OCR uploads are deleted after processing
