"""Parser/policy tests built from output captured from a real OpenShell v0.1.7 gateway."""
from pathlib import Path

import pytest
import yaml

from app import parsing as p

FIX = Path(__file__).parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text()


def test_parse_log_line_structure():
    line = "[1794244291.887] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/local/bin/python3.12(0) -> en.wikipedia.org:443 [reason:transparent_tcp_policy_denied]"
    rec = p.parse_log_line(line)
    assert rec["source"] == "sandbox"
    assert rec["level"] == "OCSF"
    assert rec["msg"].startswith("NET:OPEN")


def test_denial_extracted_from_real_log():
    denials = p.parse_denials(read("logs_sandbox_denied_and_allowed.txt"))
    assert len(denials) == 1  # NET:REFUSE + NET:OPEN + warn line dedupe to one denial
    d = denials[0]
    assert d.host == "en.wikipedia.org"
    assert d.port == 443
    assert d.binary == "/usr/local/bin/python3.12"
    assert d.reason == "transparent_tcp_policy_denied"
    assert d.scope == "network"


def test_allowed_lines_are_not_denials():
    allowed = "[1794244291.577] [sandbox] [OCSF ] [ocsf] NET:OPEN [INFO] ALLOWED /usr/local/bin/python3.12(0) -> example.com:443 [policy:probe_httpbin engine:opa]"
    assert p.parse_denial_line(allowed) is None


def test_garbage_lines_ignored():
    assert p.parse_denial_line("not a log line") is None
    assert p.parse_denials(["", "x", "[abc] nope"]) == []


def test_dedupe_repeated_denials():
    line = "[1.0] [sandbox] [OCSF ] [ocsf] NET:OPEN [MED] DENIED /usr/bin/curl(5) -> a.example.org:443 [reason:x]"
    assert len(p.parse_denials([line, line, line])) == 1


def test_build_allow_rule_is_minimal():
    name, rule = p.build_allow_rule("en.wikipedia.org", 443, "/usr/local/bin/python3.12")
    assert name == "allow_en_wikipedia_org"
    assert rule["endpoints"] == [
        {"host": "en.wikipedia.org", "port": 443, "protocol": "rest", "access": "read-only", "enforcement": "enforce"}
    ]
    assert rule["binaries"] == [{"path": "/usr/local/bin/python3.12"}]


def test_build_allow_rule_requires_host_and_binary():
    with pytest.raises(ValueError):
        p.build_allow_rule("", 443, "/x")
    with pytest.raises(ValueError):
        p.build_allow_rule("a.com", 443, "")


def test_merge_rule_preserves_other_sections_and_rules():
    policy, _ = p.parse_policy_get_json(read("policy_get_base.json"))
    name, rule = p.build_allow_rule("en.wikipedia.org", 443, "/usr/local/bin/python3.12")
    merged = p.merge_rule(policy, name, rule)
    assert merged["filesystem_policy"] == policy["filesystem_policy"]
    assert merged["process"] == policy["process"]
    assert "probe_httpbin" in merged["network_policies"]
    assert name in merged["network_policies"]
    # original untouched
    assert name not in policy["network_policies"]


def test_merge_rule_unions_binaries_idempotently():
    policy = {"version": 1, "network_policies": {}}
    name, r1 = p.build_allow_rule("a.org", 443, "/usr/bin/python3.12")
    _, r2 = p.build_allow_rule("a.org", 443, "/usr/bin/curl")
    once = p.merge_rule(policy, name, r1)
    twice = p.merge_rule(once, name, r2)
    again = p.merge_rule(twice, name, r2)
    assert [b["path"] for b in again["network_policies"][name]["binaries"]] == ["/usr/bin/python3.12", "/usr/bin/curl"]
    assert len(again["network_policies"][name]["endpoints"]) == 1


def test_replace_network_only_touches_network():
    policy, _ = p.parse_policy_get_json(read("policy_get_base.json"))
    new = p.replace_network(policy, {})
    assert new["network_policies"] == {}
    assert new["filesystem_policy"] == policy["filesystem_policy"]


def test_generated_policy_yaml_roundtrips_and_lints_clean():
    policy, meta = p.parse_policy_get_json(read("policy_get_base.json"))
    assert meta["active_version"] == 2
    name, rule = p.build_allow_rule("docs.python.org", 443, "/usr/local/bin/python3.12")
    text = p.dump_yaml(p.merge_rule(policy, name, rule))
    doc = yaml.safe_load(text)
    assert p.lint_policy(doc) == []


def test_lint_flags_problems():
    probs = p.lint_policy({"version": 1, "bogus": 1, "network_policies": {"_provider_x": {"endpoints": [{"host": "a"}], "binaries": ["/x"]}}})
    joined = " | ".join(probs)
    assert "unknown top-level field: bogus" in joined
    assert "_provider_" in joined
    assert "missing port" in joined
    assert "objects with `path`" in joined


def test_parse_agent_line():
    assert p.parse_agent_line('{"type":"step","n":1}') == {"type": "step", "n": 1}
    assert p.parse_agent_line("plain text") is None
    assert p.parse_agent_line("{broken") is None
    assert p.parse_agent_line('{"no_type":1}') is None


def test_scrubber_masks_secret():
    s = p.SecretScrubber(["sk-or-v1-abcdefghijklmnop"])
    assert s.scrub("Authorization: Bearer sk-or-v1-abcdefghijklmnop") == "Authorization: Bearer ***"
    assert p.SecretScrubber([""]).scrub("keep") == "keep"
