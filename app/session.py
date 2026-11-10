"""Console session: owns the sandbox, the event log, and the live/replay orchestration."""
from __future__ import annotations

import asyncio
import json
import os
import secrets
import time
from pathlib import Path
from typing import Any

from fastapi import WebSocket

from . import parsing
from .openshell_client import NAME_PREFIX, OpenShellClient, OpenShellError

ROOT = Path(__file__).resolve().parent.parent
IMAGE = os.environ.get("AGENT_IMAGE", "python:3.12-slim")
PROVIDER = os.environ.get("PROVIDER_NAME", "openrouter-agent")
MODEL = os.environ.get("MODEL", "nvidia/nemotron-3.5-lightning:free")
PYTHON_BIN = "/usr/local/bin/python3.12"
WORKSPACE = "/sandbox/workspace"
RUN_TIMEOUT = int(os.environ.get("RUN_TIMEOUT", "900"))  # wall-clock safety net only; 0 = none
IDLE_TTL = int(os.environ.get("IDLE_TTL", "1800"))
PRESETS = {"strict": ROOT / "policies" / "strict.yaml", "research": ROOT / "policies" / "research.yaml"}
RECORDING = ROOT / "recordings" / "success.json"
TMP = ROOT / ".cache" / "policies"


class Busy(Exception):
    pass


