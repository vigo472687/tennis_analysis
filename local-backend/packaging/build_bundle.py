#!/usr/bin/env python3
"""Assemble an offline backend bundle for one platform.

The bundle contains everything the backend needs to run with no network:
a standalone CPython, every locked dependency, the backend code, the frozen
contracts and fixtures, and the three model weights. It deliberately does NOT
contain FFmpeg: FFmpeg is installed on the user's machine with the user's
consent (the Bridge's runtime-prepare flow), so this package does not
redistribute a GPL binary.

Layout mirrors the repository, because the backend locates ``contracts/``,
``fixtures/`` and ``models/`` relative to its own source file::

    <bundle>/python/            standalone CPython 3.11
    <bundle>/site-packages/     locked dependencies (uv export, --extra real-pose)
    <bundle>/app/local-backend/src/tennis_backend/
    <bundle>/app/contracts/ fixtures/ models/
    <bundle>/tennis-backend[.cmd]   launcher

usage: build_bundle.py --target macos-arm64|windows-x64 --out DIR

``windows-x64`` is ASSEMBLED on any host (uv resolves the Windows wheels from
the lock) but can only be RUN on Windows: building it here is not a claim that
it works. ``--verify`` runs the offline smoke test and is only valid for the
host platform.
"""

from __future__ import annotations

import argparse
import hashlib
import platform
import shutil
import subprocess
import sys
import tarfile
import urllib.request
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]
BACKEND = REPO / "local-backend"

PYTHON_VERSION = "3.11.12"
PBS_TAG = "20250529"
PBS_BASE = f"https://github.com/astral-sh/python-build-standalone/releases/download/{PBS_TAG}"
PBS_SUMS = f"{PBS_BASE}/SHA256SUMS"

TARGETS = {
    "macos-arm64": {
        "asset": f"cpython-{PYTHON_VERSION}+{PBS_TAG}-aarch64-apple-darwin-install_only_stripped.tar.gz",
        "uv_platform": "aarch64-apple-darwin",
        "python": "python/bin/python3",
        "launcher": "tennis-backend",
    },
    "windows-x64": {
        "asset": f"cpython-{PYTHON_VERSION}+{PBS_TAG}-x86_64-pc-windows-msvc-install_only_stripped.tar.gz",
        "uv_platform": "x86_64-pc-windows-msvc",
        "python": "python/python.exe",
        "launcher": "tennis-backend.cmd",
    },
}

#: Locked packages with no published wheel. They are built from source, which
#: is only safe when the result is pure Python (a native build would carry the
#: HOST's binaries into another platform's bundle). Each is verified below.
PURE_PYTHON_SDISTS = ("filterpy",)

#: Repository directories copied as-is (relative to the repo root).
COPY_DIRS = ("contracts", "fixtures", "models")

LAUNCHER_UNIX = """#!/bin/sh
# Offline launcher. TENNIS_DATA_ROOT must be set by the caller (the backend refuses without it).
ROOT="$(cd "$(dirname "$0")" && pwd)"
export PYTHONPATH="$ROOT/app/local-backend/src:$ROOT/site-packages"
export PYTHONNOUSERSITE=1
export PYTHONDONTWRITEBYTECODE=1
exec "$ROOT/python/bin/python3" -c "from tennis_backend.cli import main; raise SystemExit(main())" "$@"
"""

LAUNCHER_WINDOWS = """@echo off
rem Offline launcher. TENNIS_DATA_ROOT must be set by the caller (the backend refuses without it).
set "ROOT=%~dp0"
set "PYTHONPATH=%ROOT%app\\local-backend\\src;%ROOT%site-packages"
set "PYTHONNOUSERSITE=1"
set "PYTHONDONTWRITEBYTECODE=1"
"%ROOT%python\\python.exe" -c "from tennis_backend.cli import main; raise SystemExit(main())" %*
"""


def host_target() -> str:
    """The TARGETS key of the machine running the build, or ``""`` if none."""
    system, machine = platform.system(), platform.machine().lower()
    if system == "Darwin" and machine in ("arm64", "aarch64"):
        return "macos-arm64"
    if system == "Windows" and machine in ("amd64", "x86_64"):
        return "windows-x64"
    return ""


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1 << 20), b""):
            digest.update(block)
    return digest.hexdigest()


def fetch(url: str, dest: Path) -> None:
    print(f"  download {url.rsplit('/', 1)[-1]}")
    with urllib.request.urlopen(url) as response, dest.open("wb") as out:  # noqa: S310 - fixed https URLs
        shutil.copyfileobj(response, out)


def expected_python_hash(asset: str, cache: Path) -> str:
    sums = cache / "SHA256SUMS"
    if not sums.is_file():
        fetch(PBS_SUMS, sums)
    for line in sums.read_text().splitlines():
        digest, _, name = line.partition("  ")
        if name.strip() == asset:
            return digest
    raise SystemExit(f"no published checksum for {asset}")


