import json
import sys
from pathlib import Path
from unittest.mock import MagicMock

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blog_cli import put
from contract import MAX_MEDIA_BYTES, ContractError

POST = {
    "slug": "first-post",
    "title": "First post",
    "description": "A first post.",
    "publishedAt": "2026-11-02T10:00:00Z",
    "cover": "cover.jpg",
    "body": "Hello.",
    "tags": ["swift"],
}


def mocked_db():
    db = MagicMock()
    reference = db.collection.return_value.document.return_value
    reference.collection.return_value.select.return_value.stream.return_value = []
    return db, reference


def test_put_refuses_a_missing_media_file(tmp_path):
    (tmp_path / "post.json").write_text(json.dumps(POST))
    db, reference = mocked_db()
    with pytest.raises(ContractError, match="cover.jpg"):
        put(tmp_path, db)
    reference.set.assert_not_called()


def test_put_refuses_an_oversized_media_file(tmp_path):
    (tmp_path / "post.json").write_text(json.dumps(POST))
    (tmp_path / "cover.jpg").write_bytes(b"x" * (MAX_MEDIA_BYTES + 1))
    db, reference = mocked_db()
    with pytest.raises(ContractError, match="900 KB"):
        put(tmp_path, db)
    reference.set.assert_not_called()
