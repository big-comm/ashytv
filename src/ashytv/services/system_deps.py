"""The player's system dependency: libmpv and the codecs behind it.

The AppImage does not bundle libmpv/FFmpeg (they would more than double its
size); the host's libmpv is loaded through python-mpv instead. This module
tells whether it loads, whether its FFmpeg decodes the usual IPTV codecs,
and how to install what is missing on each distro family.
"""

from __future__ import annotations

import importlib
import shlex
from pathlib import Path

from ..i18n import _

# Decoders an IPTV player cannot do without (by FFmpeg codec name).
REQUIRED_CODECS = ("h264", "hevc")

# Decoders that do not make a codec usable for IPTV on their own.
_LIMITED_DECODERS = {"libopenh264"}

# Packages providing libmpv.so.2, by distro family.
_LIBMPV_PACKAGES = {
    "arch": ["mpv"],
    "debian": ["libmpv2"],
    "rpm": ["mpv-libs"],
    "suse": ["libmpv2"],
}


def distro_base(os_release: str = "/etc/os-release") -> str | None:
    """Distro family from os-release ID/ID_LIKE: arch, debian, rpm, suse."""
    fields: dict[str, str] = {}
    try:
        for line in Path(os_release).read_text(encoding="utf-8").splitlines():
            key, sep, value = line.partition("=")
            if sep:
                fields[key.strip()] = value.strip().strip("\"'")
    except OSError:
        return None
    ids = [fields.get("ID", ""), *fields.get("ID_LIKE", "").split()]
    for family, names in (
        ("suse", ("suse", "opensuse", "opensuse-tumbleweed", "opensuse-leap", "sles")),
        ("arch", ("arch", "manjaro", "endeavouros", "biglinux", "cachyos")),
        ("debian", ("debian", "ubuntu", "linuxmint", "pop", "elementary", "zorin")),
        ("rpm", ("fedora", "rhel", "centos", "nobara", "almalinux", "rocky", "ultramarine")),
    ):
        if any(i in names for i in ids):
            return family
    return None


def libmpv_loads() -> bool:
    """python-mpv raises OSError at import when libmpv itself is missing."""
    try:
        importlib.import_module("mpv")
    except (ImportError, OSError):
        return False
    return True


def missing_codecs() -> list[str]:
    """REQUIRED_CODECS the loaded libmpv's FFmpeg cannot decode. Fedora's
    libmpv, for one, is built on the patent-free FFmpeg (no H.264/HEVC)."""
    try:
        import mpv

        player = mpv.MPV(vo="null", ao="null")
        try:
            # Fedora's FFmpeg decodes H.264 only through Cisco's OpenH264:
            # software-only and limited, so it does not count.
            codecs = {
                d.get("codec")
                for d in (player.decoder_list or [])
                if d.get("driver") not in _LIMITED_DECODERS
            }
        finally:
            player.terminate()
    except Exception:  # never block startup on a probe
        return []
    return [c for c in REQUIRED_CODECS if c not in codecs]


def install_plan(base: str | None = None) -> dict | None:
    """Command that installs libmpv, or None for an unknown distro.

    ``command`` runs through pkexec without prompts (the output is shown in
    a text view, not a terminal), ``display`` is what the user reads."""
    base = base if base is not None else distro_base()
    packages = _LIBMPV_PACKAGES.get(base or "")
    if not packages:
        return None
    command = {
        "arch": ["pkexec", "pacman", "-S", "--needed", "--noconfirm", *packages],
        "debian": ["pkexec", "apt-get", "install", "-y", *packages],
        "rpm": ["pkexec", "dnf", "install", "-y", *packages],
        "suse": ["pkexec", "zypper", "--non-interactive", "install", *packages],
    }[base]
    return {"command": command, "display": shlex.join(command), "packages": packages}


def codecs_hint(base: str | None = None) -> str:
    """How to get the full FFmpeg on this distro family."""
    base = base if base is not None else distro_base()
    if base == "rpm":
        return _(
            "Enable the RPM Fusion repository and install the full codecs:\n"
            "sudo dnf install libavcodec-freeworld"
        )
    if base == "suse":
        return _(
            "Add the Packman repository and switch the FFmpeg libraries to it:\n"
            "sudo zypper dup --from packman --allow-vendor-change"
        )
    return _("Install your distribution's full FFmpeg libraries (libavcodec).")
