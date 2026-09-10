import httpx


class TelegramClient:
    def __init__(self, token: str):
        self.token = token
        self.base = f"https://api.telegram.org/bot{token}"

    async def call(self, method: str, payload: dict | None = None) -> dict:
        if not self.token:
            raise RuntimeError("TELEGRAM_BOT_TOKEN is not configured")
        async with httpx.AsyncClient(timeout=30) as client:
            r = await client.post(f"{self.base}/{method}", json=payload or {})
        try:
            data = r.json()
        except ValueError as exc:
            raise RuntimeError(f"Telegram {method} returned invalid JSON") from exc
        if not data.get("ok"):
            raise RuntimeError(f"Telegram {method} failed: {data.get('description', r.text)}")
        return data["result"]

    async def get_me(self) -> dict:
        return await self.call("getMe")

    async def get_file(self, file_id: str) -> dict:
        return await self.call("getFile", {"file_id": file_id})

    async def download_file(self, file_id: str, max_bytes: int) -> tuple[bytes, dict]:
        meta = await self.get_file(file_id)
        declared = int(meta.get("file_size") or 0)
        if declared and declared > max_bytes:
            raise RuntimeError(
                f"Telegram-Datei ist zu groß ({declared / 1_000_000:.1f} MB; Limit {max_bytes / 1_000_000:.1f} MB)"
            )
        file_path = str(meta.get("file_path") or "")
        if not file_path:
            raise RuntimeError("Telegram getFile returned no file_path")
        url = f"https://api.telegram.org/file/bot{self.token}/{file_path}"
        async with httpx.AsyncClient(timeout=90, follow_redirects=True) as client:
            r = await client.get(url)
            r.raise_for_status()
        content = r.content
        if len(content) > max_bytes:
            raise RuntimeError(
                f"Telegram-Datei ist zu groß ({len(content) / 1_000_000:.1f} MB; Limit {max_bytes / 1_000_000:.1f} MB)"
            )
        return content, meta

    async def send_message(self, chat_id: int | str, text: str, reply_to: int | None = None):
        payload: dict = {"chat_id": chat_id, "text": text}
        if reply_to is not None:
            payload["reply_parameters"] = {"message_id": reply_to}
        return await self.call("sendMessage", payload)

    async def typing(self, chat_id: int | str):
        try:
            await self.call("sendChatAction", {"chat_id": chat_id, "action": "typing"})
        except Exception:
            pass

    async def set_webhook(self, url: str, secret_token: str):
        return await self.call(
            "setWebhook",
            {
                "url": url,
                "secret_token": secret_token,
                "allowed_updates": ["message", "my_chat_member"],
                "drop_pending_updates": False,
            },
        )
