"""scripts/install-openshell.sh with fake `openshell` / `curl` on PATH (no network, no Homebrew)."""
import os
import subprocess
from pathlib import Path

ROOT = Path(__file__).parent.parent
VERSION = (ROOT / ".openshell-version").read_text().strip()


def run_install(tmp_path: Path, installed: str, bridge: bool) -> tuple[subprocess.CompletedProcess, str]:
    bin_dir, home = tmp_path / "bin", tmp_path / "home"
    bin_dir.mkdir()
    (home / ".config" / "openshell").mkdir(parents=True)
    if bridge:
        (home / ".config" / "openshell" / "gateway.toml").write_text('supervisor_network_mode = "bridge"\n')
    calls = tmp_path / "calls.txt"
    fakes = {
        "openshell": f'echo "openshell {installed}"\n',
        # records the URL, then writes a no-op installer to the -o target
        "curl": f'echo "curl $*" >> {calls}\nwhile [ $# -gt 0 ]; do [ "$1" = "-o" ] && echo "exit 0" > "$2"; shift; done\n',
        "brew": f'echo "brew $*" >> {calls}\nexit 1\n',
    }
    for name, body in fakes.items():
        p = bin_dir / name
        p.write_text("#!/bin/sh\n" + body)
        p.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_dir}:{os.environ['PATH']}", "HOME": str(home)}
    res = subprocess.run(["sh", str(ROOT / "scripts" / "install-openshell.sh")], env=env, capture_output=True, text=True, timeout=30)
    return res, calls.read_text() if calls.exists() else ""


def test_install_is_a_no_op_when_already_set_up(tmp_path):
    res, calls = run_install(tmp_path, VERSION.lstrip("v"), bridge=True)
    assert res.returncode == 0, res.stderr
    assert "skipping the installer" in res.stdout and "leaving its config alone" in res.stdout
    assert calls == ""  # nothing downloaded, gateway not restarted


def test_install_fetches_the_installer_from_the_pinned_tag(tmp_path):
    res, calls = run_install(tmp_path, "0.1.6", bridge=True)
    assert res.returncode == 0, res.stderr
    assert f"https://raw.githubusercontent.com/NVIDIA/OpenShell/{VERSION}/install.sh" in calls
    assert "/main/" not in calls


def test_install_applies_bridge_networking_when_missing(tmp_path):
    res, calls = run_install(tmp_path, VERSION.lstrip("v"), bridge=False)
    assert "brew --prefix" in calls and "curl" not in calls  # bridge script ran (and stopped at the fake brew)
    assert res.returncode != 0
