# Sandboxed Agent Console on NVIDIA OpenShell — implementation plan

> **Historical.** This is the plan written before the build. Several details changed along the way (workspace path,
> no custom image or Dockerfile, module names, provider name). `README.md` describes the project as built.

## Context
A runnable web app (FastAPI + one static HTML page). A task runs an agent inside an OpenShell sandbox. The user watches a live timeline, sees policy denials, clicks "Allow this host" to hot-apply a network rule, retries, and sees the result. Demo is tomorrow evening, so reliability (preflight, reset, REPLAY fallback, tests) matters more than features.

## Research findings (docs and repo read; CLI not yet run)
Latest stable is **v0.1.7** (2026-11-03). Releases land weekly on Tuesdays, so pin `OPENSHELL_VERSION=v0.1.7` and don't upgrade before the demo.

### Blockers and spec conflicts
1. **Docker is not installed** on this Mac, and neither is openshell. OpenShell needs Docker Desktop 28.0 or later on Apple Silicon. **The user must install and start Docker Desktop.** Landlock (ABI 3, kernel 6.2+) runs inside Docker's Linux VM. Check with `docker run --rm alpine uname -r`.
2. **`inference.local` and `openshell inference` were removed in 0.1.0.** Replacement: attach a provider and call OpenRouter's native endpoint. The sandbox env holds an opaque placeholder, and the proxy injects the real key only for the profile's endpoint. The agent calls `https://openrouter.ai/api/v1/chat/completions` with `Authorization: Bearer $OPENROUTER_API_KEY`, which is the placeholder. A custom profile `providers/openrouter-agent.yaml` allows the sandbox's real python path. The key never enters the sandbox. Confirm with the installed CLI and note the deviation in the README.
3. **`policy set` replaces the whole policy**, and filesystem, Landlock and process sections are startup-only. So "Apply preset" and "Allow this host" take `policy get --base`, change only `network_policies`, then run `policy set --policy <file> --wait`. Strict and Research share identical non-network blocks.
4. **`--upload` can't combine with a trailing command**, and the default image has no Python. So build a local image `osc-agent:local` (`python:3.12-slim`, `agent.py` baked in at `/app`, non-root) with `docker build`, and pass it with `--from`.
5. **Rules match the binary's real path** (`readlink -f`, e.g. `/usr/local/bin/python3.12`). Determine it empirically. "Allow this host" copies the binary named in the denial log.

### Transport (decided with user): FastAPI + WebSocket
Browser to backend is **one WebSocket** (`/ws`) carrying JSON messages in both directions. Client to server: `run_task`, `retry`, `apply_preset`, `apply_policy`, `allow_host`, `delete_sandbox`. Server to client: `event` (timeline items), `policy`, `result`, `error`, `ack` (with correlation ids). Plain HTTP only for `GET /` (static page), `GET /api/health` and `/docs`. Pydantic models define each message, each with a `type` field. The client auto-reconnects and the server replays the task's buffered events after a reconnect.

### OpenShell integration
A Python SDK exists but is workspace-scoped gRPC, with no policy-set-with-wait or log-follow convenience. So wrap the **CLI** in one adapter, `app/openshell_client.py`, using `subprocess` with timeouts, `--output json` where available, and a streaming exec. Nothing else shells out.

## Architecture
```
Browser (static page) ◄── WebSocket /ws ──► FastAPI (app/main.py)
                                              ├─ TaskManager (in-memory, event buffer)
                                              ├─ openshell_client.py ─► openshell CLI ─ gRPC/mTLS ─► gateway (brew service :17670)
                                              └─ REPLAY=1 → replay.py (recordings/*.json)           │ Docker driver
                                                                           sandbox container: supervisor + python agent
                                                                           (Landlock fs, seccomp, proxy enforcing network policy,
                                                                            provider placeholder env → real key injected at proxy)
```
Sandbox lifecycle: one per console session, named `osc-demo-<id>`. Create with `sandbox create --from osc-agent:local --policy policies/strict.yaml --provider openrouter --detach -- sleep infinity`. Each task or retry is `sandbox exec -- python /app/agent.py ...`, with policy hot-applied between runs. Exact flags are confirmed from `--help`.

