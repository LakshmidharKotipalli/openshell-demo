"""Security regressions: cross-site WebSocket hijacking and non-http(s) URL fetching."""
import importlib.util
from pathlib import Path

import pytest
from starlette.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from app.main import app, ws_origin_allowed

spec = importlib.util.spec_from_file_location("agent_mod", Path(__file__).parent.parent / "agent" / "agent.py")
agent = importlib.util.module_from_spec(spec)
spec.loader.exec_module(agent)


# ---- WebSocket origin / host checks -------------------------------------------------------------
@pytest.mark.parametrize("headers,ok", [
    ({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}, True),
    ({"host": "localhost:8000", "origin": "http://localhost:8000"}, True),
    ({"host": "127.0.0.1:8000"}, True),                                      # non-browser client (e2e script)
    ({"host": "127.0.0.1:8000", "origin": "https://evil.example"}, False),   # cross-site page
    ({"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:9999"}, False),  # other local port/app
    ({"host": "127.0.0.1:8000", "origin": "null"}, False),                   # sandboxed iframe / file://
    ({"host": "evil.example:8000", "origin": "http://evil.example:8000"}, False),  # DNS rebinding
    ({"host": "evil.example", "origin": "http://127.0.0.1:8000"}, False),
    ({}, False),
])
def test_ws_origin_allowed(headers, ok):
    assert ws_origin_allowed(headers) is ok


def test_ws_handshake_rejects_foreign_origin():
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8000", "origin": "https://evil.example"}):
            pass


def test_ws_handshake_accepts_same_origin():
    c = TestClient(app, base_url="http://127.0.0.1:8000")
    # TestClient always sends "Host: testserver" on WebSocket handshakes, so set it explicitly like a browser would.
    with c.websocket_connect("/ws", headers={"host": "127.0.0.1:8000", "origin": "http://127.0.0.1:8000"}) as ws:
        assert ws.receive_json()["type"] == "state"


def test_ws_handshake_rejects_rebinding_host():
    c = TestClient(app)  # Host: testserver is not a loopback name
    with pytest.raises(WebSocketDisconnect):
        with c.websocket_connect("/ws"):
            pass


# ---- agent fetch_url scheme restriction ---------------------------------------------------------
@pytest.mark.parametrize("url", ["file:///etc/passwd", "ftp://example.com/x", "data:text/plain,hi", "gopher://x", "//example.com", "javascript:1", "/etc/passwd"])
def test_fetch_url_rejects_non_http_schemes(url):
    with pytest.raises(ValueError, match="http"):
        agent.run_tool("fetch_url", {"url": url})


def test_opener_has_no_file_or_ftp_handlers():
    names = {type(h).__name__ for h in agent._OPENER.handlers}
    assert "FileHandler" not in names and "FTPHandler" not in names and "DataHandler" not in names
    assert {"HTTPHandler", "HTTPSHandler"} <= names


def test_http_urls_pass_validation():
    agent.check_url("https://en.wikipedia.org/wiki/Sandbox_(computer_security)")
    agent.check_url("http://example.com/")