def check_pure_python(site_packages: Path) -> None:
    """Refuse a bundle where a source-built package carries native binaries."""
    for name in PURE_PYTHON_SDISTS:
        native = [
            p for pattern in ("*.so", "*.pyd", "*.dylib", "*.dll")
            for folder in site_packages.glob(f"{name}*") if folder.is_dir()
            for p in folder.rglob(pattern)
        ]
        if native:
            raise SystemExit(f"{name} was built from source but contains native code: {native[:3]}")


def safe_extract(archive: Path, dest: Path) -> None:
    """Extract a tarball, refusing any member that would land outside ``dest``.

    ``tarfile``'s own ``filter="data"`` needs Python 3.11.4+, and this script
    runs under the project's interpreter, so the check is done here.
    """
    root = dest.resolve()
    with tarfile.open(archive) as tar:
        for member in tar.getmembers():
            target = (dest / member.name).resolve()
            if root != target and root not in target.parents:
                raise SystemExit(f"unsafe path in archive: {member.name}")
            if member.issym() or member.islnk():
                link = (target.parent / member.linkname).resolve()
                if root != link and root not in link.parents:
                    raise SystemExit(f"unsafe link in archive: {member.name}")
        tar.extractall(dest)  # noqa: S202 - every member checked above


def run(argv: list[str], **kwargs: object) -> None:
    print("  $", " ".join(str(a) for a in argv))
    subprocess.run(argv, check=True, **kwargs)  # type: ignore[call-overload]


def build(target: str, out: Path, cache: Path) -> Path:
    spec = TARGETS[target]
    bundle = out / f"tennis-backend-{target}"
    if bundle.exists():
        shutil.rmtree(bundle)
    bundle.mkdir(parents=True)
    cache.mkdir(parents=True, exist_ok=True)

    print("1/5 python runtime (checksum from the official SHA256SUMS)")
    archive = cache / spec["asset"]
    if not archive.is_file():
        fetch(f"{PBS_BASE}/{spec['asset'].replace('+', '%2B')}", archive)
    want, got = expected_python_hash(spec["asset"], cache), sha256(archive)
    if want != got:
        archive.unlink()
        raise SystemExit(f"python archive checksum mismatch: {got} != {want}")
    safe_extract(archive, bundle)  # unpacks to <bundle>/python

    print("2/5 locked dependencies (from uv.lock, for the target platform)")
    requirements = cache / f"requirements-{target}.txt"
    with requirements.open("w") as handle:
        subprocess.run(
            ["uv", "export", "--frozen", "--extra", "real-pose", "--no-dev",
             "--no-emit-project", "--no-hashes"],
            cwd=BACKEND, check=True, stdout=handle,
        )
    # Cross-platform mode assumes a minimum OS (macOS 13), which rejects wheels
    # tagged macosx_14_0 such as av and torch; for the host platform let uv
    # resolve natively. Only a genuinely different platform uses the flag.
    command = [
        "uv", "pip", "install", "--no-deps", "--target", str(bundle / "site-packages"),
        "--python-version", "3.11", "--only-binary", ":all:", "-r", str(requirements),
    ]
    if target != host_target():
        command += ["--python-platform", spec["uv_platform"]]
    for name in PURE_PYTHON_SDISTS:
        command += ["--no-binary", name]
    run(command)
    check_pure_python(bundle / "site-packages")

    print("3/5 backend code")
    app = bundle / "app"
    shutil.copytree(
        BACKEND / "src", app / "local-backend" / "src",
        ignore=shutil.ignore_patterns("__pycache__", "*.pyc"),
    )

    print("4/5 contracts, fixtures, model weights")
    for name in COPY_DIRS:
        shutil.copytree(REPO / name, app / name, ignore=shutil.ignore_patterns(".DS_Store"))

    print("5/5 launcher")
    launcher = bundle / spec["launcher"]
    launcher.write_text(LAUNCHER_WINDOWS if target.startswith("windows") else LAUNCHER_UNIX,
                        newline="\r\n" if target.startswith("windows") else "\n")
    launcher.chmod(0o755)
    return bundle


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawTextHelpFormatter)
    parser.add_argument("--target", choices=sorted(TARGETS), required=True)
    parser.add_argument("--out", type=Path, required=True)
    parser.add_argument("--cache", type=Path, default=Path("/tmp/tennis-bundle-cache"))
    args = parser.parse_args()
    bundle = build(args.target, args.out.resolve(), args.cache)
    size = sum(f.stat().st_size for f in bundle.rglob("*") if f.is_file())
    print(f"bundle: {bundle}  ({size / 1e9:.2f} GB)")
    return 0


if __name__ == "__main__":
    sys.exit(main())
