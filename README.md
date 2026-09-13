# Mistral Messenger Assistant

[![CI](https://github.com/derleiti/mistral-messenger-assistant/actions/workflows/ci.yml/badge.svg?branch=main)](https://github.com/derleiti/mistral-messenger-assistant/actions/workflows/ci.yml)

Standalone Telegram assistant powered by Mistral with optional MCP workspace handoff.

## Capabilities

- persistent Mistral Agent conversations
- text and vision chat
- OCR/document handling for PDF/Office/text-oriented uploads
- Voxtral audio/voice transcription
- optional Mistral web search
- owner pairing and allow-listed groups
- first-run web setup
- optional MCP connector and AILinux workspace handoff
- detailed propagation of workspace pairing failures for diagnosable reconnect flows

TriForce is optional for ordinary Telegram/Mistral operation; it is used only when the MCP integration is enabled.

## Run

```bash
docker compose up -d --build
```

Then open `http://YOUR_SERVER:8080/setup` and configure the Telegram Bot token plus Mistral credentials. Runtime secrets are stored outside the image in the application data volume; the default Compose deployment does not bake credentials into the container.

## API

- `GET /health`
- `GET /setup` and `GET /setup/status`
- `POST /setup/configure`
- `POST /admin/pair`
- `POST /admin/chat`
- `GET /admin/settings` (redacted)
- `POST /telegram/webhook`

## Development

```bash
python3 -m venv .venv
.venv/bin/pip install -r backend/requirements-dev.txt
cd backend
PYTHONPATH=. ../.venv/bin/pytest -q tests
```

CI also builds the backend Docker image.

## Security

No credentials are shipped in the image; webhook requests use a configured secret; uploaded files are bounded; transient OCR uploads are cleaned up; admin reconfiguration requires the admin token after setup.

## License

AILinux-authored source is covered by the AILinux Proprietary Source License. Third-party libraries/services retain their own terms.
