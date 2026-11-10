"""Pure parsing / policy helpers. No subprocess, no I/O — everything here is unit-tested
against output captured from a real OpenShell v0.1.7 gateway (tests/fixtures)."""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from typing import Any

import yaml

# ---------------------------------------------------------------------------
# Log parsing
# ---------------------------------------------------------------------------
# Real line shape (openshell logs --source sandbox):
#   [1794244291.887] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> en.wikipedia.org:443 [reason:transparent_tcp_policy_denied]
LOG_LINE = re.compile(
    r"^\[(?P<ts>\d+(?:\.\d+)?)\]\s+\[(?P<source>[^\]]+?)\s*\]\s+\[(?P<level>[^\]]+?)\s*\]\s+"
    r"\[(?P<target>[^\]]*)\]\s+(?P<msg>.*)$"
)
NET_DENIED = re.compile(
    r"^NET:OPEN\s+\[(?P<sev>[A-Z]+)\]\s+DENIED\s+(?P<binary>\S+?)\((?P<pid>\d+)\)\s+->\s+"
    r"(?P<host>[^\s:]+):(?P<port>\d+)(?:\s+\[reason:(?P<reason>[^\]]+)\])?"
)
# L7 denials: "HTTP:<METHOD> [SEV] DENIED <METHOD> <url> [policy:...]". Shape taken from the docs only:
# the captured fixtures contain L4 denials, so test_http_denied_* checks the documented shape, not a capture.
HTTP_DENIED = re.compile(
    r"^HTTP:(?P<method>[A-Z_]+)\s+\[(?P<sev>[A-Z]+)\]\s+DENIED\s+(?P<m2>[A-Z_]+)\s+"
    r"(?P<url>\S+)"
)


@dataclass(frozen=True)
class Denial:
    host: str
    port: int
    binary: str
    scope: str = "network"  # "network" (L4/CONNECT) or "http" (L7)
    method: str | None = None
    path: str | None = None
    reason: str | None = None
    ts: float = 0.0

    @property
    def key(self) -> tuple:
        # Dedup key: same binary hitting same host:port(+method/path) is one denial.
        return (self.host, self.port, self.binary, self.method, self.path)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)


def parse_log_line(line: str) -> dict[str, Any] | None:
    m = LOG_LINE.match(line.strip())
    if not m:
        return None
    d = m.groupdict()
    d["ts"] = float(d["ts"])
    return d


def parse_denial_line(line: str) -> Denial | None:
    rec = parse_log_line(line)
    if not rec:
        return None
    msg = rec["msg"]
    m = NET_DENIED.match(msg)
    if m:
        return Denial(
            host=m["host"].lower(),
            port=int(m["port"]),
            binary=m["binary"],
            scope="network",
            reason=m["reason"],
            ts=rec["ts"],
        )
    m = HTTP_DENIED.match(msg)
    if m:
        from urllib.parse import urlsplit

        u = urlsplit(m["url"])
        if u.hostname:
            return Denial(
                host=u.hostname.lower(),
                port=u.port or (443 if u.scheme == "https" else 80),
                binary="",  # the L7 line carries no binary; allow_host falls back to the agent interpreter
                scope="http",
                method=m["method"],
                path=u.path or "/",
                ts=rec["ts"],
            )
    return None


def parse_denials(lines: list[str] | str) -> list[Denial]:
    """Return de-duplicated denials in log order."""
    if isinstance(lines, str):
        lines = lines.splitlines()
    seen: set[tuple] = set()
    out: list[Denial] = []
    for line in lines:
        d = parse_denial_line(line)
        if d and d.key not in seen:
            seen.add(d.key)
            out.append(d)
    return out


# ---------------------------------------------------------------------------
# Policy helpers
# ---------------------------------------------------------------------------
def slug(host: str) -> str:
    return re.sub(r"[^a-z0-9]+", "_", host.lower()).strip("_")


def build_allow_rule(host: str, port: int, binary: str, *, access: str = "read-only") -> tuple[str, dict]:
    """Minimal network rule: ONE endpoint, ONE binary, L7-inspected & enforced.

    Shape follows the v0.1.7 schema: binaries are objects with `path`; endpoint has
    host/port/protocol/access/enforcement."""
    if not host or not binary:
        raise ValueError("host and binary are required to build a rule")
    name = f"allow_{slug(host)}" + ("" if port == 443 else f"_{port}")
    rule = {
        "name": name,
        "endpoints": [
            {
                "host": host,
                "port": int(port),
                "protocol": "rest",
                "access": access,
                "enforcement": "enforce",
            }
        ],
        "binaries": [{"path": binary}],
    }
    return name, rule


