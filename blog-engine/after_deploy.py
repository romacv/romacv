import json
import os
import sys
import urllib.request
from datetime import datetime, timedelta, timezone
from pathlib import Path

from contract import ContractError, valid_slug, validate_post

SITE_URL = "https://www.resrom.com"
INDEXNOW_KEY = "ac25f5f6ffaae4eb1c7955b74136455b"
INDEXNOW_URL = "https://api.indexnow.org/indexnow"
POSTS_DIR = Path(__file__).resolve().parent.parent / "blog-posts"
RECENT = timedelta(days=2)


def moment(value):
    parsed = datetime.fromisoformat(value)
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def recent_slugs():
    cutoff = datetime.now(timezone.utc) - RECENT
    slugs = []
    for path in sorted(POSTS_DIR.glob("*/post.json")):
        try:
            slug = path.parent.name
            post = validate_post(json.loads(path.read_text()), slug) if valid_slug(slug) else None
            if post is None:
                continue
            if max(moment(post[key]) for key in ("publishedAt", "modifiedAt") if key in post) >= cutoff:
                slugs.append(slug)
        except (OSError, ValueError, KeyError, ContractError):
            continue
    return slugs


def main():
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        return
    urls = [f"{SITE_URL}/blog", *(f"{SITE_URL}/blog/{slug}" for slug in recent_slugs())]
    body = json.dumps({"host": "www.resrom.com", "key": INDEXNOW_KEY, "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt", "urlList": urls})
    request = urllib.request.Request(INDEXNOW_URL, data=body.encode(), headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(request, timeout=10).close()


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"after_deploy failed: {error}", file=sys.stderr)
