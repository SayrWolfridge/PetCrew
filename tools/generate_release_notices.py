from __future__ import annotations

import argparse
import hashlib
import json
import re
import shutil
import subprocess
import sys
import tomllib
from dataclasses import dataclass
from pathlib import Path


LICENSE_FILE_PATTERN = re.compile(r"^(license|copying|notice|unlicense)", re.IGNORECASE)
CARGO_PACKAGE_PATTERN = re.compile(r"^([^ ]+) v([^ ]+)")


@dataclass(frozen=True, order=True)
class Package:
    ecosystem: str
    name: str
    version: str
    license_expression: str
    source: str
    directory: Path


def run_json(command: list[str], cwd: Path) -> object:
    command[0] = shutil.which(command[0]) or shutil.which(f"{command[0]}.cmd") or command[0]
    result = subprocess.run(command, cwd=cwd, capture_output=True, text=True, check=True)
    return json.loads(result.stdout)


def npm_packages(overlay: Path) -> list[Package]:
    records = run_json(["npm", "query", ":not(:root):not(.dev)", "--json"], overlay)
    packages: list[Package] = []
    for record in records:
        directory = Path(record["path"])
        if directory.resolve() == overlay.resolve() or record.get("dev"):
            continue
        packages.append(
            Package(
                ecosystem="npm",
                name=record["name"],
                version=record["version"],
                license_expression=record.get("license") or "UNKNOWN",
                source=record.get("resolved") or "npm lockfile",
                directory=directory,
            )
        )
    return sorted(set(packages))


def cargo_registry_root() -> Path:
    cargo_home = Path.home() / ".cargo"
    return cargo_home / "registry" / "src"


def cargo_packages(manifest: Path, target: str) -> list[Package]:
    cargo = shutil.which("cargo") or "cargo"
    result = subprocess.run(
        [
            cargo,
            "tree",
            "--manifest-path",
            str(manifest),
            "--locked",
            "--offline",
            "--target",
            target,
            "--edges",
            "normal,build",
            "--prefix",
            "none",
            "--format",
            "{p}",
        ],
        capture_output=True,
        text=True,
        check=True,
    )
    pairs: set[tuple[str, str]] = set()
    for line in result.stdout.splitlines():
        match = CARGO_PACKAGE_PATTERN.match(line)
        if match and match.group(1) != "petcrew":
            pairs.add((match.group(1), match.group(2)))

    registry = cargo_registry_root()
    packages: list[Package] = []
    for name, version in sorted(pairs):
        matches = list(registry.glob(f"*/{name}-{version}"))
        if len(matches) != 1:
            raise RuntimeError(f"expected one cached Cargo source for {name} {version}, found {len(matches)}")
        directory = matches[0]
        metadata = tomllib.loads((directory / "Cargo.toml").read_text(encoding="utf-8"))
        package = metadata.get("package", {})
        license_expression = package.get("license") or package.get("license-file") or "UNKNOWN"
        source = package.get("repository") or package.get("homepage") or f"https://crates.io/crates/{name}/{version}"
        packages.append(
            Package(
                ecosystem="cargo",
                name=name,
                version=version,
                license_expression=license_expression,
                source=source,
                directory=directory,
            )
        )
    return packages


def license_files(package: Package) -> list[Path]:
    return sorted(
        path
        for path in package.directory.iterdir()
        if path.is_file() and LICENSE_FILE_PATTERN.match(path.name) and path.stat().st_size <= 1_000_000
    )


def render(packages: list[Package], source_commit: str) -> str:
    unknown = [package for package in packages if package.license_expression == "UNKNOWN"]
    if unknown:
        names = ", ".join(f"{package.ecosystem}:{package.name}@{package.version}" for package in unknown)
        raise RuntimeError(f"packages without license metadata: {names}")

    package_files: dict[Package, list[tuple[str, str]]] = {}
    texts: dict[str, str] = {}
    for package in packages:
        entries: list[tuple[str, str]] = []
        for path in license_files(package):
            content = path.read_text(encoding="utf-8", errors="replace").strip()
            digest = hashlib.sha256(content.encode("utf-8")).hexdigest()
            texts.setdefault(digest, content)
            entries.append((path.name, digest))
        package_files[package] = entries

    lines = [
        "PETCREW THIRD-PARTY LICENSE REPORT",
        "==================================",
        "",
        f"Source commit: {source_commit}",
        "Target: x86_64-pc-windows-msvc",
        f"Packages: {len(packages)}",
        "",
        "This report is generated from the exact installed npm production tree and",
        "the exact Cargo dependency graph used for the Windows binary. PetCrew itself",
        "is licensed under MIT; third-party components retain the licenses below.",
        "",
        "PACKAGE INVENTORY",
        "-----------------",
    ]
    for package in packages:
        files = package_files[package]
        file_summary = ", ".join(f"{name} [{digest[:12]}]" for name, digest in files) or "declared metadata only"
        lines.extend(
            [
                f"{package.ecosystem}: {package.name} {package.version}",
                f"License: {package.license_expression}",
                f"Source: {package.source}",
                f"Included notices: {file_summary}",
                "",
            ]
        )

    lines.extend(["DEDUPLICATED LICENSE AND NOTICE TEXTS", "-------------------------------------", ""])
    for digest, content in sorted(texts.items()):
        owners = []
        for package, files in package_files.items():
            if any(file_digest == digest for _, file_digest in files):
                owners.append(f"{package.ecosystem}:{package.name}@{package.version}")
        lines.extend(
            [
                f"SHA-256: {digest}",
                f"Used by: {', '.join(owners)}",
                "",
                content,
                "",
                "=" * 78,
                "",
            ]
        )
    return "\n".join(lines).rstrip() + "\n"


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--repo-root", type=Path, default=Path(__file__).resolve().parents[1])
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--source-commit", required=True)
    parser.add_argument("--target", default="x86_64-pc-windows-msvc")
    args = parser.parse_args()

    root = args.repo_root.resolve()
    packages = npm_packages(root / "apps" / "overlay")
    packages.extend(cargo_packages(root / "apps" / "overlay" / "src-tauri" / "Cargo.toml", args.target))
    report = render(sorted(packages), args.source_commit)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_text(report, encoding="utf-8", newline="\n")
    print(f"RELEASE_NOTICES_OK: {len(packages)} packages, {args.output.stat().st_size} bytes")
    return 0


if __name__ == "__main__":
    sys.exit(main())
