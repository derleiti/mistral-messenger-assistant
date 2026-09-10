from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

import httpx


@dataclass(frozen=True)
class WorkspaceLease:
    workspace_token: str
    lease_id: str
    access_mode: str
    capabilities: tuple[str, ...]
    state: str
    transport_state: str


class WorkspaceHandoffClient:
    """Minimal MCP client used by the Telegram bridge to own workspace handoff state.

    The Mistral model is not responsible for remembering the bootstrap pairing ID.
    The bridge claims the lease itself and persists the durable workspace token.
    """

    def __init__(self, server_url: str, timeout: float = 20.0):
        self.server_url = str(server_url or "").rstrip("/")
        self.timeout = timeout

    @staticmethod
    def _decode_rpc_body(response: httpx.Response) -> dict[str, Any]:
        text = response.text.strip()
        if not text:
            return {}
        if text.startswith("event:") or "\ndata:" in text:
            payloads = [line[5:].strip() for line in text.splitlines() if line.startswith("data:")]
            if payloads:
                text = payloads[-1]
        data = json.loads(text)
        if not isinstance(data, dict):
            raise RuntimeError("TriForce MCP returned a non-object response")
        return data

    async def _rpc(
        self,
        method: str,
        params: dict[str, Any],
        *,
        session_id: str | None = None,
    ) -> tuple[dict[str, Any], str | None]:
        if not self.server_url:
            raise RuntimeError("TriForce MCP server URL is not configured")
        headers = {
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
            "MCP-Protocol-Version": "2025-03-26",
            "User-Agent": "nova-telegram-bridge/workspace-handoff",
        }
        if session_id:
            headers["Mcp-Session-Id"] = session_id
        body = {"jsonrpc": "2.0", "id": 1, "method": method, "params": params}
        async with httpx.AsyncClient(timeout=self.timeout, follow_redirects=True) as client:
            response = await client.post(self.server_url, headers=headers, json=body)
        if response.status_code >= 400:
            raise RuntimeError(f"TriForce MCP HTTP {response.status_code}")
        data = self._decode_rpc_body(response)
        if isinstance(data.get("error"), dict):
            raise RuntimeError(str(data["error"].get("message") or "TriForce MCP error"))
        result = data.get("result")
        if not isinstance(result, dict):
            raise RuntimeError("TriForce MCP returned no result object")
        return result, response.headers.get("Mcp-Session-Id") or session_id

    async def _initialize(self) -> str:
        result, session_id = await self._rpc(
            "initialize",
            {
                "protocolVersion": "2025-03-26",
                "capabilities": {},
                "clientInfo": {"name": "nova-telegram-bridge", "version": "1"},
            },
        )
        if not session_id:
            raise RuntimeError("TriForce MCP did not return a session id")
        return session_id

    @staticmethod
    def _lease_from_tool_result(result: dict[str, Any], *, require_token: bool) -> WorkspaceLease:
        structured = result.get("structuredContent")
        if not isinstance(structured, dict):
            structured = {}
        if not structured.get("ok"):
            code = str(structured.get("code") or "WORKSPACE_CLAIM_FAILED")
            raise RuntimeError(code)
        token = str(structured.get("workspace_token") or "")
        if require_token and not token:
            raise RuntimeError("TriForce workspace claim returned no durable token")
        return WorkspaceLease(
            workspace_token=token,
            lease_id=str(structured.get("lease_id") or ""),
            access_mode=str(structured.get("access_mode") or structured.get("mode") or "read_only"),
            capabilities=tuple(str(x) for x in (structured.get("capabilities") or []) if str(x)),
            state=str(structured.get("state") or "ready"),
            transport_state=str(structured.get("transport_state") or "offline"),
        )

    async def claim_pair_code(self, pair_code: str) -> WorkspaceLease:
        session_id = await self._initialize()
        result, _ = await self._rpc(
            "tools/call",
            {"name": "workspace_status", "arguments": {"workspace_id": str(pair_code).strip().upper()}},
            session_id=session_id,
        )
        return self._lease_from_tool_result(result, require_token=True)

    async def status(self, workspace_token: str) -> WorkspaceLease:
        session_id = await self._initialize()
        result, _ = await self._rpc(
            "tools/call",
            {"name": "workspace_status", "arguments": {"workspace_token": workspace_token}},
            session_id=session_id,
        )
        lease = self._lease_from_tool_result(result, require_token=False)
        if not lease.workspace_token:
            lease = WorkspaceLease(
                workspace_token=workspace_token,
                lease_id=lease.lease_id,
                access_mode=lease.access_mode,
                capabilities=lease.capabilities,
                state=lease.state,
                transport_state=lease.transport_state,
            )
        return lease
