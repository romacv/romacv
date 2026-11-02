import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contract import sha256_hex
from render_all import jpeg_size, load_posts


def post(slug):
    return {
        "slug": slug,
        "title": "A post",
        "description": "About it.",
        "publishedAt": "2026-11-02T10:00:00Z",
        "cover": "cover.jpg",
        "body": "Hello.",
        "tags": ["swift"],
        "status": "published",
        "publishPending": True,
    }


def snapshot(slug, media):
    document = MagicMock(id=slug)
    document.to_dict.return_value = post(slug)
    files = []
    for name, body in media.items():
        item = MagicMock(id=name)
        item.to_dict.return_value = {"data": body, "sha256": sha256_hex(body)}
        files.append(item)
    document.reference.collection.return_value.stream.return_value = files
    return document


def test_post_with_a_missing_media_file_is_skipped(capsys):
    db = MagicMock()
    db.collection.return_value.where.return_value.stream.return_value = [
        snapshot("good-post", {"cover.jpg": b"jpg"}),
        snapshot("bad-post", {}),
    ]
    posts, media = load_posts(db)
    assert [item["slug"] for item in posts] == ["good-post"]
    assert media == {"good-post": {"cover.jpg": b"jpg"}}
    assert "skipped bad-post: missing media: cover.jpg" in capsys.readouterr().err


def test_jpeg_size_reads_the_start_of_frame_header():
    app0 = b"\xff\xe0\x00\x04\x00\x00"
    frame = b"\xff\xc0\x00\x0b\x08\x03\xe8\x02\x58\x01\x01\x11\x00"
    assert jpeg_size(b"\xff\xd8" + app0 + frame) == (600, 1000)
    assert jpeg_size(b"not a jpeg") is None