## Layout (all new)
`Makefile` (setup, preflight, run, test, reset, record, image), `README.md`, `.env.example`, `.gitignore`, `app/{main,openshell_client,parsing,tasks,replay}.py`, `app/static/index.html` (+js, css), `agent/{agent.py,Dockerfile}`, `policies/{strict,research}.yaml`, `providers/openrouter-agent.yaml`, `scripts/{preflight,reset,setup}.sh`, `recordings/success.json`, `tests/test_parsing.py` and `tests/fixtures/*.txt`. A venv via `python3 -m venv .venv` (no uv or pip on PATH) with `fastapi uvicorn[standard] pyyaml pytest`.

## Key details
- **Agent** (stdlib only): tools `fetch_url`, `read_file`, `write_file` (limited to `/workspace`). Emits JSONL on stdout: `agent_start`, `llm_request`, `step`, `tool_call`, `tool_result`, `policy_denied`, `final`, `error`. A policy-blocked fetch makes the agent stop with status `blocked`, for a deterministic demo. It also accepts a tool call returned as JSON text, since free models are flaky. The model is a `:free` OpenRouter model, picked from a live check.
- **Policies**: `strict.yaml` has `version: 1`, a filesystem policy (read-only `/usr /lib /etc /app /var/log`, read-write `/workspace /tmp`), landlock, a non-root process, and `network_policies: {}`. The attached provider supplies only the openrouter.ai rule. `research.yaml` adds a rule for a few public hosts (e.g. `en.wikipedia.org`, `docs.python.org`, `example.com`), for the python binary, as `protocol: rest`, `access: read-only`, `enforcement: enforce`. Both are validated against v0.1.7.
- **Denial detection**: poll `openshell logs <sb> --source sandbox` (or stream), then regex-parse OCSF shorthand lines like `NET:OPEN [MED] DENIED <bin>(pid) -> host:port` and `HTTP:<M> ... DENIED`. Output is a de-duplicated `{host, port, binary, method?, path?}`. Tests use **real captured output**.
- **Allow this host**: generate rule `allow_<host_slug>` with one endpoint `host:443` (rest, read-only, enforce) and only the denied binary. Merge it into the base policy, run `policy set --wait`, and show the apply time. Exit 1 or 124 gives a clear error.
- **Reliability**: timeouts on every CLI call and on the task. Errors show in the timeline. Lifespan shutdown deletes `osc-demo-*` sandboxes, and there's an idle TTL. `reset.sh` removes all demo sandboxes. `REPLAY=1` replays a recorded real run (denial, allow, success). `make record` saves a successful run.
- **Secrets**: the key is read only from host env `OPENROUTER_API_KEY` by `provider create --from-existing`. Never logged or committed. Verification greps the sandbox env, app logs and repo for it.

## Stages (short status after each)
0. Host check, and the user installs Docker Desktop. Install pinned v0.1.7, confirm the gateway and Docker driver, check the kernel, and capture `--help` for sandbox, policy, provider, profile and logs, noting docs-vs-CLI differences.
1. Official first-agent path: import the profile, create the provider, create a sandbox, make a model call from inside it, and see one allowed and one denied request. Capture fixtures and the python real path.
2. Adapter and parser, with pytest on the fixtures.
3. Agent image, run standalone under strict (denial) and research (success).
4. FastAPI WebSocket backend, orchestration, allow-host flow, cleanup, REPLAY.
5. UI: task box, timeline, policy panel (Strict and Research, Apply, Allow this host with timing), result panel, error banners.
6. `preflight.sh`, `reset.sh`, Makefile, a real recording, README, full end-to-end run.

## Verification
- `scripts/preflight.sh` shows PASS per check. `pytest -q` passes.
- `make run`, in the built-in browser: strict run shows a denial, Allow this host applies live with timing, Retry succeeds, and the result appears. The file is in `/workspace`.
- Negative checks: a write outside `/workspace` is blocked by Landlock. The real key is absent from the sandbox env, logs and repo.
- `REPLAY=1 make run` plays the recording. `make reset` leaves no demo sandboxes. App shutdown deletes its sandboxes.
- Final report: what passed or failed, workarounds, and exact start commands.

## Risks
Free-model flakiness (mitigated by the JSON-text tool fallback, a live-checked model and REPLAY). Python TLS must trust the supervisor CA. The `--detach` plus trailing command and `exec` streaming flags may differ in v0.1.7 (the adapter boundary absorbs this).
