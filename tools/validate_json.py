"""Validate publishable UTF-8 JSON files below a repository root."""

import json
import sys
from pathlib import Path


EXCLUDED_DIRECTORIES = {".git", "node_modules", "target", "dist", "tmp", "_agents"}


def should_skip(path: Path, repo_root: Path) -> bool:
    relative_parts = tuple(part.casefold() for part in path.relative_to(repo_root).parts[:-1])
    if any(part in EXCLUDED_DIRECTORIES for part in relative_parts):
        return True
    return relative_parts[:2] == ("artifacts", "backups")


def main() -> int:
    if len(sys.argv) != 2:
        print("usage: validate_json.py <repository-root>", file=sys.stderr)
        return 2

    repo_root = Path(sys.argv[1]).resolve()
    failed = False
    for path in repo_root.rglob("*.json"):
        if should_skip(path, repo_root):
            continue

        try:
            with path.open("r", encoding="utf-8-sig") as json_file:
                json.load(json_file)
        except (OSError, UnicodeError, json.JSONDecodeError) as error:
            print(f"{path}: {error}", file=sys.stderr)
            failed = True

    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
