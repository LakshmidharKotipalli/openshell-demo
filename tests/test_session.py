"""Session orchestration with a fake adapter (no gateway, no CLI)."""
import asyncio
import json

import pytest

from app import session as session_mod
from app.openshell_client import CmdResult, OpenShellError
from app.session import Session


class FakeWS:
    def __init__(self):
        self.sent: list[dict] = []

    async def send_text(self, text: str) -> None:
        self.sent.append(json.loads(text))

    def of(self, type_: str) -> list[dict]:
        return [m for m in self.sent if m["type"] == type_]


class FakeClient:
    def __init__(self, lines=(), upload_error=False):
        self.lines, self.upload_error = list(lines), upload_error
        self.deleted: list[str] = []
        self.exec_calls: list[tuple] = []
        self.hang = False  # exec_stream emits one line then never ends (an agent stuck in a loop)
        self.killed: list[str] = []
        self.stream_closed = False

    async def provider_names(self):
        return [session_mod.PROVIDER]

    async def create_sandbox(self, name, **kw):
        return None

    async def upload(self, name, local, dest=None, timeout=60):
        if self.upload_error:
            raise OpenShellError("upload failed")

    async def delete_sandbox(self, *names, timeout=90):
        self.deleted += names

    async def policy_base(self, name):
        return {"version": 1, "network_policies": {}}, {"active_version": 1}

    async def denials(self, name, since=None):
        return []

    async def exec_stream(self, name, argv, *, timeout=180, env=None):
        self.exec_calls.append((argv, env))
        try:
            for line in self.lines:
                yield "stdout", line
            if self.hang:
                await asyncio.sleep(3600)
            yield "exit", "0"
        finally:
            self.stream_closed = True

    async def kill_agent(self, name):
        self.killed.append(name)
        return 1

    async def exec_capture(self, name, argv, *, timeout=60):
        return CmdResult(0, "file text", "", 0.0)


def run(coro):
    return asyncio.run(coro)


class Msg:
    def __init__(self, type_, **kw):
        self.type = type_
        self.__dict__.update(kw)


def test_delete_refuses_sandboxes_this_app_did_not_create():
    async def go():
        c = FakeClient()
        s = Session(c)
        with pytest.raises(ValueError, match="not a demo sandbox"):
            await s.delete_sandbox("production-db")
        assert c.deleted == []
        await s.emit("info", message="kept")
        await s.delete_sandbox("osc-demo-other")  # someone else's demo sandbox: deleted, our timeline kept
        assert c.deleted == ["osc-demo-other"] and s.log[0]["message"] == "kept"

    run(go())


def test_upload_failure_does_not_leave_a_broken_session_sandbox():
    async def go():
        c = FakeClient(upload_error=True)
        s = Session(c)
        with pytest.raises(OpenShellError):
            await s.ensure_sandbox()
        assert s.sandbox is None and len(c.deleted) == 1

    run(go())


def test_run_forwards_task_safely_and_keeps_event_envelope(monkeypatch):
    monkeypatch.setenv("AGENT_MAX_STEPS", "7")
    lines = [
        json.dumps({"type": "tool_result", "seq": 99999, "kind": "spoof", "step": 1, "ok": True}),
        json.dumps({"type": "final", "status": "ok", "summary": "s", "file": "summary.md"}),
    ]

    async def go():
        c = FakeClient(lines)
        s = Session(c)
        ws = FakeWS()
        await s.on_connect(ws)
        await s.handle(ws, Msg("run_task", task="--selftest", url="https://a.example/"))
        argv, env = c.exec_calls[0]
        assert "--task=--selftest" in argv and "--selftest" not in argv  # cannot be parsed as an option
        assert env == {"AGENT_MAX_STEPS": "7"}
        assert [e["seq"] for e in s.log] == list(range(1, len(s.log) + 1))
        assert any(e["kind"] == "agent.tool_result" for e in s.log) and all(e["kind"] != "spoof" for e in s.log)
        assert s.status == "done"
        late = FakeWS()  # a page opened after the run still gets the result
        await s.on_connect(late)
        assert late.of("result")[0]["text"] == "file text"

    run(go())


def test_replay_survives_reconnect_after_allow_host():
    async def go():
        s = Session(FakeClient(), replay=True)
        monkeypatch_sleep = asyncio.sleep

        async def fast(_):
            await monkeypatch_sleep(0)

        session_mod.asyncio.sleep = fast
        try:
            ws = FakeWS()
            await s.on_connect(ws)
            await s.handle(ws, Msg("run_task", task="t", url="https://en.wikipedia.org/"))
            await s.handle(ws, Msg("allow_host", host="en.wikipedia.org", port=443, binary=""))
            assert s.preset == "custom"  # recorded state: there is no preset file with that name
            late = FakeWS()
            await s.on_connect(late)  # used to raise KeyError('custom') and kill every new connection
            assert "allow_en_wikipedia_org" in late.of("policy")[0]["yaml"]
            await s.handle(ws, Msg("retry"))
            again = FakeWS()
            await s.on_connect(again)
            assert again.of("result") and again.of("result")[0]["file"] == "summary.md"
            await s.handle(ws, Msg("delete_sandbox", name=""))
            assert s.sandbox is None and s.last_result is None and s.preset == "strict"
        finally:
            session_mod.asyncio.sleep = monkeypatch_sleep

    run(go())


def test_live_events_start_only_after_the_backlog_is_replayed():
    """A reconnecting client keeps a high-water mark, so a live event that overtakes the backlog would make it
    drop the older events. Nothing live may be delivered before hello has been answered."""
    async def go():
        s = Session(FakeClient())
        await s.emit("info", message="1")
        await s.emit("info", message="2")
        ws = FakeWS()
        await s.on_connect(ws)
        await s.emit("info", message="3")  # arrives between connect and hello
        assert ws.of("event") == []
        await s.resume(ws, 1)  # the client already has event 1
        assert [e["seq"] for e in ws.of("event")] == [2, 3]
        assert ws.sent[-1]["type"] == "state"
        await s.emit("info", message="4")
        assert [e["seq"] for e in ws.of("event")] == [2, 3, 4]

    run(go())


def test_stop_kills_a_run_that_never_finishes():
    """The kill switch must work while the run holds the session lock, and leave the session reusable."""
    lines = [json.dumps({"type": "llm_request", "step": 1})]

    async def go():
        c = FakeClient(lines)
        c.hang = True
        s = Session(c)
        ws = FakeWS()
        await s.on_connect(ws)
        for round_ in range(2):
            t = asyncio.create_task(s.handle(ws, Msg("run_task", task="t", url="https://a.example/")))
            for _ in range(200):
                await asyncio.sleep(0.01)
                if s.status == "running" and s._runner:
                    break
            await s.handle(ws, Msg("stop"))
            await asyncio.wait_for(t, timeout=5)
            assert s.status == "stopped" and not s.lock.locked()
            assert c.stream_closed and c.killed[-1] == s.sandbox  # host stream torn down AND remote process killed
            assert s.state_msg()["can_retry"] is True
        assert [e["kind"] for e in s.log].count("stopped") == 2  # and a stopped session can run again

    run(go())


def test_stop_when_idle_is_harmless():
    async def go():
        s = Session(FakeClient())
        await s.handle(FakeWS(), Msg("stop"))
        assert s.status == "idle" and s.log[-1]["message"] == "nothing is running"

    run(go())
