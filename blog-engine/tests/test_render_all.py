import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from render_all import load_posts


def post(slug, **changes):
    base = {
        "slug": slug,
        "title": "A post",
        "description": "About it.",
        "publishedAt": "2026-11-02T10:00:00Z",
        "cover": "cover.jpg",
        "body": "Hello.",
        "tags": ["swift"],
    }
    return {**base, **changes}


def test_post_with_a_missing_media_file_is_skipped(tmp_path, capsys):
    good = tmp_path / "good-post"
    good.mkdir()
    (good / "post.json").write_text(json.dumps(post("good-post")))
    (good / "cover.jpg").write_bytes(b"jpg")
    bad = tmp_path / "bad-post"
    bad.mkdir()
    (bad / "post.json").write_text(json.dumps(post("bad-post")))
    posts, media = load_posts(tmp_path)
    assert [item["slug"] for item in posts] == ["good-post"]
    assert media == {"good-post": {"cover.jpg"}}
    assert "skipped bad-post: missing media: cover.jpg" in capsys.readouterr().err
