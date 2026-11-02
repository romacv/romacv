import json
import os
import sys
import urllib.request

from datetime import datetime
from pathlib import Path

from google.api_core.exceptions import FailedPrecondition, NotFound
from google.cloud import firestore
from google.cloud.firestore import DELETE_FIELD

PROJECT = "resromcom-493b4"
SITE_URL = "https://www.resrom.com"
INDEXNOW_KEY = "ac25f5f6ffaae4eb1c7955b74136455b"
INDEXNOW_URL = "https://api.indexnow.org/indexnow"
RENDERED_PENDING = Path(__file__).with_name(".rendered-pending.json")


def ping_indexnow(urls):
    body = json.dumps({"host": "www.resrom.com", "key": INDEXNOW_KEY, "keyLocation": f"{SITE_URL}/{INDEXNOW_KEY}.txt", "urlList": urls})
    request = urllib.request.Request(INDEXNOW_URL, data=body.encode(), headers={"Content-Type": "application/json; charset=utf-8"})
    urllib.request.urlopen(request, timeout=10).close()


def main():
    if os.environ.get("GITHUB_EVENT_NAME") == "pull_request":
        return
    if not RENDERED_PENDING.is_file():
        return
    rendered = json.loads(RENDERED_PENDING.read_text())
    RENDERED_PENDING.unlink()
    if not rendered:
        return
    db = firestore.Client(project=PROJECT)
    for slug, update_time in rendered.items():
        option = db.write_option(last_update_time=datetime.fromisoformat(update_time))
        try:
            db.collection("blog").document(slug).update({"publishPending": DELETE_FIELD}, option=option)
        except (FailedPrecondition, NotFound):
            pass
    ping_indexnow([f"{SITE_URL}/blog", *(f"{SITE_URL}/blog/{slug}" for slug in rendered)])


if __name__ == "__main__":
    try:
        main()
    except Exception as error:
        print(f"after_deploy failed: {error}", file=sys.stderr)
