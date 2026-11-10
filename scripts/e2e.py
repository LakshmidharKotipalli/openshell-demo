#!/usr/bin/env python3
"""End-to-end driver: talks to the running app over its WebSocket exactly like the browser.

Flow:  strict run -> denial -> "Allow this host" (live policy apply) -> retry -> result.
With --record FILE it also writes the server messages (with timing) as a replay recording.

Usage:  .venv/bin/python scripts/e2e.py [--url ws://127.0.0.1:8000/ws] [--record recordings/success.json]
"""
from __future__ import annotations

import argparse
import asyncio
import json
import sys
import time

import websockets

PASS, FAIL = "\033[32mPASS\033[0m", "\033[31mFAIL\033[0m"
failures = 0


def check(ok: bool, what: str, extra: str = "") -> None:
    global failures
    print(f"  [{PASS if ok else FAIL}] {what}" + (f"  ({extra})" if extra else ""))
    if not ok:
        failures += 1


async def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", default="ws://127.0.0.1:8000/ws")
    ap.add_argument("--target", default="https://en.wikipedia.org/wiki/Sandbox_(computer_security)")
    ap.add_argument("--task", default="Research this page and write a short summary of what a sandbox is in computer security.")
    ap.add_argument("--record", default="")
    ap.add_argument("--keep", action="store_true", help="do not delete the sandbox at the end")
    a = ap.parse_args()

    segments: list[dict] = []
    cur: dict | None = None
    last_t = time.monotonic()
    events: list[dict] = []
    state: dict = {}
    result: dict | None = None
    policy_applied: dict | None = None
    errors: list[str] = []

    async with websockets.connect(a.url, max_size=4_000_000) as ws:
        async def pump() -> None:
            nonlocal cur, last_t, state, result, policy_applied
            async for raw in ws:
                m = json.loads(raw)
                now = time.monotonic()
                if cur is not None and m["type"] not in ("ack", "sandboxes"):
                    cur["messages"].append({"dt": round(now - last_t, 2), "msg": m})
                last_t = now
                if m["type"] == "event":
                    events.append(m)
                    if m["kind"] == "policy_applied":
                        policy_applied = m
                elif m["type"] == "state":
                    state = m
                elif m["type"] == "result":
                    result = m
                elif m["type"] == "error":
                    errors.append(m["message"])

        pumper = asyncio.create_task(pump())

        async def command(cmd: str, **kw) -> None:
            nonlocal cur, last_t
            cur = {"cmd": cmd, "messages": []}
            segments.append(cur)
            last_t = time.monotonic()
            await ws.send(json.dumps({"id": cmd, "type": cmd, **kw}))

        async def wait_for(pred, timeout: float, what: str) -> bool:
            t0 = time.monotonic()
            while time.monotonic() - t0 < timeout:
                if pred():
                    return True
                await asyncio.sleep(0.2)
            print(f"  timeout waiting for {what}")
            return False

        await ws.send(json.dumps({"id": "h", "type": "hello", "last_seq": 0}))
        await asyncio.sleep(0.5)

        if state.get("sandbox"):  # start from a clean sandbox so the strict policy applies
            await command("delete_sandbox", name="")
            await wait_for(lambda: state.get("sandbox") is None, 90, "previous sandbox deleted")
        events.clear()
        errors.clear()

        print("1) strict policy: run the task, expect a policy denial")
        await command("run_task", task=a.task, url=a.target)
        ok = await wait_for(lambda: state.get("status") in ("blocked", "done", "error"), 240, "run to settle")
        denial = next((e for e in events if e["kind"] == "denial"), None)
        check(ok and state.get("status") == "blocked", "run ended blocked by policy", f"status={state.get('status')} errors={errors[-1:] }")
        check(denial is not None, "structured denial event emitted",
              f"{denial['host']}:{denial['port']} {denial['binary']}" if denial else "")
        if not denial:
            pumper.cancel()
            return 1

        print("2) 'Allow this host': minimal rule applied live")
        n_before = len([e for e in events if e["kind"] == "policy_applied"])
        await command("allow_host", host=denial["host"], port=denial["port"], binary=denial["binary"])
        ok = await wait_for(lambda: len([e for e in events if e["kind"] == "policy_applied"]) > n_before, 90, "policy_applied")
        check(ok, "policy applied", f"{policy_applied['seconds']}s, rule {policy_applied.get('rule')}" if policy_applied else "")
        await asyncio.sleep(1.0)

        print("3) retry: should now succeed")
        result = None
        await command("retry")
        await wait_for(lambda: state.get("status") == "running", 30, "retry to start")
        ok = await wait_for(lambda: state.get("status") in ("done", "error", "blocked"), 240, "retry to settle")
        await asyncio.sleep(0.5)
        check(state.get("status") == "done", "retry finished OK", f"status={state.get('status')} errors={errors[-1:]}")
        check(bool(result and result.get("text")), "result text received", f"{len(result['text'])} chars" if result else "")

        if not a.keep:
            await command("delete_sandbox", name="")
            await wait_for(lambda: state.get("sandbox") is None, 60, "sandbox deleted")
        pumper.cancel()

    if a.record and failures == 0:
        # keep only the segments the replay needs
        keep = [s for s in segments if s["cmd"] in ("run_task", "allow_host", "retry")]
        with open(a.record, "w") as f:
            json.dump({"version": 1, "recorded": time.strftime("%Y-%m-%d %H:%M:%S"), "segments": keep}, f, indent=1)
        print(f"recording written: {a.record} ({sum(len(s['messages']) for s in keep)} messages)")
    print("RESULT:", "ALL PASS" if failures == 0 else f"{failures} FAILED")
    return 0 if failures == 0 else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
