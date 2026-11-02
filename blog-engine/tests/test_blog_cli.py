import json
import sys
from pathlib import Path
from unittest.mock import MagicMock, patch

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from blog_cli import announce, put
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


def test_announce_dispatches_the_deploy_workflow(monkeypatch, capsys):
    monkeypatch.setenv("BLOG_DEPLOY_TOKEN", "secret-token")
    response = MagicMock(status=204)
    response.__enter__.return_value = response
    response.read.return_value = b""
    with patch("blog_cli.urllib.request.urlopen", return_value=response) as urlopen:
        announce("first-post")
    request = urlopen.call_args.args[0]
    assert request.full_url.endswith("/actions/workflows/firebase-hosting-merge.yml/dispatches")
    assert request.get_method() == "POST"
    assert json.loads(request.data) == {"ref": "main"}
    assert request.get_header("Authorization") == "Bearer secret-token"
    output = capsys.readouterr().out
    assert "deploy triggered" in output and "secret-token" not in output


def test_announce_without_a_token_only_reports_it(monkeypatch, tmp_path, capsys):
    monkeypatch.delenv("BLOG_DEPLOY_TOKEN", raising=False)
    monkeypatch.setattr("blog_cli.TOKEN_FILE", tmp_path / "missing")
    with patch("blog_cli.urllib.request.urlopen") as urlopen:
        announce("first-post")
    urlopen.assert_not_called()
    assert "deploy not triggered (no token)" in capsys.readouterr().out
