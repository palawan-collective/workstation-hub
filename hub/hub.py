#!/usr/bin/env python3
"""MerQato Workstation hub API.

Read-only aggregation for the Homepage dashboard. No writes, no secrets
returned to clients, every value is measured (not mocked). Stdlib only.

Run: python3 hub.py  (listens on 0.0.0.0:3010)
"""

import json
import os
import re
import socket
import ssl
import threading
import time
import base64
import hmac
import urllib.error
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse


def env_int(name, default):
    """Read a positive integer environment value without failing on blanks."""
    raw = os.environ.get(name, "").strip()
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return default
    return value if value > 0 else default


PORT = env_int("HUB_PORT", 3010)
BUILD_ID = os.environ.get("HUB_BUILD", "2026.11.19")
CF_TOKEN = os.environ.get("CF_API_TOKEN", "").strip()
DOCKER = os.environ.get("DOCKER_API", "http://dockerproxy:2375")
_default_accounts = "palawan-collective"
GITHUB_ACCOUNTS = [
    account.strip()
    for account in (os.environ.get("GITHUB_ACCOUNTS", "").strip() or _default_accounts).split(",")
    if account.strip()
]
CACHE_TTL = env_int("CACHE_TTL", 60)
VPS_FILE = os.environ.get("VPS_FILE", "/app/vps.json")
COOLIFY_URL = os.environ.get("COOLIFY_URL", "https://coolify.merqato.digital").rstrip("/")
COOLIFY_TOKEN = os.environ.get("COOLIFY_TOKEN", "").strip()
GITHUB_TOKEN = os.environ.get("GITHUB_TOKEN", "").strip()

# Hosts that are infrastructure endpoints rather than customer-facing sites.
SKIP_SUFFIXES = ("rsend.", "send.", "_domainkey", "resend.")

_lock = threading.Lock()
_cache = {"at": 0, "data": None, "building": False}


