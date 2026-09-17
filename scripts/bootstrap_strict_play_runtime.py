#!/usr/bin/env python3
"""Provision the checksum-pinned Ren'Py runtime used by strict-play gates.

This is a setup helper, not a strict launch fallback. It downloads the official
SDK once, verifies it before extraction, and can assemble a disposable bundled
development project around the repository-owned minimal-play fixture.
"""
from __future__ import annotations

import argparse
import ast
import hashlib
import os
import shutil
import stat
import tarfile
import tempfile
import urllib.request
from pathlib import Path

RENPY_RELEASE = "8.2.0"
RENPY_FULL_VERSION = "8.2.0.24012702"
ARCHIVE_NAME = "renpy-8.2.0-sdk.tar.bz2"
ARCHIVE_URL = f"https://www.renpy.org/dl/{RENPY_RELEASE}/{ARCHIVE_NAME}"
ARCHIVE_SHA256 = "e79ec1014bad6adba69336f90802e89ae7a70172c1bb9c15301267390e9d7b38"


def sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def verify_archive(path: Path) -> None:
    actual = sha256_file(path)
    if actual != ARCHIVE_SHA256:
        raise RuntimeError(
            f"Ren'Py archive checksum mismatch: expected {ARCHIVE_SHA256}, got {actual}"
        )


def download_archive(destination: Path) -> Path:
    destination.parent.mkdir(parents=True, exist_ok=True)
    if destination.exists():
        verify_archive(destination)
        return destination
    with tempfile.NamedTemporaryFile(dir=destination.parent, delete=False) as stream:
        temporary = Path(stream.name)
        try:
            with urllib.request.urlopen(ARCHIVE_URL, timeout=60) as response:
                shutil.copyfileobj(response, stream)
            stream.flush()
            os.fsync(stream.fileno())
        except BaseException:
            temporary.unlink(missing_ok=True)
            raise
    try:
        verify_archive(temporary)
        os.replace(temporary, destination)
    except BaseException:
        temporary.unlink(missing_ok=True)
        raise
    return destination


def extract_sdk(archive: Path, destination: Path) -> Path:
    verify_archive(archive)
    expected = destination / f"renpy-{RENPY_RELEASE}-sdk"
    if expected.is_dir():
        verify_sdk(expected)
        return expected
    destination.mkdir(parents=True, exist_ok=True)
    with tarfile.open(archive, "r:bz2") as bundle:
        bundle.extractall(destination, filter="data")
    verify_sdk(expected)
    return expected


def read_full_version(root: Path) -> str:
    version_file = root / "renpy" / "vc_version.py"
    tree = ast.parse(version_file.read_text(encoding="utf-8"), filename=str(version_file))
    for node in tree.body:
        if not isinstance(node, ast.Assign):
            continue
        if not any(isinstance(target, ast.Name) and target.id == "version" for target in node.targets):
            continue
        value = ast.literal_eval(node.value)
        if isinstance(value, str):
            return value
    raise RuntimeError(f"No static version in {version_file}")


def verify_sdk(root: Path) -> None:
    actual = read_full_version(root)
    if actual != RENPY_FULL_VERSION:
        raise RuntimeError(f"Expected Ren'Py {RENPY_FULL_VERSION}, found {actual}")
    for required in (root / "renpy", root / "lib", root / "renpy.py", root / "renpy.sh"):
        if not required.exists():
            raise RuntimeError(f"Incomplete Ren'Py runtime: missing {required}")


def assemble_bundle(sdk_root: Path, fixture: Path, destination: Path) -> Path:
    verify_sdk(sdk_root)
    if destination.exists():
        raise FileExistsError(f"Bundle destination already exists: {destination}")
    destination.mkdir(parents=True)
    shutil.copytree(sdk_root / "renpy", destination / "renpy", symlinks=True)
    shutil.copytree(sdk_root / "lib", destination / "lib", symlinks=True)
    shutil.copy2(sdk_root / "renpy.py", destination / "renpy.py")
    shutil.copytree(fixture / "game", destination / "game", symlinks=True)
    launcher = destination / "StrictPlay.sh"
    launcher.write_text(
        "#!/usr/bin/env bash\n"
        "set -e\n"
        'BASE="$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)"\n'
        'cd "$BASE"\n'
        'exec "$BASE/lib/py3-linux-x86_64/renpy" "$@"\n',
        encoding="utf-8",
    )
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR)
    (destination / "RENFORGE_RUNTIME.txt").write_text(
        f"version={RENPY_FULL_VERSION}\nsource={ARCHIVE_URL}\nsha256={ARCHIVE_SHA256}\n",
        encoding="utf-8",
    )
    return destination


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--cache-dir", type=Path, required=True)
    parser.add_argument("--bundle", type=Path)
    parser.add_argument(
        "--fixture",
        type=Path,
        default=(
            Path(__file__).resolve().parents[1]
            / "examples"
            / "strict_play_environment_game"
        ),
    )
    args = parser.parse_args()
    archive = download_archive(args.cache_dir / ARCHIVE_NAME)
    sdk = extract_sdk(archive, args.cache_dir)
    print(f"verified {RENPY_FULL_VERSION} at {sdk}")
    if args.bundle:
        print(f"assembled {assemble_bundle(sdk, args.fixture, args.bundle)}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
