"""Adapter tests: argv construction, JSON parsing of real `sandbox list -o json`, secret scrubbing
and timeouts, using a fake `openshell` executable (no gateway needed)."""
import asyncio
import os
import stat
from pathlib import Path

import pytest

from app.openshell_client import OpenShellClient, OpenShellError

FIX = Path(__file__).parent / "fixtures"


def fake_cli(tmp_path: Path, body: str) -> str:
    p = tmp_path / "openshell"
    p.write_text("#!/bin/sh\n" + body)
    p.chmod(p.stat().st_mode | stat.S_IEXEC)
    return str(p)


def run(coro):
    return asyncio.run(coro)


def test_list_sandboxes_parses_real_fixture(tmp_path):
    cli = fake_cli(tmp_path, f"cat {FIX}/sandbox_list.json\n")
    c = OpenShellClient(binary=cli)
    sbs = run(c.list_sandboxes())
    assert sbs[0]["name"] == "osc-probe" and sbs[0]["phase"] == "Ready"


def test_policy_base_parses_real_fixture(tmp_path):
    cli = fake_cli(tmp_path, f"cat {FIX}/policy_get_base.json\n")
    policy, meta = run(OpenShellClient(binary=cli).policy_base("x"))
    assert meta["active_version"] == 2
    assert "probe_httpbin" in policy["network_policies"]


def test_nonzero_exit_raises_with_message(tmp_path):
    cli = fake_cli(tmp_path, 'echo "Error: nope" >&2; exit 3\n')
    with pytest.raises(OpenShellError) as e:
        run(OpenShellClient(binary=cli).status())
    assert e.value.returncode == 3 and "nope" in str(e.value)


def test_secret_is_scrubbed_from_errors(tmp_path, monkeypatch):
    monkeypatch.setenv("OPENROUTER_API_KEY", "sk-or-v1-supersecretvalue123")
    cli = fake_cli(tmp_path, 'echo "bad key sk-or-v1-supersecretvalue123" >&2; exit 1\n')
    with pytest.raises(OpenShellError) as e:
        run(OpenShellClient(binary=cli).status())
    assert "supersecretvalue" not in str(e.value) and "supersecretvalue" not in e.value.stderr
    assert "***" in str(e.value)


def test_timeout_kills_and_raises(tmp_path):
    cli = fake_cli(tmp_path, "exec sleep 30\n")  # exec: kill() must hit the sleeper, not a parent shell
    with pytest.raises(OpenShellError) as e:
        run(OpenShellClient(binary=cli)._run(["status"], timeout=1))
    assert e.value.returncode == 124


def test_exec_stream_yields_lines_and_exit_code(tmp_path):
    cli = fake_cli(tmp_path, 'echo \'{"type":"step"}\'; echo oops >&2; exit 3\n')

    async def go():
        return [x async for x in OpenShellClient(binary=cli).exec_stream("sb", ["python3", "agent.py"], timeout=20)]

    items = run(go())
    assert ("stdout", '{"type":"step"}') in items
    assert ("stderr", "oops") in items
    assert items[-1] == ("exit", "3")


def test_create_sandbox_argv(tmp_path):
    log = tmp_path / "argv.txt"
    cli = fake_cli(tmp_path, f'echo "$@" > {log}\n')
    run(OpenShellClient(binary=cli).create_sandbox("osc-demo-x", image="python:3.12-slim", policy_path="p.yaml", providers=["prov"]))
    argv = log.read_text()
    for needle in ("sandbox create", "--name osc-demo-x", "--from python:3.12-slim", "--policy p.yaml", "--provider prov",
                   "--label app=osc-demo", "--detach", "-- sleep infinity", "--color never"):
        assert needle in argv, needle
    assert "KEY" not in argv  # credentials are never passed on a command line
