"""Distro family detection and the libmpv install command (no packages needed)."""

import pytest

from ashytv.services import system_deps as sd

OS_RELEASES = {
    "arch": 'ID=arch\n',
    "biglinux": 'ID="biglinux"\nID_LIKE="manjaro arch"\n',
    "ubuntu": 'ID=ubuntu\nID_LIKE=debian\n',
    "mint": 'ID=linuxmint\nID_LIKE="ubuntu debian"\n',
    "debian": 'ID=debian\n',
    "fedora": 'ID=fedora\n',
    "alma": 'ID="almalinux"\nID_LIKE="rhel centos fedora"\n',
    "tumbleweed": 'ID="opensuse-tumbleweed"\nID_LIKE="opensuse suse"\n',
    "void": 'ID="void"\n',
}
EXPECTED = {
    "arch": "arch", "biglinux": "arch", "ubuntu": "debian", "mint": "debian",
    "debian": "debian", "fedora": "rpm", "alma": "rpm", "tumbleweed": "suse",
    "void": None,
}


@pytest.mark.parametrize("name", OS_RELEASES)
def test_distro_base(tmp_path, name):
    path = tmp_path / "os-release"
    path.write_text(OS_RELEASES[name])
    assert sd.distro_base(str(path)) == EXPECTED[name]


def test_missing_os_release(tmp_path):
    assert sd.distro_base(str(tmp_path / "nope")) is None


@pytest.mark.parametrize(("base", "package"), [
    ("arch", "mpv"), ("debian", "libmpv2"), ("rpm", "mpv-libs"), ("suse", "libmpv2"),
])
def test_install_plan_runs_unattended_through_pkexec(base, package):
    plan = sd.install_plan(base)
    assert plan["command"][0] == "pkexec"
    assert plan["command"][-1] == package
    # The output goes to a text view, not a terminal: nothing may prompt.
    assert {"--noconfirm", "-y", "--non-interactive"} & set(plan["command"])
    assert plan["display"].startswith("pkexec ")


def test_unknown_distro_has_no_plan():
    assert sd.install_plan("") is None
