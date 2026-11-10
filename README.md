# Sandboxed Agent Console

A small web app on top of **NVIDIA OpenShell**. You submit a task, an AI agent runs **inside an OpenShell
sandbox**, and you watch it work live. When the sandbox policy blocks something, you see the denial
in real time and can click **Allow this host**. A minimal network rule is generated, applied to the
*running* sandbox, and you retry.

```
Task → sandbox created (strict policy) → agent fetches a URL → DENIED (host, port, binary)
     → [Allow this host] → rule applied live (`openshell policy set --wait`, shows seconds)
     → Retry → agent fetches page, calls the model, writes summary.md → result panel
```

## Versions (pinned)

| Component | Version |
|---|---|
| **OpenShell** (`OPENSHELL_VERSION`) | **v0.1.7** (stable, 2026-11-03), recorded in `.openshell-version` |
| Compute driver | Docker (Docker Desktop, Apple Silicon) |
| Sandbox image | `python:3.12-slim` (pulled by the gateway) |
| Python (host) | 3.12, FastAPI + uvicorn |
| Model | `nvidia/nemotron-3.5-lightning:free` on OpenRouter (override with `MODEL=`) |

OpenShell releases weekly (Tuesdays). **Do not upgrade before a demo**; the installer below pins the version.

## Prerequisites

- macOS on Apple Silicon with **Docker Desktop ≥ 28** running. Landlock needs Linux kernel ≥ 6.2; on a Mac that kernel is
  Docker's Linux VM (verified: `7.0.14-linuxkit`). Nothing else on the host kernel matters.
