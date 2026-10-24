"""Provider passwords in the system keyring (libsecret).

If libsecret (or a keyring service) is unavailable, the functions return
False/None and the caller stores the password with the playlist.
"""

from __future__ import annotations

try:
    import gi

    gi.require_version("Secret", "1")
    from gi.repository import Secret
except (ImportError, ValueError):  # pragma: no cover — system dependent
    Secret = None

_SCHEMA = None


def _schema():
    global _SCHEMA
    if _SCHEMA is None and Secret is not None:
        _SCHEMA = Secret.Schema.new(
            "org.communitybig.ashytv.Xtream",
            Secret.SchemaFlags.NONE,
            {"host": Secret.SchemaAttributeType.STRING,
             "username": Secret.SchemaAttributeType.STRING},
        )
    return _SCHEMA


def store_password(host: str, username: str, password: str) -> bool:
    if Secret is None:
        return False
    try:
        return bool(Secret.password_store_sync(
            _schema(), {"host": host, "username": username},
            Secret.COLLECTION_DEFAULT, f"AshyTV — {username}@{host}", password, None))
    except Exception:  # no keyring service: the caller falls back
        return False


def lookup_password(host: str, username: str) -> str | None:
    if Secret is None:
        return None
    try:
        return Secret.password_lookup_sync(
            _schema(), {"host": host, "username": username}, None)
    except Exception:
        return None


def clear_password(host: str, username: str) -> None:
    if Secret is None:
        return
    try:
        Secret.password_clear_sync(_schema(), {"host": host, "username": username},
                                   None)
    except Exception:
        pass
