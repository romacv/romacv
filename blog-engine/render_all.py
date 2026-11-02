import argparse
import json
import shutil
import sys
from pathlib import Path

from contract import MAX_MEDIA_BYTES, ContractError, check_media_present, valid_media_file, valid_slug, validate_post
from render import render_site, shell_from_html


def write(root, relative_path, data):
    target = root / relative_path
    target.parent.mkdir(parents=True, exist_ok=True)
    target.write_bytes(data)


def load_posts(src):
    posts = []
    media = {}
    if not src.is_dir():
        return posts, media
    for folder in sorted(path for path in src.iterdir() if path.is_dir()):
        slug = folder.name
        try:
            if not valid_slug(slug):
                raise ContractError("invalid slug")
            if folder.is_symlink():
                raise ContractError("symlinked folder")
            post_file = folder / "post.json"
            if post_file.is_symlink():
                raise ContractError("symlinked post.json")
            post = validate_post(json.loads(post_file.read_text()), slug)
            files = set()
            for path in folder.iterdir():
                if not valid_media_file(path.name):
                    continue
                if path.is_symlink():
                    raise ContractError(f"symlinked media: {path.name}")
                if path.is_file():
                    if path.stat().st_size > MAX_MEDIA_BYTES:
                        raise ContractError(f"media over 25 MiB: {path.name}")
                    files.add(path.name)
            check_media_present(post, files)
        except (ContractError, OSError, ValueError) as error:
            print(f"skipped {slug}: {error}", file=sys.stderr)
            continue
        posts.append(post)
        media[slug] = files
    return posts, media


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--src", required=True, type=Path)
    parser.add_argument("--out", required=True, type=Path)
    arguments = parser.parse_args()
    posts, media = load_posts(arguments.src)
    shell = shell_from_html((arguments.out / "project.html").read_text())
    shutil.rmtree(arguments.out / "blog", ignore_errors=True)
    for path, data in render_site(posts, media, shell).items():
        write(arguments.out, path, data)
    for slug, files in media.items():
        for file in files:
            write(arguments.out, f"blog/{slug}/{file}", (arguments.src / slug / file).read_bytes())


if __name__ == "__main__":
    main()
