from types import SimpleNamespace

from src.utils import media_kind, normalize_query


def test_normalize_query_handles_file_names() -> None:
    assert normalize_query("My_Show.S01E01_1080p") == "My Show S01E01 1080p"


def test_media_kind_detects_books_and_apps() -> None:
    pdf = SimpleNamespace(document=SimpleNamespace(file_name="notes.pdf"), video=None, audio=None, photo=None)
    apk = SimpleNamespace(document=SimpleNamespace(file_name="reader.apk"), video=None, audio=None, photo=None)
    assert media_kind(pdf) == "📚 Book / Document"
    assert media_kind(apk) == "🛠 App / Tool"
