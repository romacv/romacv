import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from contract import ContractError, check_media_present, referenced_files, valid_slug, validate_post


def post(**changes):
    base = {
        "slug": "first-post",
        "title": "First post",
        "description": "A first post.",
        "publishedAt": "2026-11-02T10:00:00Z",
        "cover": "cover.jpg",
        "body": "Hello.",
        "tags": ["swift"],
    }
    return {**base, **changes}


def test_valid_post_passes():
    data = post(images=[{"src": "1.jpg", "alt": "Frame", "width": 1080, "height": 1350}], youtubeId="dQw4w9WgXcQ")
    assert validate_post(data, "first-post") == data


@pytest.mark.parametrize("changes", [
    {"slug": "other"},
    {"extra": 1},
    {"cover": "photo.png"},
    {"tags": "swift"},
    {"publishedAt": "yesterday"},
    {"youtubeFormat": "reel"},
    {"images": [{"src": "1.jpg", "alt": "Frame", "width": "wide"}]},
    {"durationSeconds": True},
])
def test_invalid_post_is_rejected(changes):
    with pytest.raises(ContractError):
        validate_post(post(**changes), "first-post")


def test_slug_pattern():
    assert valid_slug("a-b-1")
    assert not valid_slug("A-b")
    assert not valid_slug("a--b")


@pytest.mark.parametrize("slug", ["index", "feed", "sitemap", "llms", "blog"])
def test_reserved_slugs_are_rejected(slug):
    assert not valid_slug(slug)


def test_missing_media_names_the_file():
    data = post(images=[{"src": "2.jpg", "alt": "Frame"}])
    assert referenced_files(data) == ["cover.jpg", "2.jpg"]
    with pytest.raises(ContractError) as error:
        check_media_present(data, {"cover.jpg": {}})
    assert "2.jpg" in str(error.value)
    check_media_present(data, {"cover.jpg": {}, "2.jpg": {}})


def test_missing_inline_media_is_reported():
    data = post(body="Text.\n\n![Frame](3.jpg)", images=[{"src": "3.jpg", "alt": "Frame", "width": 800, "height": 1000}])
    assert referenced_files(data) == ["cover.jpg", "3.jpg"]
    validate_post(data, "first-post")
    with pytest.raises(ContractError, match="3.jpg"):
        check_media_present(data, {"cover.jpg": {}})


@pytest.mark.parametrize("body", [
    "![x](https://example.com/a.jpg)",
    "![x](../1.jpg)",
    "![x](notes.png)",
    "See ![x](1.jpg) here.",
])
def test_inline_image_must_be_a_whole_line_media_file(body):
    with pytest.raises(ContractError):
        validate_post(post(body=body), "first-post")


def test_inline_image_without_an_images_entry_is_rejected():
    with pytest.raises(ContractError, match="no images entry"):
        validate_post(post(body="![Frame](1.jpg)"), "first-post")


def test_inline_image_without_dimensions_is_rejected():
    data = post(body="![Frame](1.jpg)", images=[{"src": "1.jpg", "alt": "Frame", "width": 800}])
    with pytest.raises(ContractError, match="width and height"):
        validate_post(data, "first-post")
