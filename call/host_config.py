"""Validated identity and advertised capabilities for one Henchline host."""

from __future__ import annotations

import datetime as dt
import json
import os
import re
import uuid
from dataclasses import dataclass
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit


API_VERSION = "1.0"
DEFAULT_CONFIG = Path.home() / ".config/henchline/host.json"
DEFAULT_DATABASE = Path.home() / ".local/share/henchline/host.sqlite3"
DEFAULT_KEY = Path.home() / ".local/share/henchline/keys/host.ed25519"
CAPABILITIES = {
    "chat", "calls", "files", "usage", "plans", "workflows",
    "delegated_agents", "notifications", "reminders",
    "legacy_routes",  # the server's own routes (crew, texts, the call...) also take a signed request
}
UTC_STAMP = re.compile(r"\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}Z")


def utc_now() -> str:
    """RFC 3339 UTC to the second, the one form descriptors carry."""
    return dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _utc_stamp(value: Any) -> str:
    text = str(value)
    if not UTC_STAMP.fullmatch(text):
        raise ValueError("updated_at must be RFC 3339 UTC to the second, like 2026-11-21T12:00:00Z")
    dt.datetime.strptime(text, "%Y-%m-%dT%H:%M:%SZ")  # a real date and time
    return text


def _origin(base_url: str) -> tuple[str, str]:
    """(base_url with one trailing slash, its lowercase authority), or ValueError."""
    base_url = str(base_url).rstrip("/") + "/"
    parsed = urlsplit(base_url)
    if (parsed.scheme != "https" or not parsed.hostname or parsed.query or parsed.fragment
            or parsed.username is not None or parsed.password is not None or parsed.path != "/"):
        raise ValueError("base_url must be an HTTPS origin, like https://host.example/")
    return base_url, parsed.netloc.lower()


@dataclass(frozen=True)
class HostConfig:
    host_id: str
    label: str
    base_url: str
    tls_authority: str
    descriptor_seq: int
    updated_at: str
    capabilities: dict[str, bool]
    database: Path = DEFAULT_DATABASE
    key_file: Path = DEFAULT_KEY
    min_app_version: int = 1
    api_version: str = API_VERSION
    ledger_days: int = 30  # how long the host keeps chat events (agents' texts, messages sent over v1)

    @classmethod
    def from_dict(cls, raw: Any) -> "HostConfig":
        if not isinstance(raw, dict):
            raise ValueError("host config must be an object")
        try:
            host_id = str(uuid.UUID(str(raw["host_id"])))
            label = str(raw["label"]).strip()
            base_url, expected = _origin(raw["base_url"])
            authority = str(raw["tls_authority"]).lower()
            seq = int(raw.get("descriptor_seq", 1))
            minimum = int(raw.get("min_app_version", 1))
            updated_at = _utc_stamp(raw["updated_at"])
        except (KeyError, TypeError, ValueError) as exc:
            raise ValueError(f"invalid host identity: {exc}") from exc
        if authority != expected:
            raise ValueError("tls_authority must exactly match base_url authority")
        if not label or len(label) > 80 or re.search(r"[\x00-\x1f\x7f]", label):
            raise ValueError("label must be 1..80 printable characters")
        if seq < 1 or minimum < 1:
            raise ValueError("descriptor_seq and min_app_version must be positive")
        capabilities = raw.get("capabilities", {})
        if not isinstance(capabilities, dict) or any(k not in CAPABILITIES or not isinstance(v, bool)
                                                    for k, v in capabilities.items()):
            raise ValueError("capabilities contain an unknown name or non-boolean value")
        ledger_days = raw.get("ledger_days", 30)
        if not isinstance(ledger_days, int) or isinstance(ledger_days, bool) or not 1 <= ledger_days <= 3650:
            raise ValueError("ledger_days must be a whole number of days, 1..3650")
        api_version = str(raw.get("api_version", API_VERSION))
        if api_version != API_VERSION:
            raise ValueError(f"api_version {api_version} is not supported (this host speaks {API_VERSION})")
        return cls(
            host_id=host_id,
            label=label,
            base_url=base_url,
            tls_authority=authority,
            descriptor_seq=seq,
            updated_at=updated_at,
            capabilities={name: bool(capabilities.get(name, False)) for name in sorted(CAPABILITIES)},
            database=Path(raw.get("database", DEFAULT_DATABASE)).expanduser(),
            key_file=Path(raw.get("key_file", DEFAULT_KEY)).expanduser(),
            min_app_version=minimum,
            api_version=api_version,
            ledger_days=ledger_days,
        )

    @classmethod
    def load(cls, path: Path = DEFAULT_CONFIG) -> "HostConfig":
        try:
            raw = json.loads(Path(path).read_text())
        except (OSError, ValueError) as exc:
            raise ValueError(f"cannot read host config {path}: {exc}") from exc
        return cls.from_dict(raw)

    def validate_private_paths(self) -> None:
        for path in (self.database, self.key_file):
            if path.exists() and path.stat().st_mode & 0o077:
                raise ValueError(f"{path} must not be accessible by group or others")


def initialize(path: Path, *, label: str, base_url: str, capabilities: dict[str, bool],
               now: str | None = None) -> HostConfig:
    """Create a stable host config without overwriting an existing identity.

    Everything is validated before the file exists, so a bad base URL or label
    leaves no half-made identity behind.
    """
    path = Path(path)
    if path.exists():
        return HostConfig.load(path)
    base, authority = _origin(base_url)
    data = {
        "host_id": str(uuid.uuid4()),
        "label": label,
        "base_url": base,
        "tls_authority": authority,
        "api_version": API_VERSION,
        "descriptor_seq": 1,
        "updated_at": now or utc_now(),
        "min_app_version": 1,
        "capabilities": capabilities,
    }
    config = HostConfig.from_dict(data)
    path.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    with os.fdopen(fd, "w") as output:
        json.dump(data, output, sort_keys=True, indent=2)
        output.write("\n")
    return config
