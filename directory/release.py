#!/usr/bin/env python3
"""Build a henchline.directory-release/v1 tree: what the directory's installer verifies and installs.

    directory/release.py --output DIR [--python 3.14] [--platform x86_64]

DIR (must not exist) gets:
    release-manifest.json   schema, build_id, python, entrypoint, requirements_lock, capabilities_fixture,
                            and every other file with its SHA-256
    requirements.lock       every wheel pinned by version and hash (pip install --require-hashes --no-index)
    wheelhouse/*.whl        henchline-directory itself (built here, byte-for-byte reproducible from the same
                            commit) and its dependencies for that CPython, downloaded from PyPI as binaries
    capabilities.json       what this directory offers

The build ID is the package version plus the commit (and "-dirty" when the tree has changes): an installed
release says exactly what it was built from. Nothing secret goes in: no key, ID, database or nonce.
"""

from __future__ import annotations

import argparse
import base64
import hashlib
import io
import json
import re
import subprocess
import sys
import tempfile
import zipfile
from pathlib import Path

REPO = Path(__file__).resolve().parents[1]
PACKAGE = REPO / "directory/henchline_directory"
SHARED = [REPO / "call/canonical.py", REPO / "call/sqlite_backup.py"]  # stdlib only; carried as they are
NAME, ENTRYPOINT = "henchline-directory", "henchline-directory"
DEPENDENCIES = ["cryptography==50.0.2", "cffi==2.1.1", "pycparser==3.12"]  # = the host's pins, all of them
EPOCH = (2026, 1, 1, 0, 0, 0)  # every file in the wheel gets this time: same input, same bytes
PLATFORMS = {
    "x86_64": ["manylinux_2_34_x86_64", "manylinux_2_28_x86_64", "manylinux_2_17_x86_64", "manylinux2014_x86_64"],
    "aarch64": ["manylinux_2_34_aarch64", "manylinux_2_28_aarch64", "manylinux_2_17_aarch64",
                "manylinux2014_aarch64"],
}
CAPABILITIES = {"schema": "henchline.directory-capabilities/v1", "api_version": "1.0",
                "capabilities": {"discovery": True, "phone_pairing": True, "host_enrolment": True,
                                 "heartbeats": True, "relay": False}}


