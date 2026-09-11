import asyncio
import tempfile
from pathlib import Path

from app.db import Database
from app.mistral import MistralClient, MistralConversationNotFound


class FakeMistral(MistralClient):
    def __init__(self):
        super().__init__("test-key", "agent-123", 0)
        self.calls = []

    async def _request(self, method, path, payload=None):
        self.calls.append((method, path, payload))
        if path == "/conversations":
            return {
                "conversation_id": "conv-1",
                "outputs": [{"role": "assistant", "content": "Hallo Markus"}],
                "usage": {"total_tokens": 42},
            }
        if path == "/conversations/conv-1":
            return {
                "conversation_id": "conv-1",
                "outputs": [{"content": [{"type": "text", "text": "Kontext lebt"}]}],
                "usage": {},
            }
        raise MistralConversationNotFound(path)


def test_create_and_append():
    async def run():
        client = FakeMistral()
        first = await client.ask("Hallo", metadata={"source": "telegram"})
        assert first.text == "Hallo Markus"
        assert first.conversation_id == "conv-1"
        second = await client.ask("Weisst du noch?", conversation_id=first.conversation_id)
        assert second.text == "Kontext lebt"
        assert client.calls[0][1] == "/conversations"
        assert client.calls[0][2]["agent_id"] == "agent-123"
        assert client.calls[1][1] == "/conversations/conv-1"
    asyncio.run(run())


def test_database_conversation_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        db = Database(str(Path(td) / "bridge.sqlite3"))
        assert db.get_mistral_conversation("42") is None
        db.set_mistral_conversation("42", "conv-x")
        assert db.get_mistral_conversation("42") == "conv-x"
        assert db.count_mistral_conversations() == 1
        db.clear_mistral_conversation("42")
        assert db.get_mistral_conversation("42") is None


def test_extract_text_filters_reasoning_and_selects_final_message():
    data = {
        "outputs": [
            {
                "type": "message.output",
                "role": "assistant",
                "content": [
                    {"type": "thinking", "thinking": "internal reasoning"},
                    {"type": "text", "text": "draft"},
                ],
            },
            {"type": "function.call", "name": "tool", "arguments": "{}"},
            {
                "type": "message.output",
                "role": "assistant",
                "content": [{"type": "text", "text": "final answer"}],
            },
        ]
    }
    assert MistralClient._extract_text(data) == "final answer"
    assert "internal reasoning" not in MistralClient._extract_text(data)


def test_database_workspace_lease_roundtrip():
    with tempfile.TemporaryDirectory() as td:
        db = Database(str(Path(td) / "bridge.sqlite3"))
        assert db.get_workspace_lease("42") is None
        db.set_workspace_lease("42", "secret-token", "lease-1", "write", ["file_read", "code_edit"])
        lease = db.get_workspace_lease("42")
        assert lease is not None
        assert lease["workspace_token"] == "secret-token"
        assert lease["lease_id"] == "lease-1"
        assert lease["access_mode"] == "write"
        assert lease["capabilities"] == ["file_read", "code_edit"]
        db.clear_workspace_lease("42")
        assert db.get_workspace_lease("42") is None


def test_workspace_prompt_uses_verified_persistent_token(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "mistral_mcp_enabled", True)
    workspace = {
        "workspace_token": "durable-secret",
        "lease_id": "lease-x",
        "access_mode": "write",
        "capabilities": ["file_read", "code_edit"],
        "state": "ready",
        "transport_state": "offline",
    }
    prompt = main._mcp_hardened_prompt("zeige die dateien", workspace)
    assert "durable-secret" in prompt
    assert "Do not ask for a pairing ID" in prompt
    assert "state=ready" in prompt
    assert "access_mode=write" in prompt
    assert "transport_state=offline" in prompt
    assert prompt.endswith("zeige die dateien")


def test_workspace_pair_does_not_change_prompt_when_mcp_disabled(monkeypatch):
    import app.main as main
    monkeypatch.setattr(main.settings, "mistral_mcp_enabled", False)
    prompt = "pair 1111-2222-3333-4444-5555-6666"
    assert main._mcp_hardened_prompt(prompt, {"workspace_token": "x"}) == prompt


def test_ensure_default_mcp_refreshes_connector_tools():
    class FakeConnectorMistral(MistralClient):
        def __init__(self):
            super().__init__("test-key", "agent-123", 0)
            self.calls = []

        async def _request(self, method, path, payload=None):
            self.calls.append((method, path, payload))
            if path == "/connectors?page_size=100":
                return {"items": [{
                    "id": "conn-1",
                    "name": "ailinux",
                    "server": "https://api.ailinux.me/v1/mcp",
                    "description": "old",
                }]}
            if path == "/connectors/conn-1":
                return {
                    "id": "conn-1",
                    "name": "ailinux",
                    "server": "https://api.ailinux.me/v1/mcp",
                    "description": payload.get("description", "") if payload else "",
                }
            if path in {
                "/connectors/conn-1/workspace/activate",
                "/connectors/conn-1/workspace/credentials",
            }:
                return {"ok": True}
            if path == "/connectors/conn-1/tools?refresh=true":
                return [{"name": "file_ops"}, {"name": "workspace_clear"}]
            if path == "/agents/agent-123":
                return {"version": 7, "tools": [{"type": "connector", "connector_id": "conn-1"}]}
            raise AssertionError((method, path, payload))

    async def run():
        client = FakeConnectorMistral()
        result = await client.ensure_default_mcp(
            "ailinux",
            "https://api.ailinux.me/v1/mcp",
        )
        assert result["tool_count"] == 2
        assert any(path == "/connectors/conn-1/tools?refresh=true" for _m, path, _p in client.calls)

    asyncio.run(run())


def test_default_database_path_is_writable_outside_container(monkeypatch, tmp_path):
    from app import config
    monkeypatch.setenv("XDG_DATA_HOME", str(tmp_path))
    monkeypatch.setattr(config.os, "access", lambda *_: False)
    assert config._default_database_path() == str(tmp_path / "mistral-messenger-assistant" / "mistral_messenger.sqlite3")
