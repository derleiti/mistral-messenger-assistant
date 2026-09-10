from app.media import clip_context, detect_input


def test_photo_detection():
    item = detect_input({
        "caption": "Was ist kaputt?",
        "photo": [
            {"file_id": "small", "file_size": 20},
            {"file_id": "large", "file_size": 200},
        ],
    })
    assert item.kind == "image"
    assert item.file_id == "large"
    assert item.prompt == "Was ist kaputt?"


def test_document_routing():
    assert detect_input({"document": {"file_id": "1", "file_name": "x.pdf", "mime_type": "application/pdf"}}).kind == "document"
    assert detect_input({"document": {"file_id": "1", "file_name": "x.py", "mime_type": "text/x-python"}}).kind == "text_file"
    assert detect_input({"document": {"file_id": "1", "file_name": "x.png", "mime_type": "application/octet-stream"}}).kind == "image"
    assert detect_input({"document": {"file_id": "1", "file_name": "x.ogg", "mime_type": "application/octet-stream"}}).kind == "audio"


def test_voice_and_text():
    assert detect_input({"voice": {"file_id": "v"}}).kind == "audio"
    text = detect_input({"text": "hi"})
    assert text.kind == "text" and text.prompt == "hi"


def test_clip_context():
    text = "A" * 1000
    clipped = clip_context(text, 200)
    assert len(clipped) > 200
    assert "gekürzt" in clipped
