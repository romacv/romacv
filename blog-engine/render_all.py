import argparse
import json
import shutil
import sys
from pathlib import Path

from google.cloud import firestore
from google.cloud.firestore_v1.base_query import FieldFilter

from contract import (
    MAX_MEDIA_BYTES, ContractError, check_media_present, post_of, sha256_hex, valid_media_file, validate_post,
)
from render import render_site, shell_from_html

PROJECT = "resromcom-493b4"
RENDERED_PENDING = Path(__file__).with_name(".rendered-pending.json")


def write(root, relative_path, data):
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def jpeg_size(data):
    if data[:2] != b"\xff\xd8":
        return None
    position = 2
    while position + 9 < len(data):
        if data[position] != 0xFF:
            return None
        marker = data[position + 1]
        if marker == 0xFF:
            position += 1
            continue
        if marker in (0x01, *range(0xD0, 0xD8)):
            position += 2
            continue
        length = int.from_bytes(data[position + 2:position + 4], "big")
        if marker in (0xC0, 0xC1, 0xC2):
            height = int.from_bytes(data[position + 5:position + 7], "big")
            width = int.from_bytes(data[position + 7:position + 9], "big")
            return (width, height) if width and height else None
        position += 2 + length
    return None


def load_media(reference):
    files = {}
    for snapshot in reference.collection("media").stream():
        data = snapshot.to_dict()
        body = data.get("data")
        if not valid_media_file(snapshot.id) or not isinstance(body, bytes):
            continue
        if len(body) > MAX_MEDIA_BYTES:
            raise ContractError(f"media over 900 KB: {snapshot.id}")
        if sha256_hex(body) != data.get("sha256"):
            raise ContractError(f"media checksum mismatch: {snapshot.id}")
        files[snapshot.id] = body
    return files


def load_posts(db):
    posts = []
    media = {}
    for snapshot in db.collection("blog").where(filter=FieldFilter("status", "==", "published")).stream():
        slug = snapshot.id
        try:
            post = validate_post(post_of(snapshot.to_dict()), slug)
            files = load_media(snapshot.reference)
            check_media_present(post, files)
        except ContractError as error:
            print(f"skipped {slug}: {error}", file=sys.stderr)
            continue
        posts.append(post)
        media[slug] = files
    return posts, media


def pending_posts(db):
    return db.collection("blog").where(filter=FieldFilter("publishPending", "==", True)).stream()


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--out", type=Path)
    arguments = parser.parse_args()
    db = firestore.Client(project=PROJECT)
    if arguments.out is None:
        parser.error("--out is required")
    pending = {snapshot.id: snapshot.update_time.isoformat() for snapshot in pending_posts(db)}
    posts, media = load_posts(db)
    shell = shell_from_html((arguments.out / "project.html").read_text())
    shutil.rmtree(arguments.out / "blog", ignore_errors=True)
    cover_sizes = {post["slug"]: jpeg_size(media[post["slug"]][post["cover"]]) for post in posts}
    cover_sizes = {slug: size for slug, size in cover_sizes.items() if size}
    for path, data in render_site(posts, {slug: set(files) for slug, files in media.items()}, shell, cover_sizes).items():
        write(arguments.out, path, data)
    for slug, files in media.items():
        for file, body in files.items():
            write(arguments.out, f"blog/{slug}/{file}", body)
    RENDERED_PENDING.write_text(json.dumps(pending))


if __name__ == "__main__":
    main()
