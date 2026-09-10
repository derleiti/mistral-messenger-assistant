from app.mistral import MistralClient


def test_extract_text_uses_only_last_assistant_message():
    data = {
        "outputs": [
            {"type": "tool.execution", "content": "internal tool output"},
            {"type": "message.output", "role": "assistant", "content": "draft one"},
            {"type": "message.output", "role": "assistant", "content": "final answer"},
        ]
    }
    assert MistralClient._extract_text(data) == "final answer"


def test_extract_text_ignores_non_assistant_and_unknown_output_types():
    data = {
        "outputs": [
            {"type": "message.output", "role": "user", "content": "user text"},
            {"type": "reasoning", "role": "assistant", "content": "private reasoning"},
            {"type": "message.output", "role": "assistant", "content": "visible"},
        ]
    }
    assert MistralClient._extract_text(data) == "visible"
