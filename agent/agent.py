#!/usr/bin/env python3
"""Tiny tool-using agent that runs INSIDE an OpenShell sandbox. Stdlib only.

Calls an OpenAI-compatible chat-completions endpoint and exposes three tools:
fetch_url, read_file, write_file. Every step is printed to stdout as one JSON object per line
so the host app can stream it to the UI.

Credentials: OPENROUTER_API_KEY in this process is an opaque placeholder injected by OpenShell.
The real key lives in the gateway; the sandbox proxy substitutes it only on requests to the
provider's approved endpoint. This script never sees (or prints) the real key.

Exit codes: 0 ok, 3 blocked by sandbox policy, 1 other failure.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import socket
import sys
import time
import urllib.error
import urllib.parse
import urllib.request

LLM_URL = os.environ.get("LLM_BASE_URL", "https://openrouter.ai/api/v1").rstrip("/") + "/chat/completions"
WORKSPACE = os.environ.get("AGENT_WORKSPACE", "/sandbox/workspace")
MAX_STEPS = int(os.environ.get("AGENT_MAX_STEPS", "0"))  # 0 = no limit on tries
FETCH_LIMIT = 8000

TOOLS = [
    {"type": "function", "function": {
        "name": "fetch_url", "description": "HTTP GET a URL and return its text (HTML tags stripped, truncated).",
        "parameters": {"type": "object", "properties": {"url": {"type": "string"}}, "required": ["url"]}}},
    {"type": "function", "function": {
        "name": "read_file", "description": "Read a UTF-8 text file from the workspace.",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}}, "required": ["path"]}}},
    {"type": "function", "function": {
        "name": "write_file", "description": "Write a UTF-8 text file in the workspace (relative path).",
        "parameters": {"type": "object", "properties": {"path": {"type": "string"}, "content": {"type": "string"}},
                       "required": ["path", "content"]}}},
]


def emit(type_: str, **kw) -> None:
    kw["type"] = type_
    kw["ts"] = round(time.time(), 3)
    print(json.dumps(kw, ensure_ascii=False), flush=True)


class PolicyBlocked(Exception):
    def __init__(self, host: str, detail: str):
        super().__init__(detail)
        self.host, self.detail = host, detail


def _host(url: str) -> str:
    m = re.match(r"^[a-z]+://([^/:]+)", url, re.I)
    return m.group(1).lower() if m else url


def _build_opener() -> urllib.request.OpenerDirector:
    """An opener that can ONLY speak http/https. The default urlopen also registers FileHandler and
    FTPHandler, so a model-chosen file:///etc/passwd or ftp:// URL (or a redirect to one) would bypass the
    workspace restriction of read_file. Landlock still bounds the damage, but the tool must not offer it."""
    o = urllib.request.OpenerDirector()
    for h in (urllib.request.ProxyHandler(), urllib.request.HTTPHandler(), urllib.request.HTTPSHandler(),
              urllib.request.HTTPDefaultErrorHandler(), urllib.request.HTTPRedirectHandler(),
              urllib.request.HTTPErrorProcessor(), urllib.request.UnknownHandler()):
        o.add_handler(h)
    return o


_OPENER = _build_opener()


def check_url(url: str) -> None:
    u = urllib.parse.urlsplit(url.strip())
    if u.scheme.lower() not in ("http", "https") or not u.hostname:
        raise ValueError(f"only absolute http(s) URLs are allowed, got: {url[:80]!r}")


def http(url: str, *, data: bytes | None = None, headers: dict | None = None, timeout: int = 20) -> tuple[int, str]:
    check_url(url)
    req = urllib.request.Request(url, data=data, headers=headers or {})
    try:
        with _OPENER.open(req, timeout=timeout) as r:
            return r.status, r.read().decode("utf-8", "replace")
    except urllib.error.HTTPError as e:
        body = e.read().decode("utf-8", "replace")
        if e.code == 403 and re.search(r"policy|denied|openshell", body, re.I):
            raise PolicyBlocked(_host(url), f"HTTP 403 from sandbox policy: {body[:200]}")
        return e.code, body
    except urllib.error.URLError as e:
        reason = e.reason
        # The sandbox refuses unapproved destinations at connect time (EACCES / refused / reset).
        if isinstance(reason, PermissionError) or getattr(reason, "errno", None) in (13, 111, 104):
            raise PolicyBlocked(_host(url), f"connection refused by sandbox policy ({reason})")
        raise
    except (ConnectionError, socket.timeout) as e:
        if isinstance(e, ConnectionResetError):
            raise PolicyBlocked(_host(url), f"connection reset by sandbox policy ({e})")
        raise


def strip_html(text: str) -> str:
    text = re.sub(r"(?is)<(script|style).*?</\1>", " ", text)
    text = re.sub(r"(?s)<[^>]+>", " ", text)
    return re.sub(r"\s+", " ", text).strip()


def safe_path(rel: str) -> str:
    p = os.path.normpath(os.path.join(WORKSPACE, rel))
    if p != WORKSPACE and not p.startswith(WORKSPACE + os.sep):
        raise ValueError(f"path escapes workspace: {rel}")
    return p


def run_tool(name: str, args: dict) -> str:
    if name == "fetch_url":
        status, body = http(args["url"], headers={"User-Agent": "osc-agent/1.0"})
        text = strip_html(body)
        return f"HTTP {status}\n{text[:FETCH_LIMIT]}"
    if name == "read_file":
        with open(safe_path(args["path"]), encoding="utf-8") as f:
            return f.read()[:FETCH_LIMIT]
    if name == "write_file":
        p = safe_path(args["path"])
        os.makedirs(os.path.dirname(p), exist_ok=True)
        with open(p, "w", encoding="utf-8") as f:
            f.write(args["content"])
        return f"wrote {len(args['content'])} bytes to {os.path.relpath(p, WORKSPACE)}"
    raise ValueError(f"unknown tool {name}")


def chat(messages: list, model: str, key: str, *, timeout: int = 60, attempts: int | None = None, step: int = 0) -> dict:
    """One model call. Transient failures (network, 429/5xx, malformed reply) are retried with capped
    backoff until they succeed; attempts=None means no limit. Auth/request errors (e.g. 401, 400) fail fast
    because retrying them can never help."""
    body = json.dumps({"model": model, "messages": messages, "tools": TOOLS, "tool_choice": "auto",
                       "temperature": 0.2, "max_tokens": 900}).encode()
    headers = {"Content-Type": "application/json", "Authorization": f"Bearer {key}",
               "X-Title": "Sandboxed Agent Console"}
    last, attempt = "", 0
    while attempts is None or attempt < attempts:
        attempt += 1
        retryable = True
        try:
            status, text = http(LLM_URL, data=body, headers=headers, timeout=timeout)
            if status == 200:
                try:
                    return json.loads(text)["choices"][0]["message"]
                except (KeyError, ValueError, IndexError, TypeError):
                    last = f"unexpected response shape: {text[:200]}"
            else:
                last = f"HTTP {status}: {text[:200]}"
                retryable = status in (408, 409, 425, 429) or status >= 500
        except PolicyBlocked:
            raise
        except Exception as e:  # noqa: BLE001 - network hiccup
            last = f"{type(e).__name__}: {e}"
        if not retryable:
            break
        if attempts is None or attempt < attempts:
            emit("llm_retry", step=step, attempt=attempt, reason=last[:160])
            time.sleep(min(1.5 * attempt, 10))
    raise RuntimeError(f"LLM call failed: {last}")


def tool_calls_from(msg: dict) -> list[dict]:
    """Native tool_calls first; fall back to JSON-in-text (free models are flaky at tool calling)."""
    calls = []
    for tc in msg.get("tool_calls") or []:
        fn = tc.get("function", {})
        try:
            args = json.loads(fn.get("arguments") or "{}")
        except ValueError:
            args = {}
        calls.append({"id": tc.get("id", "call"), "name": fn.get("name"), "args": args, "native": True})
    if calls:
        return calls
    content = msg.get("content") or ""
    for m in re.finditer(r"\{.*?\}(?=\s*(?:\{|$|```|\n))", content, re.S):
        try:
            obj = json.loads(m.group(0))
        except ValueError:
            continue
        name = obj.get("tool") or obj.get("name")
        args = obj.get("args") or obj.get("arguments") or obj.get("parameters")
        if name in {"fetch_url", "read_file", "write_file"} and isinstance(args, dict):
            calls.append({"id": f"txt{len(calls)}", "name": name, "args": args, "native": False})
    return calls


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--task", required=True)
    ap.add_argument("--url", default="")
    ap.add_argument("--model", default=os.environ.get("MODEL", "nvidia/nemotron-3.5-lightning:free"))
    ap.add_argument("--out", default="summary.md")
    ap.add_argument("--selftest", action="store_true",
                    help="no LLM: just run fetch_url + write_file on --url (used for preflight/tests)")
    a = ap.parse_args()
    key = os.environ.get("OPENROUTER_API_KEY", "")
    os.makedirs(WORKSPACE, exist_ok=True)

    if a.selftest:
        emit("agent_start", task=a.task, url=a.url, model="selftest", workspace=WORKSPACE, key_in_env="n/a")
        emit("tool_call", step=1, tool="fetch_url", args={"url": a.url})
        try:
            result = run_tool("fetch_url", {"url": a.url})
        except PolicyBlocked as pb:
            emit("policy_denied", step=1, tool="fetch_url", host=pb.host, detail=pb.detail, url=a.url)
            emit("final", status="blocked", summary=f"Blocked by sandbox network policy: {pb.host}")
            return 3
        except Exception as e:  # noqa: BLE001
            emit("error", stage="selftest", message=f"{type(e).__name__}: {e}")
            return 1
        emit("tool_result", step=1, tool="fetch_url", ok=True, preview=result[:300], chars=len(result))
        run_tool("write_file", {"path": a.out, "content": result[:500]})
        emit("final", status="ok", summary="selftest fetched and wrote file", file=a.out)
        return 0

    emit("agent_start", task=a.task, url=a.url, model=a.model, workspace=WORKSPACE,
         key_in_env="placeholder" if key.startswith("openshell:") else ("present" if key else "missing"))
    if not key:
        emit("error", stage="setup", message="OPENROUTER_API_KEY placeholder missing — is the provider attached?")
        return 1

    system = (
        "You are a careful research agent running inside a locked-down sandbox. Tools: fetch_url, read_file, "
        "write_file. Work in this order: (1) call fetch_url on the given URL; (2) write a concise summary "
        f"(5-8 bullet points) to the file '{a.out}' using write_file; (3) reply with a one-paragraph final answer. "
        "Call one tool at a time. Do not invent facts that are not in the fetched text."
    )
    user = f"Task: {a.task}\nURL: {a.url}\nOutput file: {a.out}"
    messages = [{"role": "system", "content": system}, {"role": "user", "content": user}]
    fetched = wrote = False

    try:
        step = 0
        while not MAX_STEPS or step < MAX_STEPS:
            step += 1
            emit("llm_request", step=step, messages=len(messages))
            closing = fetched and wrote  # file is saved; the last model turn is only a courtesy wrap-up
            try:
                msg = chat(messages, a.model, key, timeout=25 if closing else 60, attempts=1 if closing else None, step=step)
            except PolicyBlocked:
                raise
            except Exception as e:  # noqa: BLE001
                if not closing:
                    raise
                emit("llm_response", step=step, tools=[], text=f"(wrap-up skipped: {type(e).__name__})")
                emit("final", status="ok", summary=f"Summary written to {a.out}.", file=a.out)
                return 0
            calls = tool_calls_from(msg)
            emit("llm_response", step=step, tools=[c["name"] for c in calls],
                 text=(msg.get("content") or "")[:300])
            if not calls:
                if a.url and not fetched:
                    messages.append({"role": "assistant", "content": msg.get("content") or ""})
                    messages.append({"role": "user", "content": f"Call the fetch_url tool now with url={a.url}."})
                    continue
                if not wrote:
                    messages.append({"role": "assistant", "content": msg.get("content") or ""})
                    messages.append({"role": "user", "content": f"Now call write_file to save your summary to {a.out}."})
                    continue
                emit("final", status="ok", summary=(msg.get("content") or "").strip(), file=a.out)
                return 0
            messages.append({"role": "assistant", "content": msg.get("content") or "",
                             **({"tool_calls": msg["tool_calls"]} if calls[0]["native"] else {})})
            for c in calls[:1]:  # one tool per turn keeps the timeline readable
                emit("tool_call", step=step, tool=c["name"], args={k: (v if k != "content" else f"<{len(v)} chars>") for k, v in c["args"].items()})
                try:
                    result = run_tool(c["name"], c["args"])
                    ok = True
                    fetched |= c["name"] == "fetch_url"
                    wrote |= c["name"] == "write_file"
                except PolicyBlocked as pb:
                    emit("policy_denied", step=step, tool=c["name"], host=pb.host, detail=pb.detail, url=c["args"].get("url", ""))
                    emit("final", status="blocked", summary=f"Blocked by sandbox network policy: {pb.host}")
                    return 3
                except PermissionError as e:
                    emit("fs_denied", step=step, tool=c["name"], path=str(e.filename or c["args"].get("path", "")), detail=str(e))
                    result, ok = f"error: permission denied: {e}", False
                except Exception as e:  # noqa: BLE001 - surface any tool error to the model
                    result, ok = f"error: {type(e).__name__}: {e}", False
                emit("tool_result", step=step, tool=c["name"], ok=ok, preview=result[:300], chars=len(result))
                if c["native"]:
                    messages.append({"role": "tool", "tool_call_id": c["id"], "content": result})
                else:
                    messages.append({"role": "user", "content": f"Tool {c['name']} result:\n{result}"})
        emit("final", status="incomplete", summary=f"Stopped after {MAX_STEPS} steps (AGENT_MAX_STEPS).")
        return 1
    except PolicyBlocked as pb:
        emit("policy_denied", step=0, tool="llm", host=pb.host, detail=pb.detail, url=LLM_URL)
        emit("final", status="blocked", summary=f"Blocked by sandbox network policy: {pb.host}")
        return 3
    except Exception as e:  # noqa: BLE001
        emit("error", stage="run", message=f"{type(e).__name__}: {e}")
        return 1


if __name__ == "__main__":
    sys.exit(main())
