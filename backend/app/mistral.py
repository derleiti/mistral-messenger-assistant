from __future__ import annotations

from dataclasses import dataclass
from typing import Any

import httpx


class MistralConversationNotFound(RuntimeError):
    pass


@dataclass
class MistralReply:
    text: str
    conversation_id: str
    route: str
    meta: dict[str, Any]


class MistralClient:
    def __init__(
        self,
        api_key: str,
        agent_id: str,
        agent_version: int | str | None = 0,
        base_url: str = "https://api.mistral.ai/v1",
        timeout: float = 210.0,
    ):
        self.api_key = api_key.strip()
        self.agent_id = agent_id.strip()
        self.agent_version = agent_version
        self.base_url = base_url.rstrip("/")
        self.timeout = timeout

    @property
    def configured(self) -> bool:
        return bool(self.api_key and self.agent_id)

    @property
    def api_configured(self) -> bool:
        return bool(self.api_key)

    def _headers(self) -> dict[str, str]:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        return {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
            "User-Agent": "nova-telegram-bridge/0.3",
        }

    @staticmethod
    def _error_text(response: httpx.Response) -> str:
        try:
            data = response.json()
            if isinstance(data, dict):
                detail = data.get("detail") or data.get("message") or data.get("error")
                if isinstance(detail, dict):
                    detail = detail.get("message") or detail.get("detail") or str(detail)
                if detail:
                    return str(detail)
        except Exception:
            pass
        text = response.text.strip()
        return text[:600] if text else response.reason_phrase

    async def _request(self, method: str, path: str, payload: dict[str, Any] | None = None) -> dict[str, Any]:
        if not self.configured:
            missing = []
            if not self.api_key:
                missing.append("MISTRAL_API_KEY")
            if not self.agent_id:
                missing.append("MISTRAL_AGENT_ID")
            raise RuntimeError(f"Missing Mistral configuration: {', '.join(missing)}")

        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )

        if response.status_code in {404, 410} and path.startswith("/conversations/"):
            raise MistralConversationNotFound("Stored Mistral conversation no longer exists")
        if response.status_code >= 400:
            detail = self._error_text(response)
            if response.status_code == 401:
                raise RuntimeError("Mistral authentication failed (401). Check MISTRAL_API_KEY.")
            if response.status_code == 429:
                raise RuntimeError(f"Mistral rate limit reached (429): {detail}")
            raise RuntimeError(f"Mistral API HTTP {response.status_code}: {detail}")

        if response.status_code == 204 or not response.content:
            return {}
        try:
            data = response.json()
        except ValueError as exc:
            raise RuntimeError("Mistral API returned invalid JSON") from exc
        if not isinstance(data, (dict, list)):
            raise RuntimeError("Mistral API returned an unexpected response")
        return data

    @staticmethod
    def _content_text(content: Any) -> str:
        """Return user-visible text only; never surface reasoning/thinking chunks."""
        if isinstance(content, str):
            return content
        if isinstance(content, list):
            parts: list[str] = []
            for chunk in content:
                if isinstance(chunk, str):
                    parts.append(chunk)
                    continue
                if not isinstance(chunk, dict):
                    continue
                chunk_type = str(chunk.get("type") or "").lower()
                if chunk_type in {"thinking", "reasoning", "tool_call", "function_call"}:
                    continue
                if chunk_type and chunk_type not in {"text", "output_text"}:
                    continue
                text = chunk.get("text") or chunk.get("content")
                if isinstance(text, str):
                    parts.append(text)
            return "".join(parts)
        if isinstance(content, dict):
            chunk_type = str(content.get("type") or "").lower()
            if chunk_type in {"thinking", "reasoning", "tool_call", "function_call"}:
                return ""
            text = content.get("text") or content.get("content")
            return text if isinstance(text, str) else ""
        return ""

    @classmethod
    def _extract_text(cls, data: dict[str, Any]) -> str:
        """Select the final assistant message instead of concatenating intermediate outputs."""
        outputs = data.get("outputs")
        if not isinstance(outputs, list):
            return ""

        candidates: list[str] = []
        for output in outputs:
            if not isinstance(output, dict):
                continue
            output_type = str(output.get("type") or "").lower()
            role = output.get("role")
            if output_type and output_type not in {"message.output", "message", "assistant_message"}:
                continue
            if role not in {None, "assistant"}:
                continue
            text = cls._content_text(output.get("content")).strip()
            if text:
                candidates.append(text)
        return candidates[-1] if candidates else ""

    async def create_agent(
        self,
        name: str,
        model: str,
        description: str,
        instructions: str,
        tools: list[dict[str, Any]] | None = None,
    ) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        payload = {
            "name": name,
            "model": model,
            "description": description,
            "instructions": instructions,
            "tools": tools or [],
            "completion_args": {
                "response_format": {"type": "text"},
                "reasoning_effort": "none",
                "temperature": 0.4,
                "top_p": 0.95,
                "max_tokens": 1200,
            },
        }
        return await self._request_api_only("POST", "/agents", payload)

    async def get_agent(self, agent_id: str) -> dict[str, Any]:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        return await self._request_api_only("GET", f"/agents/{agent_id}")

    async def _request_api_only(self, method: str, path: str, payload: dict[str, Any] | None = None):
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.request(
                method,
                f"{self.base_url}{path}",
                headers=self._headers(),
                json=payload,
            )
        if response.status_code >= 400:
            detail = self._error_text(response)
            raise RuntimeError(f"Mistral API HTTP {response.status_code}: {detail}")
        if response.status_code == 204 or not response.content:
            return {}
        data = response.json()
        if not isinstance(data, (dict, list)):
            raise RuntimeError("Mistral API returned an unexpected response")
        return data

    async def ask(
        self,
        message: str,
        conversation_id: str | None = None,
        metadata: dict[str, Any] | None = None,
    ) -> MistralReply:
        if conversation_id:
            data = await self._request(
                "POST",
                f"/conversations/{conversation_id}",
                {
                    "inputs": message,
                    "store": True,
                    "handoff_execution": "server",
                },
            )
        else:
            payload: dict[str, Any] = {
                "agent_id": self.agent_id,
                "inputs": message,
                "store": True,
                "handoff_execution": "server",
            }
            if self.agent_version is not None:
                payload["agent_version"] = self.agent_version
            if metadata:
                payload["metadata"] = metadata
            data = await self._request("POST", "/conversations", payload)

        new_id = str(data.get("conversation_id") or conversation_id or "").strip()
        if not new_id:
            raise RuntimeError("Mistral API response did not contain a conversation_id")
        text = self._extract_text(data)
        if not text:
            raise RuntimeError("Mistral agent returned no textual response")

        return MistralReply(
            text=text,
            conversation_id=new_id,
            route=f"mistral-agent:{self.agent_id}",
            meta={"usage": data.get("usage") or {}},
        )


    async def vision(self, image_bytes: bytes, mime_type: str, prompt: str, model: str) -> str:
        import base64
        encoded = base64.b64encode(image_bytes).decode("ascii")
        payload = {
            "model": model,
            "messages": [{
                "role": "user",
                "content": [
                    {"type": "text", "text": prompt},
                    {"type": "image_url", "image_url": f"data:{mime_type};base64,{encoded}"},
                ],
            }],
        }
        data = await self._request("POST", "/chat/completions", payload)
        choices = data.get("choices") or []
        if not choices:
            raise RuntimeError("Mistral vision returned no choices")
        message = choices[0].get("message") or {}
        text = self._content_text(message.get("content")).strip()
        if not text:
            raise RuntimeError("Mistral vision returned no text")
        return text

    async def upload_ocr_file(self, file_bytes: bytes, file_name: str, mime_type: str) -> str:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        files = {"file": (file_name, file_bytes, mime_type or "application/octet-stream")}
        data = {"purpose": "ocr", "visibility": "user"}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/files",
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                data=data,
                files=files,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Mistral files HTTP {response.status_code}: {self._error_text(response)}")
        result = response.json()
        file_id = str(result.get("id") or "")
        if not file_id:
            raise RuntimeError("Mistral file upload returned no id")
        return file_id

    async def delete_file(self, file_id: str) -> None:
        if not file_id:
            return
        try:
            await self._request("DELETE", f"/files/{file_id}")
        except Exception:
            pass

    async def ocr(self, file_bytes: bytes, file_name: str, mime_type: str, model: str) -> str:
        file_id = await self.upload_ocr_file(file_bytes, file_name, mime_type)
        try:
            data = await self._request(
                "POST",
                "/ocr",
                {
                    "model": model,
                    "document": {"type": "file", "file_id": file_id},
                    "table_format": "markdown",
                    "include_blocks": False,
                },
            )
            pages = data.get("pages") or []
            text = "\n\n".join(str(page.get("markdown") or "") for page in pages if isinstance(page, dict)).strip()
            if not text:
                raise RuntimeError("Mistral OCR returned no text")
            return text
        finally:
            await self.delete_file(file_id)

    async def transcribe(self, file_bytes: bytes, file_name: str, mime_type: str, model: str) -> str:
        if not self.api_key:
            raise RuntimeError("MISTRAL_API_KEY is not configured")
        files = {"file": (file_name, file_bytes, mime_type or "application/octet-stream")}
        data = {"model": model}
        async with httpx.AsyncClient(timeout=self.timeout) as client:
            response = await client.post(
                f"{self.base_url}/audio/transcriptions",
                headers={"Authorization": f"Bearer {self.api_key}", "Accept": "application/json"},
                data=data,
                files=files,
            )
        if response.status_code >= 400:
            raise RuntimeError(f"Mistral audio HTTP {response.status_code}: {self._error_text(response)}")
        result = response.json()
        text = str(result.get("text") or "").strip()
        if not text:
            raise RuntimeError("Mistral transcription returned no text")
        return text

    async def ensure_default_mcp(
        self,
        name: str,
        server: str,
        visibility: str = "shared_workspace",
        credentials_name: str = "default",
    ) -> dict[str, Any]:
        """Ensure the default public MCP connector exists, is usable, and is attached to the agent."""
        if not self.configured:
            raise RuntimeError("Mistral agent is not fully configured")

        listing = await self._request("GET", "/connectors?page_size=100")
        items = listing.get("items") or []
        connector = next(
            (item for item in items if isinstance(item, dict) and item.get("server") == server),
            None,
        )
        if connector is None:
            connector = next(
                (item for item in items if isinstance(item, dict) and item.get("name") == name),
                None,
            )

        description = (
            "Default AILinux TriForce MCP for Nova Telegram. Provides AILinux/TriForce tools "
            "and browser-based local workspace auto-pairing through workspace_status."
        )
        connector_prompt = (
            "Use this connector for AILinux, TriForce, coding and local workspace tasks. Keep four concepts separate: "
            "(1) the Mistral MCP transport session, which may be recreated; (2) the persistent TriForce logical workspace lease; "
            "(3) the browser executor transport, reported as transport_state=online/offline and executor_online=true/false; and "
            "(4) the local access mode, read_only or write. When an internal continuity instruction supplies a workspace ID, call "
            "workspace_status with workspace_id exactly once as bootstrap. Preserve the returned workspace_token in conversation/tool "
            "context and use workspace_token for later local workspace calls. Never keep reusing the human pairing ID: it is short-lived. "
            "state=ready and connected=true describe the logical lease and remain stable while the browser executor is offline. "
            "WORKSPACE_TRANSPORT_OFFLINE or transport_state=offline does not invalidate the lease and must not request a new ID; ask the "
            "user to resume the browser executor and retry with the same workspace_token. Only state=unpaired/expired or an invalid "
            "workspace token requires a new pairing ID. Never expose or repeat either pairing ID or workspace_token. The Mistral connector "
            "visibility value shared_workspace is connector scope only and is unrelated to TriForce workspace lease/access state. "
            "Without any active workspace context, explain that the user must open https://api.ailinux.me/v1/mcp, choose a folder and "
            "access mode, connect it, and provide the pairing ID."
        )


        if connector is None:
            connector = await self._request(
                "POST",
                "/connectors",
                {
                    "name": name,
                    "description": description,
                    "server": server,
                    "visibility": visibility,
                },
            )

        connector_id = str(connector.get("id") or "")
        if not connector_id:
            raise RuntimeError("Mistral connector setup returned no connector id")

        if connector.get("server") != server or connector.get("description") != description:
            connector = await self._request(
                "PATCH",
                f"/connectors/{connector_id}",
                {
                    "server": server,
                    "description": description,
                    "title": "AILinux TriForce MCP",
                    "system_prompt": connector_prompt,
                },
            )
        else:
            # Keep the connector-specific instructions current without touching credentials.
            try:
                connector = await self._request(
                    "PATCH",
                    f"/connectors/{connector_id}",
                    {"title": "AILinux TriForce MCP", "system_prompt": connector_prompt},
                )
            except Exception:
                pass

        if visibility == "shared_workspace":
            await self._request("POST", f"/connectors/{connector_id}/workspace/activate", {})
            await self._request(
                "POST",
                f"/connectors/{connector_id}/workspace/credentials",
                {
                    "name": credentials_name,
                    "credentials": {"headers": {}},
                    "is_default": True,
                },
            )
        else:
            await self._request(
                "POST",
                f"/connectors/{connector_id}/user/credentials",
                {
                    "name": credentials_name,
                    "credentials": {"headers": {}},
                    "is_default": True,
                },
            )

        tools = await self._request("GET", f"/connectors/{connector_id}/tools")
        if not isinstance(tools, list):
            raise RuntimeError("Mistral connector tool discovery returned an unexpected response")

        agent = await self._request("GET", f"/agents/{self.agent_id}")
        current_tools = agent.get("tools") or []
        attached = any(
            isinstance(tool, dict)
            and tool.get("type") == "connector"
            and str(tool.get("connector_id") or "") in {connector_id, name}
            for tool in current_tools
        )
        if not attached:
            current_tools = [
                tool for tool in current_tools
                if not (
                    isinstance(tool, dict)
                    and tool.get("type") == "connector"
                    and str(tool.get("connector_id") or "") == name
                )
            ]
            agent = await self._request(
                "PATCH",
                f"/agents/{self.agent_id}",
                {"tools": [*current_tools, {"type": "connector", "connector_id": connector_id}]},
            )

        return {
            "ok": True,
            "connector_id": connector_id,
            "name": connector.get("name") or name,
            "server": server,
            "tool_count": len(tools),
            "agent_version": agent.get("version"),
            "attached": True,
        }

    async def delete_conversation(self, conversation_id: str) -> None:
        try:
            await self._request("DELETE", f"/conversations/{conversation_id}")
        except MistralConversationNotFound:
            pass

    async def health(self) -> dict[str, Any]:
        if not self.configured:
            raise RuntimeError("Mistral agent is not fully configured")
        version = self.agent_version
        if version is None:
            path = f"/agents/{self.agent_id}"
        else:
            path = f"/agents/{self.agent_id}/versions/{version}"
        data = await self._request("GET", path)
        return {
            "ok": True,
            "agent_id": data.get("id") or self.agent_id,
            "name": data.get("name"),
            "model": data.get("model"),
            "version": version,
        }
