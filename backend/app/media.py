from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path


IMAGE_EXTENSIONS = {".png", ".jpg", ".jpeg", ".webp", ".gif"}
AUDIO_EXTENSIONS = {".ogg", ".oga", ".mp3", ".wav", ".flac", ".webm", ".m4a", ".aac"}
OCR_EXTENSIONS = {
    ".pdf", ".doc", ".docx", ".ppt", ".pptx", ".odt", ".rtf", ".epub",
}
TEXT_EXTENSIONS = {
    ".txt", ".md", ".markdown", ".rst", ".tex", ".csv", ".tsv",
    ".json", ".jsonl", ".xml", ".yaml", ".yml", ".toml", ".ini", ".conf",
    ".log", ".py", ".pyi", ".js", ".jsx", ".ts", ".tsx", ".sh", ".bash",
    ".zsh", ".fish", ".sql", ".html", ".htm", ".css", ".scss", ".c", ".h",
    ".cc", ".cpp", ".hpp", ".java", ".go", ".rs", ".rb", ".php",
}


@dataclass(frozen=True)
class DetectedInput:
    kind: str
    prompt: str
    file_id: str = ""
    file_name: str = ""
    mime_type: str = ""
    file_size: int = 0


def _safe_size(value) -> int:
    try:
        return max(0, int(value or 0))
    except (TypeError, ValueError):
        return 0


def _document_kind(file_name: str, mime_type: str) -> str:
    ext = Path(file_name).suffix.lower()
    mime = (mime_type or "").lower().split(";", 1)[0].strip()

    if mime.startswith("image/") or ext in IMAGE_EXTENSIONS:
        return "image"
    if mime.startswith("audio/") or ext in AUDIO_EXTENSIONS:
        return "audio"
    if mime.startswith("text/") or ext in TEXT_EXTENSIONS:
        return "text_file"
    if ext in OCR_EXTENSIONS or mime in {
        "application/pdf",
        "application/msword",
        "application/rtf",
        "application/vnd.oasis.opendocument.text",
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document",
        "application/vnd.ms-powerpoint",
        "application/vnd.openxmlformats-officedocument.presentationml.presentation",
        "application/epub+zip",
    }:
        return "document"
    return "unsupported"


def detect_input(message: dict) -> DetectedInput:
    text = (message.get("text") or "").strip()
    caption = (message.get("caption") or "").strip()

    photos = message.get("photo") or []
    if photos:
        photo = photos[-1]
        return DetectedInput(
            kind="image",
            prompt=caption or "Analysiere dieses Bild und beschreibe die relevanten Details.",
            file_id=str(photo.get("file_id") or ""),
            file_name="telegram-photo.jpg",
            mime_type="image/jpeg",
            file_size=_safe_size(photo.get("file_size")),
        )

    voice = message.get("voice")
    if isinstance(voice, dict):
        return DetectedInput(
            kind="audio",
            prompt=caption or "Beantworte den Inhalt dieser Sprachnachricht.",
            file_id=str(voice.get("file_id") or ""),
            file_name="telegram-voice.ogg",
            mime_type=str(voice.get("mime_type") or "audio/ogg"),
            file_size=_safe_size(voice.get("file_size")),
        )

    audio = message.get("audio")
    if isinstance(audio, dict):
        return DetectedInput(
            kind="audio",
            prompt=caption or "Analysiere den Inhalt dieser Audiodatei und antworte passend.",
            file_id=str(audio.get("file_id") or ""),
            file_name=str(audio.get("file_name") or "telegram-audio"),
            mime_type=str(audio.get("mime_type") or "application/octet-stream"),
            file_size=_safe_size(audio.get("file_size")),
        )

    document = message.get("document")
    if isinstance(document, dict):
        file_name = str(document.get("file_name") or "telegram-file")
        mime_type = str(document.get("mime_type") or "application/octet-stream")
        kind = _document_kind(file_name, mime_type)
        defaults = {
            "image": "Analysiere dieses Bild und beschreibe die relevanten Details.",
            "audio": "Analysiere den Inhalt dieser Audiodatei und antworte passend.",
            "text_file": "Analysiere diese Datei und fasse die relevanten Inhalte zusammen.",
            "document": "Analysiere dieses Dokument und fasse die relevanten Inhalte zusammen.",
            "unsupported": "",
        }
        return DetectedInput(
            kind=kind,
            prompt=caption or defaults[kind],
            file_id=str(document.get("file_id") or ""),
            file_name=file_name,
            mime_type=mime_type,
            file_size=_safe_size(document.get("file_size")),
        )

    if text:
        return DetectedInput(kind="text", prompt=text)

    return DetectedInput(kind="unsupported", prompt="")


def clip_context(text: str, max_chars: int) -> str:
    if max_chars <= 0 or len(text) <= max_chars:
        return text
    if max_chars < 200:
        return text[:max_chars]
    head = int(max_chars * 0.8)
    tail = max_chars - head
    return (
        text[:head]
        + "\n\n[... Inhalt wegen Kontextlimit gekürzt ...]\n\n"
        + text[-tail:]
    )
