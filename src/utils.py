import re
from typing import Any


def normalize_query(value: str) -> str:
    """Normalize user input so titles and filenames search consistently."""
    return re.sub(r"\s+", " ", re.sub(r"[_\-.]+", " ", value)).strip()


def media_kind(message: Any) -> str:
    if getattr(message, "video", None):
        return "🎬 Video"
    if getattr(message, "audio", None):
        return "🎵 Audio"
    if getattr(message, "photo", None):
        return "🖼 Image"
    document = getattr(message, "document", None)
    if document:
        name = (document.file_name or "").lower()
        if name.endswith((".apk", ".exe", ".msi", ".dmg", ".zip", ".rar", ".7z")):
            return "🛠 App / Tool"
        if name.endswith((".pdf", ".epub", ".mobi", ".cbz")):
            return "📚 Book / Document"
    return "📄 File"


def message_file(message: Any) -> tuple[str | None, str | None]:
    """Return Telegram file ID and a searchable display name for supported media."""
    if getattr(message, "document", None):
        return message.document.file_id, message.document.file_name
    if getattr(message, "video", None):
        return message.video.file_id, message.video.file_name or "video"
    if getattr(message, "audio", None):
        return message.audio.file_id, message.audio.file_name or message.audio.title or "audio"
    if getattr(message, "animation", None):
        return message.animation.file_id, message.animation.file_name or "animation"
    if getattr(message, "photo", None):
        return message.photo[-1].file_id, "image"
    return None, None