class Session:
    def __init__(self, client: OpenShellClient, *, replay: bool = False):
        self.client = client
        self.replay = replay
        self.log: list[dict] = []
        self.clients: set[WebSocket] = set()
        self.sandbox: str | None = None
        self.preset = "strict"
        self.status = "idle"  # idle | creating | running | blocked | done | error
        self.last_task: dict | None = None
        self.seen_denials: dict[str, parsing.Denial] = {}
        self.lock = asyncio.Lock()
        self.last_activity = time.monotonic()
        self.result_text = ""
        self._segments: dict[str, list[list[dict]]] = {}
        self._played: dict[str, int] = {}
        if replay:
            self._load_recording()

    # ---- broadcasting ----------------------------------------------------
    async def send(self, ws: WebSocket, msg: dict) -> None:
        try:
            await ws.send_text(json.dumps(msg))
        except Exception:  # noqa: BLE001 - client went away
            self.clients.discard(ws)

    async def broadcast(self, msg: dict) -> None:
        for ws in list(self.clients):
            await self.send(ws, msg)

    async def emit(self, kind: str, **data: Any) -> None:
        ev = {"type": "event", "seq": len(self.log) + 1, "kind": kind, "ts": round(time.time(), 3), **data}
        self.log.append(ev)
        await self.broadcast(ev)

    async def set_status(self, status: str) -> None:
        self.status = status
        await self.broadcast(self.state_msg())

    def state_msg(self) -> dict:
        return {"type": "state", "status": self.status, "sandbox": self.sandbox, "preset": self.preset,
                "mode": "replay" if self.replay else "live", "model": MODEL, "provider": PROVIDER,
                "can_retry": self.last_task is not None and self.status in ("blocked", "error", "done")}

    async def on_connect(self, ws: WebSocket, last_seq: int = 0) -> None:
        self.clients.add(ws)
        await self.send(ws, self.state_msg())
        # Backlog is replayed when the client sends {"type":"hello","last_seq":N} (see main.py).
        await self.send_policy(ws)

    # ---- command dispatch ------------------------------------------------
    async def handle(self, ws: WebSocket, msg: Any) -> None:
        self.last_activity = time.monotonic()
        t = msg.type
        if t == "hello":
            return
        if t == "list_sandboxes":
            return await self.send(ws, {"type": "sandboxes", "items": await self.list_sandboxes()})
        if t == "get_policy":
            return await self.send_policy(ws)
        if self.lock.locked():
            raise Busy("another operation is still running")
        async with self.lock:
            if t == "run_task":
                self.last_task = {"task": msg.task, "url": msg.url}
                self.seen_denials.clear()
                await (self._play("run_task") if self.replay else self.run_agent())
            elif t == "retry":
                if not self.last_task:
                    raise ValueError("nothing to retry yet")
                await (self._play("retry") if self.replay else self.run_agent())
            elif t == "apply_preset":
                await (self._play("apply_preset", fallback=lambda: self._replay_preset(msg.preset))
                       if self.replay else self.apply_preset(msg.preset))
            elif t == "apply_policy":
                if self.replay:
                    raise ValueError("custom policy is disabled in replay mode")
                await self.apply_policy_yaml(msg.yaml, label="custom")
            elif t == "allow_host":
                await (self._play("allow_host") if self.replay else self.allow_host(msg.host, msg.port, msg.binary))
            elif t == "delete_sandbox":
                await self.delete_sandbox(msg.name)

    # ---- sandbox lifecycle -----------------------------------------------
    async def ensure_sandbox(self) -> str:
        if self.sandbox:
            return self.sandbox
        if PROVIDER not in await self.client.provider_names():
            raise OpenShellError(f"provider '{PROVIDER}' not found — run `make setup` first")
        name = NAME_PREFIX + secrets.token_hex(3)
        await self.set_status("creating")
        await self.emit("sandbox_creating", name=name, image=IMAGE, policy="strict", provider=PROVIDER)
        t0 = time.monotonic()
        await self.client.create_sandbox(name, image=IMAGE, policy_path=str(PRESETS["strict"]), providers=[PROVIDER])
        self.sandbox = name  # from here on cleanup knows about it
        await self.client.upload(name, str(ROOT / "agent" / "agent.py"))
        self.preset = "strict"
        await self.emit("sandbox_ready", name=name, seconds=round(time.monotonic() - t0, 1))
        await self.set_status("idle")
        await self.broadcast_policy()
        return name

    async def delete_sandbox(self, name: str = "") -> None:
        target = name or self.sandbox
        if not target:
            return
        if not self.replay:
            await self.client.delete_sandbox(target)
        if target == self.sandbox:
            self.sandbox = None
            self.last_task = None
            self.seen_denials.clear()
            self.preset = "strict"
        if target == self.sandbox or self.sandbox is None:
            self.log.clear()  # new session: reconnecting clients must not replay the old timeline
            self.result_text = ""
            await self.broadcast({"type": "reset"})
        await self.emit("sandbox_deleted", name=target)
        await self.set_status("idle")
        await self.broadcast_policy()

    async def list_sandboxes(self) -> list[dict]:
        if self.replay:
            return [{"name": self.sandbox, "phase": "Ready"}] if self.sandbox else []
        out = []
        for sb in await self.client.list_sandboxes():
            if str(sb.get("name", "")).startswith(NAME_PREFIX):
                out.append({"name": sb["name"], "phase": sb.get("phase", "?"), "created": sb.get("created_at", "")})
        return out

    async def cleanup(self) -> None:
        """Called on app shutdown: remove every demo sandbox this gateway still holds."""
        if self.replay:
            return
        try:
            names = await self.client.list_demo_sandbox_names()
            if names:
                await self.client.delete_sandbox(*names)
        except Exception:  # noqa: BLE001
            pass

    async def idle_reaper(self) -> None:
        while True:
            await asyncio.sleep(30)
            if self.sandbox and not self.lock.locked() and time.monotonic() - self.last_activity > IDLE_TTL:
                await self.emit("info", message=f"idle > {IDLE_TTL}s: deleting sandbox")
                await self.delete_sandbox()

    # ---- running the agent -----------------------------------------------
    async def run_agent(self) -> None:
        try:
            name = await self.ensure_sandbox()
        except OpenShellError as e:
            await self.emit("error", message=str(e))
            await self.set_status("error")
            return
        task = self.last_task or {}
        start = time.time()
        await self.set_status("running")
        await self.emit("run_started", task=task["task"], url=task["url"], model=MODEL, preset=self.preset)
        seen: set[tuple] = set()
        stop = asyncio.Event()
        watcher = asyncio.create_task(self._watch_denials(name, start, seen, stop))
        rc, agent_final, agent_err, denied_host = None, None, "", None
        argv = ["python3", "agent.py", "--task", task["task"], "--url", task["url"], "--model", MODEL]
        if os.environ.get("AGENT_SELFTEST"):  # dev/CI: skip the LLM, exercise fetch + policy only
            argv.append("--selftest")
        try:
            async for tag, line in self.client.exec_stream(name, argv, timeout=RUN_TIMEOUT):
                if tag == "stdout":
                    obj = parsing.parse_agent_line(line)
                    if not obj:
                        continue
                    typ = obj.pop("type")
                    if typ == "final":
                        agent_final = obj
                    elif typ == "policy_denied":
                        denied_host = obj.get("host")
                    elif typ == "error":
                        agent_err = obj.get("message", "")
                    await self.emit("agent." + typ, **obj)
                elif tag == "stderr" and line.strip():
                    await self.emit("stderr", line=line[:300])
                elif tag == "exit":
                    rc = int(line)
        except OpenShellError as e:
            await self.emit("error", message=str(e))
        finally:
            stop.set()
            await asyncio.gather(watcher, return_exceptions=True)

        if rc == 3 or denied_host:
            await self._ensure_denial_event(name, start, seen, denied_host)
            await self.emit("blocked", message="Request blocked by sandbox policy. Review it below.")
            await self.set_status("blocked")
        elif rc == 0 and agent_final and agent_final.get("status") == "ok":
            out_file = agent_final.get("file") or "summary.md"
            res = await self.client.exec_capture(name, ["cat", f"{WORKSPACE}/{out_file}"], timeout=20)
            text = res.stdout if res.returncode == 0 else (agent_final.get("summary") or "")
            await self.broadcast({"type": "result", "text": text, "file": out_file, "summary": agent_final.get("summary", "")})
            self.result_text = text
            await self.emit("done", message="Task completed", file=out_file)
            await self.set_status("done")
        else:
            msg = agent_err or (agent_final or {}).get("summary") or f"agent exited with code {rc}"
            await self.emit("error", message=msg)
            await self.set_status("error")

    async def _watch_denials(self, name: str, start: float, seen: set, stop: asyncio.Event) -> None:
        while not stop.is_set():
            await self._poll_denials(name, start, seen)
            try:
                await asyncio.wait_for(stop.wait(), timeout=1.5)
            except asyncio.TimeoutError:
                pass

    async def _poll_denials(self, name: str, start: float, seen: set) -> None:
        try:
            denials = await self.client.denials(name)
        except OpenShellError:
            return
        for d in denials:
            if d.ts >= start - 1 and d.key not in seen:
                seen.add(d.key)
                self.seen_denials[d.host] = d
                await self.emit("denial", **d.to_dict())

    async def _ensure_denial_event(self, name: str, start: float, seen: set, agent_host: str | None) -> None:
        for _ in range(6):  # logs can lag the agent by a moment
            await self._poll_denials(name, start, seen)
            if not agent_host or agent_host in self.seen_denials:
                return
            await asyncio.sleep(1)
        if agent_host and agent_host not in self.seen_denials:  # fall back to what the agent reported
            d = parsing.Denial(host=agent_host, port=443, binary=PYTHON_BIN, reason="reported_by_agent", ts=time.time())
            self.seen_denials[d.host] = d
            await self.emit("denial", **d.to_dict())

    # ---- policy ----------------------------------------------------------
    async def current_policy(self) -> tuple[str, dict]:
        if self.replay or not self.sandbox:
            doc = parsing.load_policy_file(str(PRESETS[self.preset]))
            return parsing.dump_yaml(doc), {}
        base, meta = await self.client.policy_base(self.sandbox)
        return parsing.dump_yaml(base), meta

    async def send_policy(self, ws: WebSocket) -> None:
        try:
            text, meta = await self.current_policy()
            await self.send(ws, {"type": "policy", "yaml": text, "preset": self.preset,
                                 "version": meta.get("active_version"), "sandbox": self.sandbox})
        except OpenShellError as e:
            await self.send(ws, {"type": "error", "ref": "", "message": str(e)})

    async def broadcast_policy(self, applied_seconds: float | None = None) -> None:
        text, meta = await self.current_policy()
        await self.broadcast({"type": "policy", "yaml": text, "preset": self.preset, "version": meta.get("active_version"),
                              "sandbox": self.sandbox,
                              "applied_seconds": None if applied_seconds is None else round(applied_seconds, 1)})

    async def _apply(self, policy: dict, *, label: str, rule: str | None = None) -> float:
        problems = parsing.lint_policy(policy)
        if problems:
            raise ValueError("policy lint: " + "; ".join(problems))
        TMP.mkdir(parents=True, exist_ok=True)
        path = TMP / f"{self.sandbox}-{int(time.time())}.yaml"
        path.write_text(parsing.dump_yaml(policy))
        await self.emit("policy_applying", label=label, rule=rule)
        secs = await self.client.policy_set(self.sandbox, str(path), wait=True, timeout=60)
        await self.emit("policy_applied", label=label, rule=rule, seconds=round(secs, 1))
        return secs

    async def apply_preset(self, preset: str) -> None:
        name = await self.ensure_sandbox()
        base, _ = await self.client.policy_base(name)
        target = parsing.load_policy_file(str(PRESETS[preset]))
        secs = await self._apply(parsing.replace_network(base, target.get("network_policies")), label=f"preset:{preset}")
        self.preset = preset
        await self.set_status(self.status)
        await self.broadcast_policy(secs)

    async def apply_policy_yaml(self, text: str, label: str) -> None:
        import yaml

        try:
            doc = yaml.safe_load(text)
        except yaml.YAMLError as e:
            raise ValueError(f"invalid YAML: {e}") from e
        if not isinstance(doc, dict):
            raise ValueError("policy must be a YAML mapping")
        name = await self.ensure_sandbox()
        secs = await self._apply(doc, label=label)
        self.preset = "custom"
        await self.set_status(self.status)
        await self.broadcast_policy(secs)
        _ = name

    async def allow_host(self, host: str, port: int, binary: str) -> None:
        if not self.sandbox:
            raise ValueError("no sandbox")
        seen = self.seen_denials.get(host)
        if not seen:
            raise ValueError(f"{host} was not denied in this session")
        binary = binary or seen.binary or PYTHON_BIN
        rule_name, rule = parsing.build_allow_rule(host, port or seen.port, binary)
        base, _ = await self.client.policy_base(self.sandbox)
        secs = await self._apply(parsing.merge_rule(base, rule_name, rule), label=f"allow:{host}", rule=rule_name)
        self.preset = "custom" if self.preset == "strict" else self.preset
        await self.set_status("blocked" if self.status == "blocked" else self.status)
        await self.broadcast_policy(secs)

    # ---- replay ----------------------------------------------------------
    def _load_recording(self) -> None:
        if not RECORDING.exists():
            return
        doc = json.loads(RECORDING.read_text())
        for seg in doc.get("segments", []):
            self._segments.setdefault(seg["cmd"], []).append(seg["messages"])

    async def _play(self, cmd: str, fallback=None) -> None:
        segs = self._segments.get(cmd)
        if not segs:
            if fallback:
                return await fallback()
            raise ValueError(f"no recorded segment for '{cmd}' — record one with `make record`")
        i = self._played.get(cmd, 0)
        self._played[cmd] = i + 1
        seg = segs[min(i, len(segs) - 1)]
        for item in seg:
            await asyncio.sleep(min(float(item.get("dt", 0)), 2.5))
            m = dict(item["msg"])
            if m.get("type") == "event":
                kind = m.pop("kind")
                for k in ("type", "seq", "ts"):
                    m.pop(k, None)
                await self.emit(kind, **m)
            else:
                if m.get("type") == "state":
                    self.status = m.get("status", self.status)
                    self.sandbox = m.get("sandbox", self.sandbox)
                    self.preset = m.get("preset", self.preset)
                    m = self.state_msg()
                await self.broadcast(m)

    async def _replay_preset(self, preset: str) -> None:
        self.preset = preset
        await self.emit("policy_applied", label=f"preset:{preset}", rule=None, seconds=0.0)
        await self.broadcast_policy(0.0)
        await self.broadcast(self.state_msg())
