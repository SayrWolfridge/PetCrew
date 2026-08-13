"""Validate UTF-8 JSON files received as newline-delimited paths on stdin."""

import json
import sys
from pathlib import Path


def main() -> int:
    failed = False
    for raw_path in sys.stdin:
        path_text = raw_path.rstrip("\r\n")
        if not path_text:
            continue

        path = Path(path_text)
        try:
            with path.open("r", encoding="utf-8-sig") as json_file:
                json.load(json_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
