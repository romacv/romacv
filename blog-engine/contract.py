import re
from datetime import datetime

MEDIA_FILES = (
    "cover.jpg",
    *(f"{number}.jpg" for number in range(1, 21)),
)
SLUG_PATTERN = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
RESERVED_SLUGS = ("index", "feed", "sitemap", "llms", "blog")
YOUTUBE_ID_PATTERN = re.compile(r"^[A-Za-z0-9_-]{6,32}$")
MAX_MEDIA_BYTES = 25 * 1024 * 1024
POST_FIELDS = {
    "slug", "title", "description", "publishedAt", "modifiedAt", "cover", "body", "tags",
    "youtubeId", "youtubeFormat", "durationSeconds", "transcript", "images",
}
REQUIRED_FIELDS = ("slug", "title", "description", "publishedAt", "cover", "body", "tags")
IMAGE_FIELDS = {"src", "alt", "width", "height"}


class ContractError(Exception):
    pass


def valid_slug(slug):
    return isinstance(slug, str) and bool(SLUG_PATTERN.match(slug)) and slug not in RESERVED_SLUGS


def valid_media_file(file):
    return file in MEDIA_FILES


def valid_image_file(file):
    return file in MEDIA_FILES and file.endswith(".jpg")


def parse_iso(value):
    return datetime.fromisoformat(value)


def is_text(value, allow_empty=False):
    return isinstance(value, str) and (allow_empty or value.strip() != "")


def is_count(value):
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def check_iso(value, name):
    if not is_text(value):
        raise ContractError(f"{name} must be an ISO date string")
    try:
        parse_iso(value)
    except ValueError:
        raise ContractError(f"{name} must be an ISO date string")


def check_image(image):
    if not isinstance(image, dict):
        raise ContractError("images items must be objects")
    unknown = set(image) - IMAGE_FIELDS
    if unknown:
        raise ContractError(f"unknown image field: {sorted(unknown)[0]}")
    if not valid_image_file(image.get("src")):
        raise ContractError("images[].src must be a .jpg media file name")
    if not isinstance(image.get("alt"), str):
        raise ContractError("images[].alt must be a string")
    for key in ("width", "height"):
        if key in image and not is_count(image[key]):
            raise ContractError(f"images[].{key} must be a positive integer")


def validate_post(data, slug):
    if not isinstance(data, dict):
        raise ContractError("post must be a JSON object")
    unknown = set(data) - POST_FIELDS
    if unknown:
        raise ContractError(f"unknown field: {sorted(unknown)[0]}")
    for name in REQUIRED_FIELDS:
        if name not in data:
            raise ContractError(f"missing field: {name}")
    if data["slug"] != slug:
        raise ContractError("slug must equal the path slug")
    for name in ("title", "description"):
        if not is_text(data[name]):
            raise ContractError(f"{name} must be a non-empty string")
    if not isinstance(data["body"], str):
        raise ContractError("body must be a string")
    check_iso(data["publishedAt"], "publishedAt")
    if "modifiedAt" in data:
        check_iso(data["modifiedAt"], "modifiedAt")
    if not valid_image_file(data["cover"]):
        raise ContractError("cover must be a .jpg media file name")
    tags = data["tags"]
    if not isinstance(tags, list) or not all(is_text(tag) for tag in tags):
        raise ContractError("tags must be a list of strings")
    if "youtubeId" in data and not (isinstance(data["youtubeId"], str) and YOUTUBE_ID_PATTERN.match(data["youtubeId"])):
        raise ContractError("youtubeId is not a valid YouTube id")
    if "youtubeFormat" in data and data["youtubeFormat"] not in ("video", "short"):
        raise ContractError("youtubeFormat must be video or short")
    if "durationSeconds" in data and not is_count(data["durationSeconds"]):
        raise ContractError("durationSeconds must be a positive integer")
    if "transcript" in data and not isinstance(data["transcript"], str):
        raise ContractError("transcript must be a string")
    if "images" in data:
        if not isinstance(data["images"], list):
            raise ContractError("images must be a list")
        for image in data["images"]:
            check_image(image)
    return data


def referenced_files(post):
    files = [post["cover"]]
    files.extend(image["src"] for image in post.get("images", []))
    return list(dict.fromkeys(files))


def check_media_present(post, media):
    for file in referenced_files(post):
        if file not in media:
            raise ContractError(f"missing media: {file}")
