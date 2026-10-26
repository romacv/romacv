import argparse
import json
from pathlib import Path
import platforms


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument('command', choices=['draft', 'montage-plan'])
    parser.add_argument('path', type=Path)
    args = parser.parse_args()
    value = json.loads(args.path.read_text())
    platforms.configure({}, None, Path.cwd())
    if args.command == 'draft':
        from drafter import validate
        errors = validate(value)
    else:
        from montage import validate
        errors = validate(value['edl'], value['sources'])
    for error in errors:
        print('ERROR ' + error)
    if not errors:
        print('OK')
    return int(bool(errors))


if __name__ == '__main__':
    raise SystemExit(main())
