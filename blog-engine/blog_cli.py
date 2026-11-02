import argparse
import json
import sys
from pathlib import Path

from google.cloud import firestore

from contract import (
    MAX_MEDIA_BYTES, ContractError, check_media_present, sha256_hex, valid_media_file, valid_slug, validate_post,
)

PROJECT = "resromcom-493b4"
POST_FILE = "post.json"


def announce(slug):
    print(f"https://www.resrom.com/blog/{slug}")
    print("live within 15 minutes")


def put(directory, db):
    directory = Path(directory)
    post_path = directory / POST_FILE
    if not post_path.is_file():
        raise ContractError(f"{POST_FILE} not found in {directory}")
    data = json.loads(post_path.read_text())
    slug = data.get("slug") if isinstance(data, dict) else None
    if not valid_slug(slug):
        raise ContractError("invalid slug")
    post = validate_post(data, slug)
    local = {}
    for path in sorted(directory.iterdir()):
        if path.name == POST_FILE:
            continue
        if not valid_media_file(path.name):
            raise ContractError(f"not a contract media file: {path.name}")
        body = path.read_bytes()
        if len(body) > MAX_MEDIA_BYTES:
            raise ContractError(f"media over 900 KB: {path.name}")
        local[path.name] = body
    reference = db.collection("blog").document(slug)
    stored = {snapshot.id: snapshot.to_dict().get("sha256") for snapshot in reference.collection("media").select(["sha256"]).stream()}
    check_media_present(post, set(stored) | set(local))
    for file, body in local.items():
        digest = sha256_hex(body)
        if stored.get(file) != digest:
            reference.collection("media").document(file).set({"data": body, "sha256": digest})
    reference.set({**post, "status": "published", "publishPending": True})
    announce(slug)


def delete(slug, db):
    if not valid_slug(slug):
        raise ContractError("invalid slug")
    reference = db.collection("blog").document(slug)
    if not reference.get().exists:
        raise ContractError("no such post")
    reference.update({"status": "deleted", "publishPending": True})
    announce(slug)


def main():
    parser = argparse.ArgumentParser()
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("put").add_argument("directory")
    commands.add_parser("delete").add_argument("slug")
    arguments = parser.parse_args()
    db = firestore.Client(project=PROJECT)
    try:
        if arguments.command == "put":
            put(arguments.directory, db)
        else:
            delete(arguments.slug, db)
    except ContractError as error:
        sys.exit(str(error))


if __name__ == "__main__":
    main()