def merge_rule(policy: dict, name: str, rule: dict) -> dict:
    """Return a copy of `policy` with rule merged in (binaries unioned if the key exists)."""
    new = json.loads(json.dumps(policy))
    nets = new.setdefault("network_policies", {}) or {}
    new["network_policies"] = nets
    if name in nets:
        existing = nets[name]
        have = {b["path"] for b in existing.get("binaries", [])}
        for b in rule["binaries"]:
            if b["path"] not in have:
                existing.setdefault("binaries", []).append(b)
        have_eps = {(e["host"], e.get("port")) for e in existing.get("endpoints", [])}
        for e in rule["endpoints"]:
            if (e["host"], e.get("port")) not in have_eps:
                existing.setdefault("endpoints", []).append(e)
    else:
        nets[name] = rule
    return new


def replace_network(policy: dict, network_policies: dict | None) -> dict:
    """Swap ONLY network_policies (the only live-reloadable section that presets may change)."""
    new = json.loads(json.dumps(policy))
    new["network_policies"] = json.loads(json.dumps(network_policies or {}))
    return new


def parse_policy_get_json(text: str) -> tuple[dict, dict]:
    """`openshell policy get <sb> --base -o json` -> (policy_dict, meta)."""
    doc = json.loads(text)
    policy = doc.get("policy") or {}
    meta = {k: doc.get(k) for k in ("version", "active_version", "hash", "status", "sandbox")}
    return policy, meta


def dump_yaml(policy: dict) -> str:
    return yaml.safe_dump(policy, sort_keys=False, default_flow_style=False)


def load_policy_file(path: str) -> dict:
    with open(path, encoding="utf-8") as f:
        doc = yaml.safe_load(f) or {}
    if not isinstance(doc, dict) or doc.get("version") != 1:
        raise ValueError(f"{path}: policy must be a mapping with `version: 1`")
    return doc


# Top-level keys the v0.1.7 schema accepts (unknown keys are rejected by the gateway).
ALLOWED_TOP_LEVEL = {"version", "filesystem_policy", "landlock", "process", "network_policies", "network_middlewares"}


def lint_policy(doc: dict) -> list[str]:
    """Cheap local lint (the authoritative validation is the gateway's `policy set`)."""
    problems: list[str] = []
    for k in doc:
        if k not in ALLOWED_TOP_LEVEL:
            problems.append(f"unknown top-level field: {k}")
    if doc.get("version") != 1:
        problems.append("version must be 1")
    nets = doc.get("network_policies") or {}
    if not isinstance(nets, dict):
        return problems + ["network_policies must be a mapping of rule name -> rule"]
    for name, rule in nets.items():
        if str(name).startswith("_provider_"):
            problems.append(f"rule key {name!r} may not start with _provider_")
        if not isinstance(rule, dict):
            problems.append(f"{name}: rule must be a mapping")
            continue
        binaries, endpoints = rule.get("binaries") or [], rule.get("endpoints") or []
        if not isinstance(binaries, list) or not isinstance(endpoints, list):
            problems.append(f"{name}: binaries and endpoints must be lists")
            continue
        for b in binaries:
            if not isinstance(b, dict) or "path" not in b:
                problems.append(f"{name}: binaries must be objects with `path`")
        for e in endpoints:
            if not isinstance(e, dict):
                problems.append(f"{name}: endpoints must be objects")
                continue
            if "host" not in e:
                problems.append(f"{name}: endpoint missing host")
            if "port" not in e and "ports" not in e:
                problems.append(f"{name}: endpoint missing port")
            if "protocol" not in e:
                problems.append(f"{name}: endpoint {e.get('host')} has no protocol (access/rules ignored)")
    return problems


# ---------------------------------------------------------------------------
# Agent stdout (JSON lines)
# ---------------------------------------------------------------------------
def parse_agent_line(line: str) -> dict[str, Any] | None:
    line = line.strip()
    if not line.startswith("{"):
        return None
    try:
        obj = json.loads(line)
    except json.JSONDecodeError:
        return None
    return obj if isinstance(obj, dict) and "type" in obj else None


@dataclass
class SecretScrubber:
    """Replace known secret values in any text that might be logged or sent to the UI."""

    secrets: list[str] = field(default_factory=list)

    def scrub(self, text: str) -> str:
        for s in self.secrets:
            if s and len(s) >= 8:
                text = text.replace(s, "***")
        return text
