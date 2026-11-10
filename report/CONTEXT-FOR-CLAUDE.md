# Context: "Sandboxed Agent Console" built on NVIDIA OpenShell

> Paste or attach this file at the start of a Claude chat, then ask questions. It contains no secrets.
> Written 2026-11-09. Everything here was built and tested on one Mac (Apple Silicon, Docker Desktop).

## 1. What this is
A small web app, built in one session, to demo **NVIDIA OpenShell** (open-source runtime that sandboxes autonomous AI agents with kernel-level isolation and declarative YAML policies). The user shows it live to a technical audience on 2026-11-10.

User flow: type a task → an agent runs **inside an OpenShell sandbox** → live timeline of events → when the sandbox policy blocks something, a **denial card** appears (host, port, binary) with an **"Allow this host"** button → the app builds a minimal rule, applies it to the *running* sandbox with `openshell policy set --wait`, shows how many seconds it took → **Retry** → agent fetches the page, calls an LLM, writes `summary.md` → result panel.

## 2. Stack and versions
- OpenShell **v0.1.7** (pinned in `.openshell-version`; do not upgrade — it releases weekly). Docker compute driver. Gateway runs as a Homebrew service at `https://localhost:17670` (mTLS).
- Backend: Python 3.12, FastAPI + uvicorn, **one WebSocket** `/ws` (plus `/api/health` and static files). In-memory state, no DB, no auth.
- Frontend: single static page (HTML/CSS/JS, no build).
- Agent: `agent/agent.py`, stdlib only, runs in a `python:3.12-slim` sandbox as uid 1000. Tools: `fetch_url`, `read_file`, `write_file`. Prints one JSON object per line on stdout.
- Model: OpenRouter, default `nvidia/nemotron-3.5-lightning:free` (env `MODEL`).
- ~2,400 lines of code/config; 26 pytest tests.

## 3. Architecture (data flow)
```
Browser ──WebSocket──> FastAPI (app/main.py) ── app/session.py (event log, orchestration, replay)
                                                   │ app/parsing.py  (pure: log→Denial, rule build/merge, lint)
                                                   │ app/openshell_client.py  ← ONLY code that runs the `openshell` CLI
                                                   ▼ subprocess (timeouts, secret scrubbing)
                                              openshell CLI ──gRPC+mTLS──> OpenShell gateway (control plane)
                                                                              │ Docker driver
                                                                              ▼
                                  sandbox container (data plane): supervisor (Landlock, seccomp, network proxy + OPA policy)
                                  + agent.py.  OPENROUTER_API_KEY inside = opaque placeholder; proxy swaps in the real key
                                  only for python3.12 → openrouter.ai.
```
Why a CLI adapter instead of the Python SDK: the SDK is workspace-scoped gRPC with no "policy set and wait" or "follow logs" helper; the CLI has everything.

## 4. Lifecycle of one task
1. `run_task` → `session.ensure_sandbox()` creates `osc-demo-<hex>` with `--from python:3.12-slim --policy policies/strict.yaml --provider openrouter-agent --label app=osc-demo --detach -- sleep infinity` (~1 s with image cached), uploads `agent/agent.py` to `/sandbox`.
2. Each run/retry = `openshell sandbox exec -n <sb> -- python3 agent.py --task … --url … --model …`; stdout JSON lines become timeline events (`agent.*`).
3. Under strict policy, the agent's `fetch_url` to Wikipedia is refused by the proxy → agent emits `policy_denied`, exits 3.
4. App polls `openshell logs <sb> --source sandbox`, parses e.g. `NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> en.wikipedia.org:443 [reason:transparent_tcp_policy_denied]` into `Denial(host, port, binary, …)` → `denial` event → red card.
5. `allow_host`: fetch base policy (`policy get --base -o json`), merge rule `allow_en_wikipedia_org` (one endpoint host:443, `protocol: rest`, `access: read-only`, `enforcement: enforce`; one binary `{path: /usr/local/bin/python3.12}`), write temp YAML, `policy set --wait`, measure time (3–10 s observed). Server refuses hosts not denied in the session.
6. `retry` → exec again → success → app `cat`s `/sandbox/workspace/summary.md` → `result` message.
Sandbox is deleted on "Delete sandbox", idle TTL (1800 s), app shutdown, `make reset`.

## 5. Policies (important mechanics)
- `policies/strict.yaml`: `filesystem_policy` (read-only /usr /lib /etc /var/log /proc /dev/urandom; read-write /sandbox /tmp /dev/null), `landlock: best_effort`, `process: uid/gid 1000`, `network_policies: {}`. The *provider* contributes the openrouter.ai rule.
- `policies/research.yaml`: same + rule `research_hosts` (python3.12 may GET en.wikipedia.org, docs.python.org, example.com).
- **Startup-only** sections: filesystem_policy, landlock, process. **Live** section: network_policies. So presets and "Allow" fetch the current base policy and swap/merge **only** `network_policies`, then `policy set`. Strict and Research share identical startup blocks.
- Verified: Research preset applies 3 hosts, Strict revokes them, custom YAML round-trips.

## 6. Credentials
`.env` (git-ignored, host only) holds `OPENROUTER_API_KEY`. `make setup` imports provider profile `providers/openrouter-agent.yaml` and runs `openshell provider create --name openrouter-agent --type openrouter-agent --from-existing` (key read from env). The sandbox only sees `openshell:resolve:env:…`; proxy substitutes the real key if binary = python3.12, host = openrouter.ai, header = Authorization. All CLI output is scrubbed for the key. A repo-wide scan found it nowhere except `.env`.

