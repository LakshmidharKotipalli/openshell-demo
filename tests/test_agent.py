"""The agent must not give up after a fixed number of tries."""
import importlib.util
import json
from pathlib import Path

import pytest

spec = importlib.util.spec_from_file_location("agent_mod", Path(__file__).parent.parent / "agent" / "agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)

OK = json.dumps({"choices": [{"message": {"content": "hi"}}]})


@pytest.fixture(autouse=True)
def no_sleep(monkeypatch):
    monkeypatch.setattr(agent.time, "sleep", lambda s: None)


def test_default_has_no_step_limit():
    assert agent.MAX_STEPS == 0  # 0 = unlimited


def test_transient_errors_are_retried_far_beyond_old_limit(monkeypatch):
    calls = {"n": 0}

    def fake_http(url, **kw):
        calls["n"] += 1
        if calls["n"] < 25:  # old code gave up after 3-4 attempts
            return (503, "overloaded") if calls["n"] % 2 else (429, "rate limited")
        return 200, OK

    monkeypatch.setattr(agent, "http", fake_http)
    msg = agent.chat([{"role": "user", "content": "x"}], "m", "k")
    assert msg["content"] == "hi" and calls["n"] == 25


def test_network_exceptions_and_garbage_replies_are_retried(monkeypatch):
    seq = iter([OSError("boom"), (200, "<html>not json</html>"), (200, OK)])

    def fake_http(url, **kw):
        item = next(seq)
        if isinstance(item, Exception):
            raise item
        return item

    monkeypatch.setattr(agent, "http", fake_http)
    assert agent.chat([], "m", "k")["content"] == "hi"


def test_auth_error_fails_fast_not_forever(monkeypatch):
    calls = {"n": 0}

    def fake_http(url, **kw):
        calls["n"] += 1
        return 401, "bad key"

    monkeypatch.setattr(agent, "http", fake_http)
    with pytest.raises(RuntimeError, match="401"):
        agent.chat([], "m", "k")
    assert calls["n"] == 1


def test_policy_block_is_never_retried(monkeypatch):
    def fake_http(url, **kw):
        raise agent.PolicyBlocked("openrouter.ai", "refused")

    monkeypatch.setattr(agent, "http", fake_http)
    with pytest.raises(agent.PolicyBlocked):
        agent.chat([], "m", "k")
