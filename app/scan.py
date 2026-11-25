"""Tiny scheduled site-health scan for MerQato Workstation.

One scanner, one source of truth: the hub's existing HTTPS probe of every
public host. The scan result is stored in data/scans.json and shown in the
Workstation dashboard and Agents view. No model is required for the facts —
the agent reports what the probe found, in plain language.

Used two ways:
  * imported by app/server.py for /api/scans and POST /api/agent/scan
  * run directly by cron: python3 /opt/homepage/app/scan.py
"""

import json
import os
import sys
import time
import urllib.request

HUB_URL = os.environ.get("HUB_URL", "http://host.docker.internal:3010")
MAX_SCANS = 200


def data_dir():
    return os.environ.get("WS_DATA_DIR", "/data")


def scans_path():
    return os.path.join(data_dir(), "scans.json")


def _get_json(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "merqato-scan/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def reason(code):
    return {
        0: "no response",
        502: "502 Bad Gateway",
        503: "503 Service Unavailable",
        504: "504 Gateway Timeout",
        526: "526 Invalid SSL certificate",
    }.get(code, "HTTP %s" % code)


def build_report(data):
    probes = data.get("probes") or []
    failing = [p for p in probes if not p.get("ok")]
    slow = [p for p in probes if p.get("ok") and p.get("slow")]
    total = len(probes)
    ok = total - len(failing)
    all_clear = total > 0 and not failing

    if not total:
        summary = "No hosts were probed — check the hub API."
    elif all_clear:
        summary = "ALL CLEAR — %d/%d sites responding." % (ok, total)
        if slow:
            summary += " Slow: %s." % ", ".join(p["host"] for p in slow)
    else:
        detail = ", ".join("%s (%s)" % (p["host"], reason(p.get("code", 0))) for p in failing)
        summary = "%d FAILING of %d — %s." % (len(failing), total, detail)

    return {
        "at": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "total": total,
        "ok": ok,
        "failing": len(failing),
        "slow": len(slow),
        "all_clear": all_clear,
        "summary": summary,
        "failing_hosts": [
            {"host": p["host"], "code": p.get("code", 0), "reason": reason(p.get("code", 0)),
             "ms": p.get("ms", 0)}
            for p in failing
        ],
        "slow_hosts": [p["host"] for p in slow],
        "certs": data.get("certificates") or {},
        "source": "hub HTTPS probe",
    }


def load_reports():
    path = scans_path()
    if not os.path.exists(path):
        return []
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        return data.get("scans", []) if isinstance(data, dict) else data
    except Exception:
        return []


def save_report(report):
    reports = [report] + [r for r in load_reports() if r.get("at") != report.get("at")]
    reports = reports[:MAX_SCANS]
    path = scans_path()
    os.makedirs(data_dir(), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"scans": reports}, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)
    return report


def run_and_save():
    """Perform one scan against the hub and persist the result."""
    data = _get_json(HUB_URL.rstrip("/") + "/domains.json")
    return save_report(build_report(data))


if __name__ == "__main__":
    try:
        report = run_and_save()
        print(report["at"] + " " + report["summary"], flush=True)
    except Exception as exc:
        print("scan failed: %s" % exc, file=sys.stderr, flush=True)
        sys.exit(1)
