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


def test_workspace_pair_is_rebound_for_followup_turns(monkeypatch):
    import app.main as main
    main._workspace_pairs.clear()
    monkeypatch.setattr(main.settings, "mistral_mcp_enabled", True)
    code = "1A2B-3C4D-5E6F-7788-99AA-BBCC"
    first = main._mcp_hardened_prompt("42", f"connect {code}")
    second = main._mcp_hardened_prompt("42", "zeige die dateien")
    assert code in first
    assert "workspace_status" in first
    assert "workspace_id" in first
    assert code in second
    assert "workspace_status" in second
    assert "state=suspended" in second
    assert "access_mode" in second
    assert "shared_workspace" in second
    assert second.endswith("zeige die dateien")


def test_workspace_pair_is_scoped_per_chat_and_expires(monkeypatch):
    import app.main as main
    main._workspace_pairs.clear()
    monkeypatch.setattr(main.settings, "mistral_mcp_enabled", True)
    code = "AAAA-BBBB-CCCC-DDDD-EEEE-FFFF"
    main._mcp_hardened_prompt("chat-a", f"pair {code}")
    assert main._active_workspace_pair("chat-a") == code
    assert main._active_workspace_pair("chat-b") is None
    main._workspace_pairs["chat-a"] = (code, 0.0)
    assert main._active_workspace_pair("chat-a") is None


def test_workspace_pair_does_not_change_prompt_when_mcp_disabled(monkeypatch):
    import app.main as main
    main._workspace_pairs.clear()
    monkeypatch.setattr(main.settings, "mistral_mcp_enabled", False)
    prompt = "pair 1111-2222-3333-4444-5555-6666"
    assert main._mcp_hardened_prompt("42", prompt) == prompt