def sha256(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def version() -> str:
    found = re.search(r'__version__ = "([^"]+)"', (PACKAGE / "__init__.py").read_text())
    return found.group(1)


def build_id() -> str:
    commit = subprocess.run(["git", "-C", str(REPO), "rev-parse", "--short=12", "HEAD"],
                            capture_output=True, text=True, check=True).stdout.strip()
    dirty = subprocess.run(["git", "-C", str(REPO), "status", "--porcelain", "--", "directory", *map(str, SHARED)],
                           capture_output=True, text=True, check=True).stdout.strip()
    return f"{version()}-{commit}" + ("-dirty" if dirty else "")


def wheel(build: str) -> tuple[str, bytes]:
    """The pure-Python wheel, made directly: no build backend, fixed timestamps and order."""
    ver = version()
    dist = f"henchline_directory-{ver}"
    files: dict[str, bytes] = {}
    for path in sorted(PACKAGE.glob("*.py")):
        files[f"henchline_directory/{path.name}"] = path.read_bytes()
    for path in SHARED:
        files[f"henchline_directory/{path.name}"] = path.read_bytes()
    files["henchline_directory/_build.py"] = f'BUILD_ID = "{build}"\n'.encode()
    requires = "".join(f"Requires-Dist: {d.split('==')[0]}>={d.split('==')[1]}\n" for d in DEPENDENCIES[:1])
    files[f"{dist}.dist-info/METADATA"] = (
        f"Metadata-Version: 2.1\nName: {NAME}\nVersion: {ver}\nSummary: Henchline host directory "
        f"(discovery only)\nLicense: see the Henchline repository\nRequires-Python: >=3.12\n{requires}").encode()
    files[f"{dist}.dist-info/WHEEL"] = (b"Wheel-Version: 1.0\nGenerator: henchline-release\n"
                                        b"Root-Is-Purelib: true\nTag: py3-none-any\n")
    files[f"{dist}.dist-info/entry_points.txt"] = (
        f"[console_scripts]\n{ENTRYPOINT} = henchline_directory.cli:main\n").encode()
    record = [f"{name},sha256={base64.urlsafe_b64encode(hashlib.sha256(data).digest()).rstrip(b'=').decode()},"
              f"{len(data)}" for name, data in files.items()]
    record.append(f"{dist}.dist-info/RECORD,,")
    files[f"{dist}.dist-info/RECORD"] = ("\n".join(record) + "\n").encode()
    out = io.BytesIO()
    with zipfile.ZipFile(out, "w", zipfile.ZIP_DEFLATED) as archive:
        for name, data in files.items():
            info = zipfile.ZipInfo(name, EPOCH)
            info.external_attr = 0o644 << 16
            info.compress_type = zipfile.ZIP_DEFLATED
            archive.writestr(info, data)
    return f"{dist}-py3-none-any.whl", out.getvalue()


def download(into: Path, python: str, platform: str) -> None:
    abi = "cp" + python.replace(".", "")
    command = [sys.executable, "-m", "pip", "download", "--quiet", "--no-deps", "--only-binary=:all:",
               "--implementation", "cp", "--python-version", python, "--abi", abi, "--abi", "abi3",
               "--abi", "none", "--dest", str(into)]
    for tag in PLATFORMS[platform] + ["any"]:
        command += ["--platform", tag]
    subprocess.run(command + DEPENDENCIES, check=True)
    names = sorted(p.name for p in into.glob("*.whl"))
    for dep in DEPENDENCIES:  # exactly one wheel per pin, for this Python
        stem = dep.split("==")[0].replace("-", "_")
        if sum(n.lower().startswith(f"{stem}-{dep.split('==')[1]}-") for n in names) != 1:
            raise SystemExit(f"no single {python}/{platform} wheel for {dep}: {names}")


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--output", type=Path, required=True)
    parser.add_argument("--python", default="3.14", help="the CPython the release targets (the installer wants 3.14)")
    parser.add_argument("--platform", choices=sorted(PLATFORMS), default="x86_64")
    args = parser.parse_args()
    if args.output.exists():
        raise SystemExit(f"{args.output} already exists")
    build = build_id()
    with tempfile.TemporaryDirectory(dir=args.output.parent) as scratch:
        tree = Path(scratch) / "release"
        wheels = tree / "wheelhouse"
        wheels.mkdir(parents=True)
        name, data = wheel(build)
        (wheels / name).write_bytes(data)
        download(wheels, args.python, args.platform)
        lock = ["# henchline-directory release lock: pip install --require-hashes --no-index --find-links wheelhouse"]
        pins = [f"{NAME}=={version()}"] + DEPENDENCIES
        for pin in pins:
            stem, ver = pin.split("==")
            found = [p for p in sorted(wheels.glob("*.whl"))
                     if p.name.lower().startswith(f"{stem.replace('-', '_')}-{ver}-")]
            lock.append(f"{pin} " + " ".join(f"--hash=sha256:{sha256(p.read_bytes())}" for p in found))
        (tree / "requirements.lock").write_text("\n".join(lock) + "\n")
        (tree / "capabilities.json").write_text(json.dumps(CAPABILITIES, indent=2, sort_keys=True) + "\n")
        files = sorted(p for p in tree.rglob("*") if p.is_file())
        manifest = {
            "schema": "henchline.directory-release/v1",
            "build_id": build,
            "python": args.python,
            "platform": args.platform,
            "entrypoint": ENTRYPOINT,
            "requirements_lock": "requirements.lock",
            "capabilities_fixture": "capabilities.json",
            "files": [{"path": str(p.relative_to(tree)), "sha256": sha256(p.read_bytes())} for p in files],
        }
        (tree / "release-manifest.json").write_text(json.dumps(manifest, indent=2) + "\n")
        tree.rename(args.output)
    print(json.dumps({"release": str(args.output), "build_id": build, "python": args.python,
                      "platform": args.platform}))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
