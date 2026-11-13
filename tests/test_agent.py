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


def test_tool_call_arguments_tolerate_objects_and_garbage():
    def call(arguments):
        return agent.tool_calls_from({"tool_calls": [{"id": "1", "function": {"name": "fetch_url", "arguments": arguments}}]})[0]["args"]

    assert call('{"url": "https://a.example/"}') == {"url": "https://a.example/"}
    assert call({"url": "https://a.example/"}) == {"url": "https://a.example/"}  # already an object
    assert call("[1, 2]") == {} and call("null") == {} and call("{broken") == {} and call(None) == {}


def test_host_ignores_userinfo_and_port():
    assert agent._host("https://user:pw@En.Wikipedia.org:8443/wiki/X") == "en.wikipedia.org"
    assert agent._host("not a url") == "not a url"


def test_parallel_tool_calls_keep_history_valid(monkeypatch, tmp_path, capsys):
    """The agent runs one tool per turn, so the assistant turn it stores must carry exactly one tool_call
    (an unanswered tool_call id makes the next request fail with HTTP 400). Non-string content must not crash."""
    def tc(i, name, args):
        return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}

    replies = iter([
        {"content": "", "tool_calls": [tc(1, "fetch_url", {"url": "https://a.example/"}),
                                       tc(2, "write_file", {"path": "summary.md", "content": "early"})]},
        {"content": "", "tool_calls": [tc(3, "write_file", {"path": "summary.md", "content": 123})]},
        {"content": "", "tool_calls": [tc(4, "write_file", {"path": "summary.md", "content": "- a sandbox"})]},
        {"content": "done"},
    ])
    seen = []

    def fake_chat(messages, model, key, **kw):
        seen.append(json.loads(json.dumps(messages)))
        return next(replies)

    monkeypatch.setattr(agent, "chat", fake_chat)
    monkeypatch.setattr(agent, "http", lambda url, **kw: (200, "<p>page text</p>"))
    monkeypatch.setattr(agent, "WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "openshell:resolve:env:X")
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "--task=-starts with a dash", "--url=https://a.example/"])

    assert agent.main() == 0
    assert (tmp_path / "summary.md").read_text() == "- a sandbox"
    for messages in seen:
        ids = [c["id"] for m in messages if m["role"] == "assistant" for c in m.get("tool_calls", [])]
        answered = [m["tool_call_id"] for m in messages if m["role"] == "tool"]
        assert ids == answered
    events = [json.loads(line) for line in capsys.readouterr().out.splitlines()]
    assert events[0]["task"] == "-starts with a dash" and events[-1]["status"] == "ok"


def test_json_in_text_tool_calls_single_line_and_pretty_printed():
    one_line = 'I will fetch it. {"tool": "fetch_url", "args": {"url": "https://a.example/"}} Then summarise.'
    pretty = 'Sure:\n```json\n{\n  "name": "write_file",\n  "arguments": {\n    "path": "summary.md",\n    "content": "- {braces} ok"\n  }\n}\n```'
    assert agent.tool_calls_from({"content": one_line}) == [
        {"id": "txt0", "name": "fetch_url", "args": {"url": "https://a.example/"}, "native": False}]
    got = agent.tool_calls_from({"content": pretty})
    assert [(c["name"], c["args"]["content"]) for c in got] == [("write_file", "- {braces} ok")]
    # not tool calls: unknown tool, args not an object, broken JSON, plain prose with braces
    for text in ('{"tool": "rm_rf", "args": {}}', '{"tool": "fetch_url", "args": "x"}', '{"tool": "fetch_url", ', "use {curly} words"):
        assert agent.tool_calls_from({"content": text}) == []


def _run_agent(monkeypatch, tmp_path, capsys, replies, http_body="<p>page</p>"):
    it = iter(replies)
    monkeypatch.setattr(agent, "chat", lambda *a, **k: next(it))
    monkeypatch.setattr(agent, "http", lambda url, **kw: (200, http_body))
    monkeypatch.setattr(agent, "WORKSPACE", str(tmp_path))
    monkeypatch.setenv("OPENROUTER_API_KEY", "openshell:resolve:env:X")
    monkeypatch.setattr(agent.sys, "argv", ["agent.py", "--task=t", "--url=https://a.example/"])
    rc = agent.main()
    return rc, [json.loads(l) for l in capsys.readouterr().out.splitlines()]


def _tc(i, name, args):
    return {"id": f"c{i}", "type": "function", "function": {"name": name, "arguments": json.dumps(args)}}


def test_repeating_the_same_tool_call_is_warned_then_stopped(monkeypatch, tmp_path, capsys):
    same = lambda i: {"content": "", "tool_calls": [_tc(i, "fetch_url", {"url": "https://a.example/"})]}
    rc, events = _run_agent(monkeypatch, tmp_path, capsys, (same(i) for i in range(100)))  # never runs out
    results = [e for e in events if e["type"] == "tool_result"]
    assert rc == 4 and events[-1]["status"] == "stalled"
    assert [r["ok"] for r in results] == [True, True, False]  # 3rd call not executed, model told it is looping
    assert "already made this exact" in results[-1]["preview"]


def test_model_that_never_calls_tools_is_stopped(monkeypatch, tmp_path, capsys):
    rc, events = _run_agent(monkeypatch, tmp_path, capsys, ({"content": "I think so."} for _ in range(100)))
    assert rc == 4 and events[-1]["status"] == "stalled"
    assert len([e for e in events if e["type"] == "llm_request"]) == agent.MAX_NUDGES + 1


def test_varied_calls_and_one_recovery_are_not_flagged(monkeypatch, tmp_path, capsys):
    replies = [
        {"content": "let me think"},  # one nudge is fine and the counter resets afterwards
        {"content": "", "tool_calls": [_tc(1, "fetch_url", {"url": "https://a.example/"})]},
        {"content": "", "tool_calls": [_tc(2, "write_file", {"path": "summary.md", "content": "- ok"})]},
        {"content": "done"},
    ]
    rc, events = _run_agent(monkeypatch, tmp_path, capsys, replies)
    assert rc == 0 and events[-1]["status"] == "ok"