- Homebrew (the OpenShell installer uses it), Python 3.12, `make`.
- An [OpenRouter](https://openrouter.ai) API key (free models need no credits).

## Environment variables

| Variable | Where | Purpose |
|---|---|---|
| `OPENROUTER_API_KEY` | **host only**, in `.env` (git-ignored) or exported | Read once by `make setup` → stored in the OpenShell gateway as a *provider*. Never passed to the app, never in the sandbox, never logged. |
| `MODEL` | optional | OpenRouter model id for the agent. |
| `REPLAY=1` | optional | Replay a recorded run instead of using a live sandbox. |
| `PORT` | optional (`make run PORT=8080`) | Web port, default 8000. |
| `RUN_TIMEOUT`, `IDLE_TTL` | optional | Wall-clock safety net for one agent run (900s; `0` = none), idle sandbox TTL (1800s). |
| `AGENT_MAX_STEPS` | optional | Cap on agent steps, forwarded into the sandbox on each run. Default `0` = **no limit on tries**: the agent keeps going (and retries transient model errors) until it finishes. |
| `PROVIDER_NAME`, `AGENT_IMAGE`, `OPENSHELL_BIN` | optional | Provider name (`openrouter-agent`), sandbox image (`python:3.12-slim`), CLI path (`openshell`). The first two are also read by `make setup` / `make preflight`. Allow rules assume the interpreter is `/usr/local/bin/python3.12`. |
| `AGENT_SELFTEST=1` | dev/CI | Agent skips the LLM (fetch + file write only). |

## One-time setup

```bash
cp .env.example .env            # then edit .env and paste your key (never commit it)
make install                    # pinned OpenShell + gateway; switches it to Docker bridge networking
make setup                      # venv, provider profile, provider (stores the key in the gateway), warms the image
make preflight                  # PASS/FAIL checklist
```

`make install` is skipped if OpenShell v0.1.7 is already installed; `scripts/enable-bridge-networking.sh` is the
Docker Desktop fix described under *Notes on OpenShell 0.1.7* below.

## Run

```bash
make run                        # http://127.0.0.1:8000
```

Other targets: `make replay` (recorded run, no sandbox/model needed), `make test`, `make e2e`, `make record`,
`make reset` (delete demo sandboxes), `make preflight`.

### Demo script (≈3 minutes)

1. Open the page. Policy panel shows **Strict** (no network rules besides the model route). Click **Run**.
2. Timeline: sandbox created → agent starts → `fetch_url(wikipedia)` → **⛔ denial card** (host, port, binary).
3. Click **Allow this host** → "Policy live in N s". The policy YAML now contains `allow_en_wikipedia_org`.
4. Click **Retry task** → fetch succeeds → model summarises → file written → **Result** panel.
5. Optional: click **Strict** to revoke, or **Research** for a preset allowlist; Landlock demo: ask the agent to write to `/etc`.

If the network or model fails during the live session: `Ctrl-C`, `make replay`, reload the page.

## Architecture

```
┌────────────────────────── Browser (static HTML/CSS/JS) ──────────────────────────┐
│ task box · live timeline · denial card + "Allow this host" · policy YAML · result │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │  one WebSocket  /ws  (JSON, typed, resumable)
┌───────────────────────────────────────▼───────────────────────────────────────────┐
│ FastAPI  app/main.py  ──  app/session.py (event log, orchestration, REPLAY, cleanup)│
│   app/protocol.py (pydantic msgs)   app/parsing.py (log→denial, policy rule merge)  │
│   app/openshell_client.py  ← the ONLY code that calls the `openshell` CLI           │
└───────────────────────────────────────┬───────────────────────────────────────────┘
                                        │ subprocess (timeouts, secret scrubbing)
                              ┌─────────▼──────────┐   gRPC + mTLS  ┌───────────────────┐
                              │  openshell CLI     │ ─────────────► │ OpenShell gateway │ control plane
                              └────────────────────┘                │  (brew service)   │  policy, providers,
                                                                    └─────────┬─────────┘  credentials, logs
                                                                              │ Docker driver
                         ┌────────────────────────────────────────────────────▼─────────────┐
                         │ sandbox container (python:3.12-slim) — data plane                  │
                         │  supervisor: Landlock fs · seccomp · network proxy + OPA policy    │
                         │  agent/agent.py  (stdlib only) → tools fetch_url/read_file/write_file │
                         │  OPENROUTER_API_KEY = opaque placeholder  (real key stays outside)  │
                         └────────────────────────────────────────────────────────────────────┘
```

Why a CLI adapter and not the Python SDK: the SDK (`openshell` on PyPI) is workspace-scoped gRPC with no
policy-set-with-wait or log-follow helper; the CLI exposes everything this app needs and is what an operator
sees. All CLI use is in `app/openshell_client.py`.

### Sandbox lifecycle
One sandbox per console session (`osc-demo-<id>`, label `app=osc-demo`), created on first Run with
`--policy policies/strict.yaml --provider openrouter-agent`. `agent.py` is uploaded; each Run/Retry is a
`sandbox exec`. Policy changes are applied live between runs. Sandboxes are deleted on **Delete sandbox**, after
`IDLE_TTL`, on app shutdown, and by `make reset`.

## How OpenShell is used here

- **Gateway = control plane.** Runs as a Homebrew service (`https://localhost:17670`, mTLS). Owns sandbox lifecycle,
  policy revisions, providers/credentials and the sandbox log buffer. The app talks to it only through the CLI.
- **Sandbox = data plane.** Each task runs in a Docker container started by the gateway's Docker driver. A supervisor
  outside the workload boundary enforces policy; the agent process cannot change it.
- **Network policy.** Default-deny. Every outbound connection is matched on *(binary real path, host, port)*; with
  `protocol: rest` OpenShell terminates TLS and checks method/path too. Denials are logged as
  `NET:OPEN [MED] DENIED <binary>(pid) -> host:port`; the app parses those lines into structured events.
  `network_policies` reload **live**; "Allow this host" builds one endpoint + one binary, merges it into the sandbox's
  base policy (`policy get --base`), and applies it with `policy set --wait`.
- **Landlock (+ seccomp) filesystem limits.** `policies/*.yaml` allow read-write only on `/sandbox` (the workspace)
  and `/tmp`; system paths are read-only. Verified: writes to `/etc` and `/usr` fail with `PermissionError`.
  Filesystem/process settings are startup-only, which is why Strict and Research share those blocks.
- **Providers for credentials.** `make setup` stores the OpenRouter key in the gateway via
  `provider create --from-existing`. The sandbox receives only a placeholder
  (`openshell:resolve:env:…`); the proxy substitutes the real key only on requests from `python3.12` to `openrouter.ai`.
  The provider profile (`providers/openrouter-agent.yaml`) is what grants that route.
- **Model route (differs from the original brief).** `inference.local` and `openshell inference` were **removed in
  OpenShell 0.1.0**. The documented replacement, used here, is: attach a provider and call the provider's native
  endpoint (`https://openrouter.ai/api/v1/chat/completions`) with the placeholder key. Same security property
  (the key never enters the sandbox), different URL.

## Notes on OpenShell 0.1.7 (docs vs CLI vs reality)

- **Docker Desktop needs bridge networking.** Default `supervisor_network_mode = host` makes the supervisor unable to
  reach the gateway ("failed to connect to OpenShell server"). `scripts/enable-bridge-networking.sh` copies the
  Homebrew `gateway.toml` to `~/.config/openshell/` and sets `supervisor_network_mode = "bridge"`. (A user config
  *replaces* the Homebrew one, so a minimal hand-written file breaks the gateway.)
- Profile commands are `openshell profile …` (the example profile's header says `provider profile`). `profile import` fails if the
  profile exists (and `profile update` needs a resource_version), so setup deletes the provider and profile and re-imports.
- No `policy validate` command; policies are linted locally (`app/parsing.lint_policy`) and validated for real by the gateway
  on `policy set` (unknown fields are rejected).
- A sandbox with **zero** network rules has no proxy: connections fail with `EACCES` and **no denial is logged**.
  The strict preset relies on the provider's openrouter.ai rule so a proxy exists and denials are logged.
- `policy set --wait` took ~3–9 s in testing (the sandbox picks up changes on a poll interval); the UI shows the measured time.
- OpenShell can also draft allow rules itself (`openshell rule get <sb> --status pending`); this app uses explicit `policy set`.
- Agent-side file denials (Landlock `EACCES`) are not in the gateway log stream; the agent reports them (`agent.fs_denied`).

## Security notes

- **WebSocket origin check.** The console has no login, so any web page in your browser could otherwise open
  `ws://127.0.0.1:8000/ws` and drive it (run tasks, widen the sandbox policy). `app/main.py` rejects the handshake unless
  the `Host` header is a loopback name (blocks DNS rebinding) and any `Origin` header is this same server. Scripts such as
  `scripts/e2e.py` send no `Origin` and still work. The app binds to `127.0.0.1` only; do not expose it on a network
  without adding real authentication.
- **`fetch_url` is http/https only.** The agent's HTTP opener has no `file://`, `ftp://` or `data:` handlers, so a
  model-chosen URL (or redirect) cannot read local files around the workspace restriction. Landlock still bounds the
  sandbox either way.
- Both are covered by `tests/test_security.py`.

## Tests

`make test` runs pytest against output captured from a real v0.1.7 gateway (`tests/fixtures/`): log parsing (real denial
lines), policy rule generation/merge, adapter argv/timeouts/secret scrubbing (fake CLI), session behaviour (fake adapter),
the agent's retry and tool-call handling, and the security checks above. `make e2e` drives the full
denial → allow → retry → result flow over the app's WebSocket against live OpenShell.

## Repository layout

```
app/            FastAPI backend (main, session, protocol, parsing, openshell_client) + static UI
agent/agent.py  the in-sandbox agent (stdlib only, JSON-lines on stdout)
policies/       strict.yaml, research.yaml        providers/  openrouter-agent.yaml
scripts/        install-openshell, enable-bridge-networking, setup, preflight, reset, e2e
recordings/     success.json (replay)             tests/      pytest + captured fixtures
docs/cli-help/  `openshell … --help` captured from v0.1.7
report/         single-file visual report: how everything works, how to run it, how to reuse it (open report/index.html)
```