def http_json(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "merqato-hub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read().decode())


def http_text(url, headers=None, timeout=8):
    req = urllib.request.Request(url, headers=headers or {"User-Agent": "merqato-hub/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read().decode(errors="replace").strip()


def clean_text(value, limit=60):
    """Return a short plain-text value, or "" for HTML/oversized content.

    Upstreams behind Cloudflare Access answer with a full sign-in page; that
    must never reach the UI as if it were a version string or a label.
    """
    if not isinstance(value, str):
        return ""
    text = " ".join(value.split())
    if not text or len(text) > limit:
        return ""
    if "<" in text or ">" in text:
        return ""
    return text


def safe_http_url(value):
    """Return an absolute HTTP(S) URL, or an empty string when unsafe."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or len(candidate) > 2048:
        return ""
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in candidate):
        return ""
    try:
        parsed = urlparse(candidate)
        parsed.port  # reject malformed ports
    except ValueError:
        return ""
    if parsed.scheme.lower() not in ("http", "https"):
        return ""
    if not parsed.hostname or parsed.username or parsed.password:
        return ""
    return candidate


def safe_domain_url(value):
    """Turn a bare inventory hostname into a safe HTTPS URL."""
    if not isinstance(value, str):
        return ""
    candidate = value.strip()
    if not candidate or any(char.isspace() for char in candidate):
        return ""
    if "/" in candidate or "@" in candidate or "://" in candidate:
        return ""
    return safe_http_url("https://" + candidate)


# ---------------------------------------------------------------- cloudflare
def cf(path):
    return http_json(
        "https://api.cloudflare.com/client/v4" + path,
        headers={"Authorization": "Bearer " + CF_TOKEN, "User-Agent": "merqato-hub/1.0"},
        timeout=10,
    )


def cloudflare_data():
    out = {"zones": 0, "dns_records": 0, "hosts": [], "ok": False}
    if not CF_TOKEN:
        return out
    try:
        zones = cf("/zones?per_page=50")["result"]
        out["zones"] = len(zones)
        for z in zones:
            recs = cf("/zones/%s/dns_records?per_page=200" % z["id"])["result"]
            out["dns_records"] += len(recs)
            for r in recs:
                if r["type"] in ("A", "AAAA", "CNAME"):
                    name = r["name"]
                    if any(s in name for s in SKIP_SUFFIXES):
                        continue
                    out["hosts"].append(name)
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    out["hosts"] = sorted(set(out["hosts"]))
    return out


# ------------------------------------------------------------------- probes
PROBE_TIMEOUT = env_int("PROBE_TIMEOUT", 20)


def probe(host):
    url = "https://" + host
    start = time.time()
    code = 0
    timed_out = False
    try:
        req = urllib.request.Request(url, method="GET", headers={"User-Agent": "merqato-hub/1.0"})
        with urllib.request.urlopen(req, timeout=PROBE_TIMEOUT) as r:
            code = r.status
    except urllib.error.HTTPError as e:
        code = e.code
    except (socket.timeout, TimeoutError):
        timed_out = True
    except Exception:  # noqa: BLE001
        code = 0
    ms = int((time.time() - start) * 1000)
    return {
        "host": host,
        "code": code,
        "ok": 100 <= code < 500,  # 5xx / no response = failing
        "ms": ms,
        # Slow-but-alive hosts are flagged separately so the tile can show
        # "slow" instead of counting them as down.
        "slow": bool(timed_out or ms >= PROBE_TIMEOUT * 1000 * 0.8),
    }


def cert_days(host):
    try:
        ctx = ssl.create_default_context()
        with socket.create_connection((host, 443), timeout=6) as sock:
            with ctx.wrap_socket(sock, server_hostname=host) as ss:
                exp = ss.getpeercert()["notAfter"]
        return int((ssl.cert_time_to_seconds(exp) - time.time()) / 86400)
    except Exception:  # noqa: BLE001
        return None


def probe_all(hosts):
    with ThreadPoolExecutor(max_workers=10) as pool:
        return list(pool.map(probe, hosts))


# -------------------------------------------------------------------- docker
def docker_data():
    out = {"running": 0, "healthy": 0, "unhealthy": 0, "exited": 0, "total": 0, "problems": []}
    try:
        items = http_json(DOCKER + "/containers/json?all=1", timeout=6)
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
        return out
    out["total"] = len(items)
    for c in items:
        name = (c.get("Names") or ["?"])[0].lstrip("/")
        state = c.get("State", "")
        status = c.get("Status", "")
        if state == "running":
            out["running"] += 1
            if "(healthy)" in status:
                out["healthy"] += 1
            if "(unhealthy)" in status:
                out["unhealthy"] += 1
                out["problems"].append(name)
        elif state == "exited":
            out["exited"] += 1
            out["problems"].append(name)
        elif state in ("restarting", "dead", "paused"):
            out["problems"].append(name)
    return out


# -------------------------------------------------------------------- github
def github_data():
    accounts, total = [], 0
    auth_headers = None
    if GITHUB_TOKEN:
        # Authenticated public-account reads use the token owner's quota and
        # avoid the shared-VPS-IP unauthenticated rate limit.
        auth_headers = {
            "Authorization": "Bearer " + GITHUB_TOKEN,
            "Accept": "application/vnd.github+json",
            "User-Agent": "merqato-hub/1.0",
        }
    for login in GITHUB_ACCOUNTS:
        entry = {"login": login, "public_repos": None, "private_repos": None,
                 "total_repos": None, "error": None, "authenticated": False}
        try:
            u = http_json(
                "https://api.github.com/users/" + login,
                headers=auth_headers,
                timeout=8,
            )
            entry["public_repos"] = u.get("public_repos", 0)
            entry["name"] = u.get("name") or login
            entry["updated"] = u.get("updated_at")
            entry["type"] = u.get("type") or "User"
            total += entry["public_repos"] or 0
        except Exception as exc:  # noqa: BLE001
            entry["error"] = str(exc)[:80]

        # Authenticated pass: org repos (incl. private) when the token can see
        # them, and the token owner's own repos. affiliation=owner keeps the
        # count per-account, so a user's org repos are not counted twice.
        if auth_headers:
            hdr = auth_headers
            try:
                repos = []
                if entry.get("type") == "Organization":
                    repos = http_json(
                        "https://api.github.com/orgs/" + login
                        + "/repos?per_page=100&type=all",
                        headers=hdr, timeout=12)
                else:
                    me = http_json("https://api.github.com/user", headers=hdr, timeout=8)
                    if (me.get("login") or "").lower() == login.lower():
                        repos = http_json(
                            "https://api.github.com/user/repos?per_page=100"
                            "&affiliation=owner",
                            headers=hdr, timeout=12)
                priv = sum(1 for r in repos if r.get("private"))
                entry["authenticated"] = bool(repos)
                entry["total_repos"] = len(repos)
                entry["private_repos"] = priv
                entry["visibility"] = "authenticated"
            except Exception as exc:  # noqa: BLE001
                entry["error"] = (entry["error"] or "") + " auth:" + str(exc)[:40]
        accounts.append(entry)
    private_total = sum(a.get("private_repos") or 0 for a in accounts)
    return {
        "accounts": accounts,
        "public_repos_total": total,
        "private_repos_total": private_total,
        "visible_repos_total": total + private_total,
        "authenticated": bool(GITHUB_TOKEN),
    }


# ------------------------------------------------------------------------ vps
def vps_data():
    out = {"ok": False}
    try:
        with open(VPS_FILE, "r", encoding="utf-8") as fh:
            out = json.load(fh)
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


# ------------------------------------------------------------------- coolify
def coolify_get(path):
    return http_json(
        COOLIFY_URL + "/api/v1" + path,
        headers={"Authorization": "Bearer " + COOLIFY_TOKEN, "User-Agent": "merqato-hub/1.0"},
        timeout=10,
    )


def coolify_data():
    out = {
        "ok": False,
        "version": None,
        "projects": 0,
        "applications": 0,
        "servers": 0,
        "reachable": 0,
        "deployments": 0,
        "error": None,
    }
    if not COOLIFY_TOKEN:
        out["error"] = "no token"
        return out
    try:
        raw_version = http_text(
            COOLIFY_URL + "/api/v1/version",
            headers={"Authorization": "Bearer " + COOLIFY_TOKEN, "User-Agent": "merqato-hub/1.0"},
            timeout=8,
        )
        version = clean_text(raw_version, 40)
        if not version:
            raise ValueError("Coolify API returned a non-API page (Cloudflare Access?)")
        out["version"] = version
        out["projects"] = len(coolify_get("/projects") or [])
        out["applications"] = len(coolify_get("/applications") or [])
        servers = coolify_get("/servers") or []
        out["servers"] = len(servers)
        out["reachable"] = sum(1 for s in servers if s.get("is_reachable"))
        out["deployments"] = len(coolify_get("/deployments") or [])
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


# -------------------------------------------------------------------- vercel
VERCEL_TOKEN = os.environ.get("VERCEL_TOKEN", "").strip()
RESEND_TOKEN = os.environ.get("RESEND_API_KEY", "").strip()
ALERT_TO = os.environ.get("ALERT_TO", "david@merqato.digital").strip()
ALERT_FROM = os.environ.get("ALERT_FROM", "MerQato Status <david@merqato.digital>").strip()
ALERT_KEY = os.environ.get("ALERT_KEY", "").strip()


def resend_data():
    """Sending domains and recent delivery outcomes (read-only)."""
    out = {"ok": False, "error": None, "domains": 0, "verified": 0,
           "recent": 0, "delivered": 0, "bounced": 0, "failed": 0, "names": []}
    if not RESEND_TOKEN:
        out["error"] = "no token"
        return out
    hdr = {"Authorization": "Bearer " + RESEND_TOKEN, "User-Agent": "merqato-hub/1.0"}
    try:
        dom = http_json("https://api.resend.com/domains", headers=hdr, timeout=10)
        items = dom.get("data") or []
        out["domains"] = len(items)
        out["verified"] = sum(1 for d in items if d.get("status") == "verified")
        out["names"] = [d.get("name") for d in items][:6]

        emails = http_json("https://api.resend.com/emails?limit=20", headers=hdr, timeout=10)
        recent = emails.get("data") or []
        out["recent"] = len(recent)
        for e in recent:
            ev = (e.get("last_event") or "").lower()
            if ev in ("delivered", "opened", "clicked"):
                out["delivered"] += 1
            elif ev in ("bounced",):
                out["bounced"] += 1
            elif ev in ("failed", "delivery_delayed"):
                out["failed"] += 1
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


def alert_key_matches(provided, expected):
    """Fail closed when no webhook key is configured."""
    if not expected:
        return False
    provided_bytes = str(provided or "").encode("utf-8")
    expected_bytes = str(expected).encode("utf-8")
    return hmac.compare_digest(provided_bytes, expected_bytes)


def send_alert(monitor, status, message):
    """Email an outage/recovery notice through Resend."""
    if not RESEND_TOKEN:
        return False, "no resend key"
    down = str(status).lower() in ("down", "0", "failed")
    subject = ("🔴 DOWN: %s" if down else "✅ UP: %s") % monitor
    body = ("%s is %s.\n\n%s\n\nTime: %s UTC\n"
            % (monitor, "DOWN" if down else "back UP", message or "",
               time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime())))
    payload = json.dumps({
        "from": ALERT_FROM,
        "to": [ALERT_TO],
        "subject": subject,
        "text": body,
    }).encode()
    try:
        req = urllib.request.Request(
            "https://api.resend.com/emails", data=payload, method="POST",
            headers={"Authorization": "Bearer " + RESEND_TOKEN,
                     "Content-Type": "application/json",
                     "User-Agent": "merqato-hub/1.0"})
        with urllib.request.urlopen(req, timeout=15) as r:
            return r.status in (200, 201), "ok"
    except urllib.error.HTTPError as e:
        return False, "http %s" % e.code
    except Exception as exc:  # noqa: BLE001
        return False, str(exc)[:80]


# -------------------------------------------------------------------- vercel


def vercel_data():
    """Projects, deployments and health from the Vercel REST API (read-only)."""
    out = {"ok": False, "error": None, "team": None, "projects": 0,
           "deployments": 0, "ready": 0, "errors": 0, "canceled": 0,
           "error_details": [], "latest": []}
    if not VERCEL_TOKEN:
        out["error"] = "no token"
        return out
    hdr = {"Authorization": "Bearer " + VERCEL_TOKEN, "User-Agent": "merqato-hub/1.0"}
    try:
        teams = http_json("https://api.vercel.com/v2/teams?limit=20", headers=hdr, timeout=10)
        if teams.get("teams"):
            out["team"] = teams["teams"][0].get("slug")

        projects = http_json("https://api.vercel.com/v9/projects?limit=100",
                             headers=hdr, timeout=12)
        plist = projects.get("projects") or []
        out["projects"] = len(plist)
        out["project_names"] = [p.get("name") for p in plist[:20]]

        deps = http_json("https://api.vercel.com/v6/deployments?limit=20",
                         headers=hdr, timeout=12)
        dlist = deps.get("deployments") or []
        out["deployments"] = len(dlist)
        for d in dlist:
            state = (d.get("state") or d.get("readyState") or "").upper()
            if state in ("READY", "FINISHED"):
                out["ready"] += 1
            elif state == "ERROR":
                out["errors"] += 1
                out["error_details"].append({
                    "name": d.get("name"),
                    "state": state,
                    "target": d.get("target"),
                    "created": d.get("createdAt"),
                })
            elif state == "CANCELED":
                out["canceled"] += 1
            if d.get("target") == "production":
                out["latest"].append({
                    "name": d.get("name"),
                    "url": "https://" + (d.get("url") or ""),
                    "state": state,
                    "created": d.get("createdAt"),
                })
        out["latest"] = out["latest"][:5]
        out["ok"] = True
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


def vercel_summary(data):
    """Stable summary fields for the Vercel API contract."""
    return {
        "vc_team": data.get("team") or "n/a",
        "vc_projects": data.get("projects", 0),
        "vc_deployments": data.get("deployments", 0),
        "vc_ready": data.get("ready", 0),
        "vc_errors": data.get("errors", 0),
        "vc_canceled": data.get("canceled", 0),
        "vc_error_details": data.get("error_details", []),
    }


# ----------------------------------------------------------------- inventory
INVENTORY_REPO = os.environ.get("INVENTORY_REPO", "palawan-collective/projects")


def inventory_data():
    """Summary of the existing Projects inventory snapshot (read-only)."""
    out = {"ok": False, "error": None, "captured": None}
    if not GITHUB_TOKEN:
        out["error"] = "no token"
        return out
    try:
        hdr = {"Authorization": "Bearer " + GITHUB_TOKEN,
               "Accept": "application/vnd.github+json",
               "User-Agent": "merqato-hub/1.0"}
        meta = http_json(
            "https://api.github.com/repos/%s/contents/inventory.json" % INVENTORY_REPO,
            headers=hdr, timeout=10)
        raw = base64.b64decode(meta["content"]).decode("utf-8", "replace")
        data = json.loads(raw)
        summary = data.get("summary") or {}
        out.update({
            "ok": True,
            "captured": data.get("capturedOn"),
            "repositories": summary.get("repositories", 0),
            "vercel_projects": summary.get("vercelProjects", 0),
            "matched_deployments": summary.get("matchedDeploymentSources", 0),
            "deployment_errors": summary.get("latestDeploymentErrors", 0),
            "review_notes": len(data.get("reviewNotes") or []),
        })
        out["projects"] = normalize_projects(data.get("projects") or [])
    except Exception as exc:  # noqa: BLE001
        out["error"] = str(exc)[:120]
    return out


def normalize_projects(items):
    """Flatten inventory rows into safe, renderable project records."""
    rows = []
    if not isinstance(items, list):
        return rows
    for item in items[:200]:
        if not isinstance(item, dict):
            continue
        try:
            domains = item.get("domains") or []
            if not isinstance(domains, list):
                domains = []
            url = ""
            for domain in domains:
                url = safe_domain_url(domain)
                if url:
                    break
            deployment_raw = item.get("deploymentURL")
            deployment_url = safe_http_url(deployment_raw)
            if deployment_raw and not deployment_url:
                continue
            if not url:
                url = deployment_url

            repo_raw = item.get("repoURL")
            repo_url = safe_http_url(repo_raw)
            if repo_raw and not repo_url:
                continue

            name = item.get("project")
            if not isinstance(name, str) or not name.strip():
                name = "untitled"
            repo = item.get("repo")
            if not isinstance(repo, str):
                repo = None
            state = item.get("state")
            if not isinstance(state, str):
                state = ""
            rows.append({
                "name": name.strip()[:120],
                "url": url,
                "repo": repo,
                "repoUrl": repo_url,
                "provider": "Vercel" if item.get("vercelTeam") else None,
                "state": state.strip().upper() or None,
                "updatedAt": item.get("updatedAt"),
                "protected": bool(item.get("protected")),
            })
        except Exception:  # noqa: BLE001
            # One malformed inventory row must not discard all valid projects.
            continue
    rows.sort(key=lambda r: (r["state"] != "ERROR", r["name"].lower()))
    return rows


def summarize_probes(probes):
    """Summarize HTTPS probes without hiding failing hostnames."""
    failing = [p for p in probes if not p["ok"]]
    slow = [p["host"] for p in probes if p["ok"] and p["slow"]]
    failing_hosts = [p["host"] for p in failing]
    slow_hosts = list(slow)
    return {
        "total": len(probes),
        "ok": len(probes) - len(failing),
        "failing": len(failing),
        "failing_hosts": failing_hosts,
        "failing_names": ", ".join(failing_hosts) or "none",
        "slowest_ms": max([p["ms"] for p in probes], default=0),
        "slow_count": len(slow_hosts),
        "slow_hosts": slow_hosts,
        "slow_names": ", ".join(slow_hosts) or "none",
    }


# --------------------------------------------------------------------- build
def build():
    cfdata = cloudflare_data()
    hosts = cfdata["hosts"]
    probes = probe_all(hosts) if hosts else []

    domain_summary = summarize_probes(probes)

    certs = {}
    for p in probes[:12]:
        d = cert_days(p["host"])
        if d is not None:
            certs[p["host"]] = d
    min_cert = min(certs.values()) if certs else None

    dock = docker_data()
    gh = github_data()
    cld = coolify_data()
    vps = vps_data()
    inv = inventory_data()
    vc = vercel_data()
    rs = resend_data()

    return {
        "updated": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "domains": domain_summary,
        "certificates": {
            "checked": len(certs),
            "min_days": min_cert if min_cert is not None else -1,
        },
        "docker": dock,
        "github": gh,
        "coolify": cld,
        "vps": vps,
        "inventory": inv,
        "vercel": vc,
        "resend": rs,
        "cloudflare": {
            "zones": cfdata["zones"],
            "dns_records": cfdata["dns_records"],
            "ok": cfdata["ok"],
            "error": cfdata.get("error"),
        },
        "detail": {"probes": probes, "certificates": certs},
    }


def _rebuild():
    """Refresh the cache in place. Safe to run from a background thread."""
    try:
        data = build()
    except Exception as exc:  # noqa: BLE001
        with _lock:
            if _cache["data"]:
                _cache["data"]["last_error"] = str(exc)[:160]
                _cache["at"] = time.time()  # back off briefly, keep serving stale
        return
    with _lock:
        _cache["data"] = data
        _cache["at"] = time.time()


def _rebuild_after():
    try:
        _rebuild()
    finally:
        with _lock:
            _cache["building"] = False


def cached():
    """Never block a request on a full rebuild.

    A cold or stale cache returns whatever we have immediately and refreshes in
    the background, so dashboard tiles answer in milliseconds instead of waiting
    on 18 outbound HTTPS probes. Only the very first request after startup waits,
    and only because there is nothing to serve yet.
    """
    with _lock:
        data = _cache["data"]
        fresh = data is not None and (time.time() - _cache["at"]) < CACHE_TTL
        if fresh:
            return data
        if data is not None:
            # Stale but usable: serve it now, refresh behind the scenes.
            if not _cache["building"]:
                _cache["building"] = True
                threading.Thread(target=_rebuild_after, daemon=True).start()
            out = dict(data)
            out["stale"] = True
            return out

    # No data yet (first request after startup): wait briefly for the first build.
    deadline = time.time() + float(os.environ.get("FIRST_BUILD_WAIT", "25"))
    while time.time() < deadline:
        with _lock:
            data = _cache["data"]
            if data is not None:
                out = dict(data)
                out["stale"] = False
                return out
        time.sleep(0.4)

    return {"stale": True, "error": "cache warming", "updated": None}


# ---------------------------------------------------------------------- http
class Handler(BaseHTTPRequestHandler):
    server_version = "merqato-hub/1.0"

    def _send(self, payload, code=200):
        body = json.dumps(payload, default=str).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        self.wfile.write(body)

    def do_GET(self):  # noqa: N802
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path in ("/", "/health"):
                self._send({"status": "ok", "service": "merqato-hub", "build": BUILD_ID})
                return
            data = cached()
            if path == "/summary.json":
                self._send({
                    "domains_total": data["domains"]["total"],
                    "domains_ok": data["domains"]["ok"],
                    "domains_failing": data["domains"]["failing"],
                    "failing_names": data["domains"]["failing_names"],
                    "failing_hosts": data["domains"].get("failing_hosts", []),
                    "slow_count": data["domains"].get("slow_count", 0),
                    "slow_hosts": data["domains"].get("slow_hosts", []),
                    "containers_running": data["docker"]["running"],
                    "containers_healthy": data["docker"]["healthy"],
                    "containers_unhealthy": data["docker"]["unhealthy"],
                    "containers_total": data["docker"]["total"],
                    "github_repos": data["github"]["public_repos_total"],
                    "github_private_repos": data["github"]["private_repos_total"],
                    "github_visible_repos": data["github"]["visible_repos_total"],
                    "github_authenticated": data["github"]["authenticated"],
                    "cf_zones": data["cloudflare"]["zones"],
                    "cf_dns_records": data["cloudflare"]["dns_records"],
                    "cert_min_days": data["certificates"]["min_days"],
                    "coolify_projects": data["coolify"]["projects"],
                    "coolify_apps": data["coolify"]["applications"],
                    "coolify_servers": data["coolify"]["servers"],
                    "coolify_reachable": data["coolify"]["reachable"],
                    "coolify_deployments": data["coolify"]["deployments"],
                    "coolify_version": data["coolify"]["version"] or "n/a",
                    "vps_instance": data["vps"].get("instance_id") or "n/a",
                    "vps_region": data["vps"].get("region") or "n/a",
                    "vps_cpu_pct": data["vps"].get("cpu_used_pct", -1),
                    "vps_mem_pct": data["vps"].get("mem_used_pct", -1),
                    "vps_mem_free_mb": (data["vps"].get("mem_total_mb", 0) - data["vps"].get("mem_used_mb", 0)),
                    "vps_disk_pct": data["vps"].get("disk_used_pct", -1),
                    "vps_load1": data["vps"].get("load1", -1),
                    "vps_uptime_h": data["vps"].get("uptime_hours", -1),
                    "inv_repositories": data["inventory"].get("repositories", 0),
                    "inv_vercel_projects": data["inventory"].get("vercel_projects", 0),
                    "inv_deploy_errors": data["inventory"].get("deployment_errors", 0),
                    "inv_captured": data["inventory"].get("captured") or "n/a",
                    **vercel_summary(data["vercel"]),
                    "rs_domains": data["resend"].get("domains", 0),
                    "rs_verified": data["resend"].get("verified", 0),
                    "rs_delivered": data["resend"].get("delivered", 0),
                    "rs_bounced": data["resend"].get("bounced", 0),
                    "rs_failed": data["resend"].get("failed", 0),
                    "updated": data["updated"],
                })
            elif path == "/domains.json":
                self._send(data["detail"])
            elif path == "/github.json":
                self._send(data["github"])
            elif path == "/docker.json":
                self._send(data["docker"])
            elif path == "/cloudflare.json":
                self._send(data["cloudflare"])
            elif path == "/coolify.json":
                self._send(data["coolify"])
            elif path == "/vps.json":
                self._send(data["vps"])
            elif path == "/inventory.json":
                self._send(data["inventory"])
            elif path == "/projects.json":
                self._send({"projects": data["inventory"].get("projects") or []})
            elif path == "/vercel.json":
                self._send(data["vercel"])
            elif path == "/resend.json":
                self._send(data["resend"])
            elif path == "/full.json":
                self._send(data)
            else:
                self._send({"error": "not found"}, 404)
        except Exception as exc:  # noqa: BLE001
            self._send({"error": str(exc)[:200]}, 500)

    def do_POST(self):  # noqa: N802
        """Alert sink for Uptime Kuma's webhook notification."""
        path = self.path.split("?")[0].rstrip("/") or "/"
        try:
            if path != "/alert":
                self._send({"error": "not found"}, 404)
                return

            # Authenticate before reading any request body so an unauthenticated
            # caller cannot tie up a worker or force an oversized allocation.
            if not alert_key_matches(self.headers.get("X-Hub-Key"), ALERT_KEY):
                self._send({"error": "forbidden"}, 403)
                return

            try:
                length = int(self.headers.get("Content-Length") or 0)
            except (TypeError, ValueError):
                self._send({"error": "invalid content length"}, 400)
                return
            if length < 0 or length > 65536:
                self._send({"error": "body too large"}, 413)
                return

            raw = self.rfile.read(length).decode("utf-8", "replace") if length else "{}"
            try:
                data = json.loads(raw)
            except Exception:  # noqa: BLE001
                data = {}
            monitor = ((data.get("monitor") or {}).get("name")
                       or data.get("name") or "unknown")
            hb = data.get("heartbeat") or {}
            status = hb.get("status", data.get("status", "down"))
            msg = data.get("msg") or hb.get("msg") or ""
            ok, info = send_alert(monitor, status, msg)
            self._send({"ok": ok, "monitor": monitor, "detail": info},
                       200 if ok else 502)
        except Exception as exc:  # noqa: BLE001
            self._send({"error": str(exc)[:160]}, 500)

    def log_message(self, fmt, *args):  # keep the log quiet
        pass


if __name__ == "__main__":
    print("hub api listening on :%d (ttl=%ds)" % (PORT, CACHE_TTL), flush=True)
    # Build once at startup so the first visitor gets a warm cache instead of
    # waiting on a full probe cycle.
    threading.Thread(target=_rebuild_after, daemon=True).start()
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()
