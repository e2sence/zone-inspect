#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""Zone Inspect gate for a line system that can only run a command.

The line system scans a board's serial number at the end of an operation and
runs this script with it. The script asks Zone Inspect for the verdict of the
latest visual inspection of that board, prints it (the line system keeps the
output as the operation log) and reports the outcome twice: as the process
exit code and as the LAST line of the output:

    EXIT WITH ERRORLEVEL==0   the board was inspected, every zone is OK
    EXIT WITH ERRORLEVEL==1   defect, undecided warning or unfinished inspection
    EXIT WITH ERRORLEVEL==2   no inspection found for this serial number
    EXIT WITH ERRORLEVEL==3   could not get an answer (network, key, settings)

Usage:   python.exe zi_check.py <serial> [path\\to\\zi_check.ini]
Needs:   Python 3.7 or newer, nothing else to install.
Settings are read from zi_check.ini next to this file (see zi_check.ini.example).
"""
from __future__ import print_function

import configparser
import json
import os
import socket
import ssl
import sys
import urllib.error
import urllib.parse
import urllib.request

EXIT_PASS = 0
EXIT_FAIL = 1
EXIT_NOT_INSPECTED = 2
EXIT_ERROR = 3

REASON_TEXT = {
    "ok": "all zones OK",
    "defect": "defect found",
    "warn": "warning not decided by the operator",
    "incomplete": "inspection not finished (unchecked zones)",
}


def say(text=""):
    print(text)
    sys.stdout.flush()


class CheckError(Exception):
    """Anything that prevents a trustworthy answer."""


def load_settings(path):
    if not os.path.isfile(path):
        raise CheckError("settings file not found: %s" % path)
    parser = configparser.ConfigParser(interpolation=None)
    try:
        parser.read(path, encoding="utf-8-sig")
        cfg = parser["zone-inspect"]
    except (configparser.Error, KeyError, UnicodeDecodeError) as exc:
        raise CheckError("cannot read [zone-inspect] from %s (%s)" % (path, exc))

    url = cfg.get("url", "").strip().rstrip("/")
    key = cfg.get("key", "").strip()
    if not url.lower().startswith(("https://", "http://")):
        raise CheckError("setting 'url' must start with https://")
    if not key:
        raise CheckError("setting 'key' is empty")
    try:
        timeout = float(cfg.get("timeout", "8"))
    except ValueError:
        raise CheckError("setting 'timeout' must be a number of seconds")
    ca_file = cfg.get("ca_file", "").strip()
    if ca_file and not os.path.isabs(ca_file):
        ca_file = os.path.join(os.path.dirname(path), ca_file)
    if ca_file and not os.path.isfile(ca_file):
        raise CheckError("ca_file not found: %s" % ca_file)
    return {
        "url": url,
        "key": key,
        "timeout": timeout,
        "ca_file": ca_file,
        "template": cfg.get("template", "").strip(),
        "max_age_hours": cfg.get("max_age_hours", "").strip(),
    }


def ask_server(settings, serial):
    query = {"serial": serial}
    if settings["template"]:
        query["template"] = settings["template"]
    if settings["max_age_hours"]:
        query["max_age_hours"] = settings["max_age_hours"]
    url = settings["url"] + "/api/v1/verdict?" + urllib.parse.urlencode(query)
    request = urllib.request.Request(url, headers={
        "X-Auth-Key": settings["key"],
        "Accept": "application/json",
        "User-Agent": "zi_check/1",
    })
    context = None
    if url.lower().startswith("https://"):
        context = ssl.create_default_context(cafile=settings["ca_file"] or None)

    try:
        with urllib.request.urlopen(request, timeout=settings["timeout"],
                                    context=context) as response:
            body = response.read()
    except urllib.error.HTTPError as exc:
        hint = {
            401: "the key is not accepted",
            403: "the key is not allowed to read verdicts",
            404: "the server does not know /api/v1/verdict (old version?)",
            503: "the server database is unavailable",
        }.get(exc.code, "unexpected answer")
        raise CheckError("server answered HTTP %d: %s" % (exc.code, hint))
    except urllib.error.URLError as exc:
        reason = exc.reason
        if isinstance(reason, ssl.SSLError):
            raise CheckError(
                "secure connection failed (%s). If this computer has outdated "
                "root certificates, set ca_file in zi_check.ini" % reason)
        if isinstance(reason, socket.timeout):
            raise CheckError("no answer within %g s" % settings["timeout"])
        raise CheckError("cannot reach %s (%s)" % (settings["url"], reason))
    except ssl.SSLError as exc:
        raise CheckError("secure connection failed (%s)" % exc)
    except socket.timeout:
        raise CheckError("no answer within %g s" % settings["timeout"])
    except (OSError, ValueError) as exc:
        raise CheckError("request failed (%s)" % exc)

    try:
        data = json.loads(body.decode("utf-8"))
    except ValueError:
        raise CheckError("server answer is not JSON (proxy or login page?)")
    if not isinstance(data, dict) or data.get("verdict") not in (
            "pass", "fail", "not_inspected"):
        raise CheckError("server answer has no verdict")
    return data


def report(data):
    """Print the answer; return the exit code."""
    verdict = data["verdict"]
    if verdict == "not_inspected":
        say("VERDICT=NOT_INSPECTED")
        say("No saved Zone Inspect inspection for this serial number.")
        return EXIT_NOT_INSPECTED

    reason = data.get("reason", "")
    # A pass must be backed by the reason too; anything else is a fail.
    passed = verdict == "pass" and reason == "ok"
    say("VERDICT=%s" % ("PASS" if passed else "FAIL"))
    say("reason=%s" % REASON_TEXT.get(reason, reason or "unknown"))
    version = data.get("recipe_version")
    say("recipe=%s%s" % (data.get("template", ""),
                         " v%s" % version if version else ""))
    say("operator=%s" % data.get("operator", ""))
    say("inspected=%s (UTC+8)" % str(data.get("timestamp", ""))[:19])
    say("zones=%s/%s checked" % (data.get("zones_checked", "?"),
                                 data.get("zones_total", "?")))
    for zone in data.get("problem_zones") or []:
        say("zone %s = %s" % (zone.get("label", "?"),
                              str(zone.get("status", "?")).upper()))
    if data.get("inspections", 1) > 1:
        say("inspections of this serial=%s (the latest one counts)"
            % data["inspections"])
    say("result=%s" % data.get("url", ""))
    return EXIT_PASS if passed else EXIT_FAIL


def run(argv):
    if len(argv) < 2 or len(argv) > 3:
        raise CheckError("usage: zi_check.py <serial> [path\\to\\zi_check.ini]")
    serial = argv[1].strip()
    say("ZONE-INSPECT CHECK SN=%s" % serial)
    if not serial or serial == "{0}":
        raise CheckError("no serial number was passed")
    here = os.path.dirname(os.path.abspath(__file__))
    settings = load_settings(argv[2] if len(argv) == 3
                             else os.path.join(here, "zi_check.ini"))
    return report(ask_server(settings, serial))


def main():
    # Old consoles (cp866 / cp1251) must not crash on a non-ASCII zone name.
    try:
        sys.stdout.reconfigure(errors="replace")
    except Exception:
        pass
    try:
        code = run(sys.argv)
    except CheckError as exc:
        say("VERDICT=ERROR")
        say("error=%s" % exc)
        code = EXIT_ERROR
    except BaseException as exc:      # never let a crash look like a pass
        say("VERDICT=ERROR")
        say("error=unexpected %s: %s" % (type(exc).__name__, exc))
        code = EXIT_ERROR
    say("EXIT WITH ERRORLEVEL==%d" % code)
    return code


if __name__ == "__main__":
    sys.exit(main())
