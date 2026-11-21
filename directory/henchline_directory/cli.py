"""henchline-directory: serve, show a pairing or enrolment code, manage phones, back up.

    henchline-directory --config FILE [--listen 127.0.0.1:8791]   serve (loopback only)
    henchline-directory --version
    henchline-directory pair-phone  --config FILE                 a five-minute code for a phone
    henchline-directory enroll-host --config FILE                 a five-minute code for a host
    henchline-directory devices list|revoke DEVICE_ID --config FILE
    henchline-directory backup --config FILE --output FILE
"""

from __future__ import annotations

import argparse
import json
import signal
import sys
from pathlib import Path

from henchline_directory import __version__
from henchline_directory.identity import DirectoryConfig, Identity, expires
from henchline_directory.shared import sqlite_backup
from henchline_directory.store import PAIRING_LIFETIME, DirectoryStore

try:
    from henchline_directory._build import BUILD_ID  # type: ignore[import-not-found]  (written by the release)
except ImportError:
    BUILD_ID = "dev"

LOOPBACK = {"127.0.0.1", "::1"}


def listen_address(text: str) -> tuple[str, int]:
    host, sep, port = text.rpartition(":")
    if host.startswith("[") and host.endswith("]"):
        host = host[1:-1]
    if not sep or host not in LOOPBACK or not port.isdigit() or not 0 < int(port) < 65536:
        raise argparse.ArgumentTypeError("--listen must be a loopback ADDRESS:PORT (127.0.0.1 or [::1])")
    return host, int(port)


def open_all(config_path: Path) -> tuple[DirectoryConfig, DirectoryStore, Identity]:
    config = DirectoryConfig.load(config_path)
    store = DirectoryStore(config.database)
    try:
        return config, store, Identity.load(config, store.directory_id)
    except Exception:
        store.close()
        raise


def serve(args: argparse.Namespace) -> int:
    from henchline_directory.service import Directory, make_server
    config, store, identity = open_all(args.config)
    expected = config.expected_build_id
    if expected and expected != "@BUILD_ID@" and expected != BUILD_ID:
        print(f"config expects build {expected}, this is {BUILD_ID}", file=sys.stderr)
        store.close()
        return 1
    host, port = args.listen or listen_address(config.listen)
    server = make_server(Directory(config, store, identity, BUILD_ID), host, port)
    signal.signal(signal.SIGTERM, lambda *_: (_ for _ in ()).throw(KeyboardInterrupt))
    print(f"henchline-directory {BUILD_ID} on {host}:{port}"
          + ("" if config.base_url else " (no base_url: signed routes and pairing answer 503)"), file=sys.stderr)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()
    return 0


def code(args: argparse.Namespace, kind: str) -> int:
    config, store, identity = open_all(args.config)
    try:
        if not config.base_url:
            print("set base_url in the directory config first", file=sys.stderr)
            return 1
        nonce = store.new_code(kind)
        value = {"schema": f"henchline.directory-{'pair' if kind == 'phone' else 'enroll'}/v1",
                 "directory_id": store.directory_id, "base_url": config.base_url,
                 "tls_authority": identity.descriptor["tls_authority"],
                 "directory_signing_key": canonical_key(identity), "directory_key_algorithm": "ed25519",
                 "nonce": nonce, "expires_at": expires(PAIRING_LIFETIME)}
        print(json.dumps(value, separators=(",", ":")))
        return 0
    finally:
        store.close()


def canonical_key(identity: Identity) -> str:
    return identity.descriptor["directory_signing_key"]


def devices(args: argparse.Namespace) -> int:
    config = DirectoryConfig.load(args.config)
    store = DirectoryStore(config.database)
    try:
        if args.action == "list":
            for phone in store.phones():
                state = "revoked" if phone["revoked_at"] else "active"
                print(f"{phone['device_id']}  {state:7}  {phone['algorithm']:7}  {phone['label']}")
            return 0
        if not store.revoke_phone(args.device_id.lower()):
            print(f"no device {args.device_id}", file=sys.stderr)
            return 1
        print(f"revoked {args.device_id}")
        return 0
    finally:
        store.close()


def backup(args: argparse.Namespace) -> int:
    try:
        config = DirectoryConfig.load(args.config)
        print(json.dumps(sqlite_backup.backup(config.database, args.output)))
    except (OSError, RuntimeError, ValueError) as exc:
        print(f"backup failed: {exc}", file=sys.stderr)
        return 1
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if argv[:1] == ["--version"]:
        print(f"henchline-directory {__version__} ({BUILD_ID})")
        return 0
    commands = {"pair-phone", "enroll-host", "devices", "backup"}
    if argv[:1] and argv[0] in commands:
        parser = argparse.ArgumentParser(prog=f"henchline-directory {argv[0]}")
        parser.add_argument("--config", type=Path, required=True)
        if argv[0] == "devices":
            sub = parser.add_subparsers(dest="action", required=True)
            sub.add_parser("list")
            sub.add_parser("revoke").add_argument("device_id")
        if argv[0] == "backup":
            parser.add_argument("--output", type=Path, required=True)
        args = parser.parse_args(argv[1:])
        try:
            if argv[0] == "backup":
                return backup(args)
            if argv[0] == "devices":
                return devices(args)
            return code(args, "phone" if argv[0] == "pair-phone" else "host")
        except (OSError, RuntimeError, ValueError) as exc:
            print(f"henchline-directory {argv[0]}: {exc}", file=sys.stderr)
            return 1
    parser = argparse.ArgumentParser(prog="henchline-directory", description=__doc__,
                                     formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--config", type=Path, required=True)
    parser.add_argument("--listen", type=listen_address, help="loopback ADDRESS:PORT (else the config's)")
    parser.add_argument("--version", action="store_true")
    args = parser.parse_args(argv)
    try:
        return serve(args)
    except (OSError, RuntimeError, ValueError, argparse.ArgumentTypeError) as exc:
        print(f"henchline-directory: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
