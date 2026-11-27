"""The line-system gate script (integrations/windows/zi_check.py), run as a
real process against a stub server. It must never report a pass by accident."""

import json
import subprocess
import sys
import threading
from http.server import BaseHTTPRequestHandler, HTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import pytest

pytestmark = pytest.mark.unit

SCRIPT = Path(__file__).resolve().parent.parent / "integrations" / "windows" / "zi_check.py"

PASS = {"verdict": "pass", "reason": "ok", "serial": "SN1", "template": "Board-X",
        "recipe_version": 3, "operator": "op1", "zones_total": 2,
        "zones_checked": 2, "problem_zones": [], "inspections": 1,
        "timestamp": "2026-11-26T10:00:00.000000",
        "url": "https://zi.example/r/abc"}


@pytest.fixture()
def stub():
    """Stub server; set stub.answer = (status, body bytes) per test."""
    state = type("S", (), {"answer": (200, b"{}"), "seen": []})()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self):
            state.seen.append((urlparse(self.path), self.headers.get("X-Auth-Key")))
            status, body = state.answer
            self.send_response(status)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *args):
            pass

    server = HTTPServer(("127.0.0.1", 0), Handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    state.url = "http://127.0.0.1:%d" % server.server_port
    yield state
    server.shutdown()
    server.server_close()


def _ini(tmp_path, url, extra=""):
    path = tmp_path / "zi_check.ini"
    path.write_text("[zone-inspect]\nurl = %s\nkey = apk_k\ntimeout = 3\n%s"
                    % (url, extra), encoding="utf-8")
    return str(path)


def _run(*args):
    proc = subprocess.run([sys.executable, str(SCRIPT)] + list(args),
                          capture_output=True, timeout=30)
    lines = proc.stdout.decode("utf-8", "replace").splitlines()
    return proc.returncode, lines


def _answer(stub, **changes):
    stub.answer = (200, json.dumps(dict(PASS, **changes)).encode())


def test_pass(stub, tmp_path):
    _answer(stub)
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert code == 0
    assert lines[0] == "ZONE-INSPECT CHECK SN=SN1"
    assert "VERDICT=PASS" in lines
    assert "result=https://zi.example/r/abc" in lines
    assert lines[-1] == "EXIT WITH ERRORLEVEL==0"
    parsed, key = stub.seen[0]
    assert parsed.path == "/api/v1/verdict"
    assert parse_qs(parsed.query) == {"serial": ["SN1"]}
    assert key == "apk_k"


def test_defect(stub, tmp_path):
    _answer(stub, verdict="fail", reason="defect",
            problem_zones=[{"label": "IC U5", "status": "defect"}])
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert code == 1
    assert "VERDICT=FAIL" in lines and "zone IC U5 = DEFECT" in lines
    assert lines[-1] == "EXIT WITH ERRORLEVEL==1"


@pytest.mark.parametrize("reason", ["warn", "incomplete"])
def test_warn_and_incomplete_fail(stub, tmp_path, reason):
    _answer(stub, verdict="fail", reason=reason)
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert (code, lines[-1]) == (1, "EXIT WITH ERRORLEVEL==1")


def test_not_inspected(stub, tmp_path):
    stub.answer = (200, json.dumps({"verdict": "not_inspected"}).encode())
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert code == 2
    assert "VERDICT=NOT_INSPECTED" in lines
    assert lines[-1] == "EXIT WITH ERRORLEVEL==2"


def test_pass_without_ok_reason_is_not_a_pass(stub, tmp_path):
    _answer(stub, reason="defect")
    assert _run("SN1", _ini(tmp_path, stub.url))[0] == 1


@pytest.mark.parametrize("answer", [
    (401, b'{"error": "Unauthorized"}'),
    (500, b"oops"),
    (200, b"<html>login</html>"),
    (200, b'{"verdict": "maybe"}'),
    (200, b'["pass"]'),
])
def test_bad_answers_are_errors(stub, tmp_path, answer):
    stub.answer = answer
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert code == 3
    assert "VERDICT=ERROR" in lines
    assert lines[-1] == "EXIT WITH ERRORLEVEL==3"


def test_server_down(tmp_path):
    code, lines = _run("SN1", _ini(tmp_path, "http://127.0.0.1:9"))
    assert (code, lines[-1]) == (3, "EXIT WITH ERRORLEVEL==3")


@pytest.mark.parametrize("args", [[], [""], ["{0}"], ["A", "B", "C"]])
def test_missing_serial(args, tmp_path, stub):
    _answer(stub)
    if len(args) == 1:
        args = args + [_ini(tmp_path, stub.url)]
    code, lines = _run(*args)
    assert (code, lines[-1]) == (3, "EXIT WITH ERRORLEVEL==3")
    assert stub.seen == []


def test_missing_or_broken_settings(tmp_path):
    assert _run("SN1", str(tmp_path / "absent.ini"))[0] == 3
    broken = tmp_path / "zi_check.ini"
    broken.write_text("[zone-inspect]\nurl = https://x\nkey =\n", encoding="utf-8")
    code, lines = _run("SN1", str(broken))
    assert (code, lines[-1]) == (3, "EXIT WITH ERRORLEVEL==3")


def test_optional_filters_and_odd_serials(stub, tmp_path):
    _answer(stub)
    ini = _ini(tmp_path, stub.url, "template = Board X\nmax_age_hours = 24\n")
    assert _run("SN 1&x=#/", ini)[0] == 0
    query = parse_qs(stub.seen[0][0].query)
    assert query == {"serial": ["SN 1&x=#/"], "template": ["Board X"],
                     "max_age_hours": ["24"]}


def test_non_ascii_names_do_not_crash(stub, tmp_path):
    _answer(stub, verdict="fail", reason="defect", template="Плата",
            problem_zones=[{"label": "Разъём J2", "status": "defect"}])
    code, lines = _run("SN1", _ini(tmp_path, stub.url))
    assert (code, lines[-1]) == (1, "EXIT WITH ERRORLEVEL==1")
