"""Modules shared with the host server (call/): copied into the release, imported from the repository."""

try:  # the release wheel carries its own copies
    from henchline_directory import canonical, sqlite_backup  # type: ignore[attr-defined]
except ImportError:  # running from the repository
    from call import canonical, sqlite_backup  # noqa: F401