## 7. WebSocket protocol (app/protocol.py, pydantic)
Client→server: `hello{last_seq}`, `run_task{task,url}`, `retry`, `allow_host{host,port,binary}`, `apply_preset{strict|research}`, `apply_policy{yaml}`, `get_policy`, `list_sandboxes`, `delete_sandbox{name}`.
Server→client: `event{seq,kind,ts,…}` (buffered, resumable), `state`, `policy`, `result`, `reset`, `ack`, `error`.
Event kinds: sandbox_creating, sandbox_ready, run_started, agent.{agent_start,llm_request,llm_retry,llm_response,tool_call,tool_result,policy_denied,fs_denied,final,error}, denial, blocked, policy_applying, policy_applied, done, error, info.

## 8. Repo layout (project root: ~/Desktop/OpenShell-Demo)
```
app/{main,session,openshell_client,parsing,protocol}.py   app/static/{index.html,style.css,app.js}
agent/agent.py   policies/{strict,research}.yaml   providers/openrouter-agent.yaml
scripts/{install-openshell,enable-bridge-networking,setup,preflight,reset}.sh   scripts/e2e.py
recordings/success.json (real run, used by REPLAY=1)   tests/ (26 tests + fixtures captured from the real gateway)
docs/cli-help/ (openshell --help captured from v0.1.7)   report/ (visual HTML report + this file)
Makefile  README.md  .env.example  .openshell-version  requirements.txt
```

## 9. How to run
One-time: `cp .env.example .env` then **edit** `.env` to add the key (do NOT re-run `cp` — it overwrites), `make install`, `make setup`, `make preflight`.
Demo: `make preflight` → `make run` (http://127.0.0.1:8000). Fallback: `make replay` (REPLAY=1, plays a recorded real run, no sandbox/model needed). Others: `make test`, `make e2e`, `make record` (re-record replay), `make reset`.
Env: `MODEL`, `REPLAY=1`, `AGENT_MAX_STEPS` (default 0 = unlimited), `RUN_TIMEOUT` (900 s wall-clock net; 0 = none), `IDLE_TTL`, `AGENT_SELFTEST=1` (dev: agent skips the LLM).

## 10. Deviations from the original brief and gotchas (the non-obvious parts)
- **`inference.local` and `openshell inference` were removed in OpenShell 0.1.0.** The agent instead calls `https://openrouter.ai/api/v1/chat/completions` with the placeholder key; the provider profile authorises that route. Same security property, different URL.
- **Docker Desktop needs bridge networking**: default host mode made sandbox supervisors fail with "failed to connect to OpenShell server". `scripts/enable-bridge-networking.sh` copies the Homebrew `gateway.toml` to `~/.config/openshell/` and sets `supervisor_network_mode = "bridge"`. A user `gateway.toml` *replaces* the Homebrew one — a minimal hand-written file broke the gateway.
- A sandbox with **zero** network rules has no proxy: connections fail with EACCES and **no denial is logged**. Strict relies on the provider's openrouter.ai rule so a proxy exists.
- No `policy validate` command; local lint + gateway validation on `policy set`. `profile import` fails if the profile exists, so `setup.sh` deletes provider+profile and re-imports (`profile update` needs a resource_version).
- Rules match the binary's **real path** (`/usr/local/bin/python3.12`), not symlinks.
- File (Landlock) denials don't appear in the gateway log stream; the agent reports them as `agent.fs_denied`. Verified: writes to /etc, /usr fail.
- HTTP method/path (L7) denial log format is implemented from docs only; verified captures are L4 connection denials.
- Agent has **no cap on tries** by design: unlimited steps, transient LLM errors (network/429/5xx/bad JSON) retried until success; 401/400 fail fast; a policy denial stops it. The closing wrap-up model call (after the file is written) gets one 25 s attempt and falls back to the file content.
- Free models can be slow (one final reply took ~50 s); replay is the safety net.
- An environment hook in the session blocked reading outside the project folder, so gateway logs outside it were never read directly.

## 11. What was verified
Real-model end-to-end run (strict denial → Allow → retry → 1.2–1.4k-char summary) via `scripts/e2e.py` and by clicking through the UI; 26 tests; `make preflight` all PASS incl. a real sandbox smoke test; REPLAY mode; shutdown cleanup leaves no sandboxes; key placeholder verified inside the sandbox.

## 12. Reusing the pattern
Swap points: workload (`agent.py` / the `exec` argv in `session.run_agent()`), image (`AGENT_IMAGE`), provider profile (`providers/*.yaml`: host, binary path, credential env var), policy presets (`policies/*.yaml`, keep non-network blocks identical), granularity of "Allow" (`parsing.build_allow_rule()`), result extraction. Reusable core: adapter, parser, denial→rule loop, WebSocket protocol, replay. Alternatives: OpenShell SDKs (Python/TS/Go/Rust), OpenShell's native policy advisor (`openshell rule get/approve`), Kubernetes/microVM drivers.

## 13. Glossary
Gateway = control plane service. Sandbox = isolated container where the agent runs. Supervisor = OpenShell process outside the agent enforcing policy and running the proxy. Provider = stored credential + rules for using it; Profile = its schema. Landlock = Linux filesystem LSM (needs kernel ≥ 6.2; here Docker's VM kernel 7.0.14). seccomp = syscall filter. OPA = policy engine. OCSF = structured event format behind the log lines.

## 14. Suggested questions for the chat
"Explain section 4 step by step." · "Why can't presets replace the whole policy?" · "How would I adapt this to run a coding agent against my repo?" · "What could go wrong live tomorrow and how do I recover?" · "How does the placeholder-to-real-key swap work and what are its limits?"
