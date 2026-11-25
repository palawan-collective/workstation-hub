#!/usr/bin/env python3
"""MerQato Workstation — dashboard API and static host.

Standard library only: no framework, no build step, nothing to keep patched.
Serves the single-page UI plus the Quick Links Manager API. Metrics are not
computed here — the browser reads them from the hub API at /hub/summary.json,
which keeps one source of truth for every number on screen.

Endpoints
    GET    /                     the SPA
    GET    /static/<file>        css, js, fonts, icons
    GET    /api/health           liveness
    POST   /api/login            username + password -> session cookie
    POST   /api/logout           clear the session
    GET    /api/session          current user and role
    POST   /api/password         change own password                 [session]
    GET    /api/links            list links                          [session]
    POST   /api/links            add a link                          [session]
    PATCH  /api/links/<id>       edit a link                         [session]
    DELETE /api/links/<id>       delete a link                       [session]
    POST   /api/links/reorder    persist drag ordering               [session]
    POST   /api/links/import     replace from JSON                   [session]
    GET    /api/export           download all links as JSON          [session]
    GET    /api/users            list users                          [admin]
    POST   /api/users            add user with role                  [admin]
    PATCH  /api/users/<name>     change role / reset password        [admin]
    DELETE /api/users/<name>     remove user                         [admin]
    GET    /api/agent/config     agent settings (key masked)         [admin]
    PUT    /api/agent/config     update agent settings               [admin]
    GET    /api/agent/models     free models (refresh=1 to re-sync)  [admin]
    POST   /api/agent/chat       send a message through the agent    [session]
"""

import json
import os
import re
import secrets
import shutil
import threading
import time
import uuid
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

try:
    import admin
    import scan
except ModuleNotFoundError:  # loaded by path in unit tests
    import sys
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import admin
    import scan

PORT = int(os.environ.get("WS_PORT", "3005"))
DATA_DIR = os.environ.get("WS_DATA_DIR", "/data")
LINKS_FILE = os.path.join(DATA_DIR, "links.json")
BACKUP_DIR = os.path.join(DATA_DIR, "backups")
STATIC_DIR = os.environ.get("WS_STATIC_DIR", "/app/static")
SESSION_COOKIE = "ws_session"
SESSION_TTL = 12 * 3600
MAX_BACKUPS = 20

CATEGORIES = ["Infrastructure", "Projects", "Clients", "Email", "Agents", "Other"]

MIME = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".woff2": "font/woff2",
    ".ico": "image/x-icon",
}

_lock = threading.Lock()


# --------------------------------------------------------------------- storage
def ensure_dirs():
    os.makedirs(DATA_DIR, exist_ok=True)
    os.makedirs(BACKUP_DIR, exist_ok=True)
    secret_path = os.path.join(DATA_DIR, ".session_secret")
    if not os.path.exists(secret_path):
        with open(secret_path, "w", encoding="utf-8") as fh:
            fh.write(secrets.token_hex(32))
        os.chmod(secret_path, 0o600)
    return secret_path


def session_secret():
    with open(os.path.join(DATA_DIR, ".session_secret"), "r", encoding="utf-8") as fh:
        return fh.read().strip().encode()


def _backup_locked():
    try:
        os.makedirs(BACKUP_DIR, exist_ok=True)
        dest = os.path.join(BACKUP_DIR, "links-%s.json" % time.strftime("%Y%m%d-%H%M%S"))
        shutil.copy2(LINKS_FILE, dest)
        files = sorted(os.listdir(BACKUP_DIR))
        for old in files[:-MAX_BACKUPS]:
            os.remove(os.path.join(BACKUP_DIR, old))
    except Exception:
        pass


def _write_locked(links):
    tmp = LINKS_FILE + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump({"links": links}, fh, indent=2)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, LINKS_FILE)
    _backup_locked()


def load_links():
    with _lock:
        try:
            with open(LINKS_FILE, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            links = data.get("links") if isinstance(data, dict) else data
            return links if isinstance(links, list) else []
        except FileNotFoundError:
            return []
        except Exception:
            return []


def save_links(links):
    with _lock:
        _write_locked(links)


# ------------------------------------------------------------------ validation
URL_RE = re.compile(r"^https?://[^\s/$.?#].[^\s]*$", re.IGNORECASE)
ICON_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,39}$", re.IGNORECASE)


def clean_url(raw):
    if not isinstance(raw, str):
        return None, "URL must be text"
    url = raw.strip()
    if not url:
        return None, "URL is required"
    if len(url) > 2048:
        return None, "URL is too long"
    if any(char.isspace() or ord(char) < 32 or ord(char) == 127 for char in url):
        return None, "URL does not look valid"
    try:
        parsed = urlparse(url)
        hostname = parsed.hostname
        parsed.port  # reject malformed and out-of-range ports
    except ValueError:
        return None, "URL does not look valid"
    if parsed.scheme.lower() not in ("http", "https"):
        return None, "Only http and https links are allowed"
    if not parsed.netloc or not hostname or parsed.username or parsed.password:
        return None, "URL must be a plain http(s) address without credentials"
    if not URL_RE.match(url):
        return None, "URL does not look valid"
    return url, None


def clean_icon(raw):
    """Normalize a Lucide-style icon name without accepting arbitrary data."""
    if raw is None or raw == "":
        return "link", None
    if not isinstance(raw, str):
        return None, "Icon must be text"
    value = raw.strip().lower()
    if not value:
        return "link", None
    if not ICON_RE.match(value):
        return None, "Icon contains unsupported characters"
    return value, None


def clean_text(value, field, limit, required=False):
    if value is None:
        value = ""
    if not isinstance(value, str):
        return None, "%s must be text" % field
    value = value.strip()
    if required and not value:
        return None, "%s is required" % field
    if len(value) > limit:
        return None, "%s is too long (max %d)" % (field, limit)
    return value, None


def validate(payload, partial=False):
    """Return (fields, error). partial=True allows editing a subset."""
    fields = {}
    if not isinstance(payload, dict):
        return None, "Body must be a JSON object"

    if not partial or "name" in payload:
        name, err = clean_text(payload.get("name"), "Name", 80, required=True)
        if err:
            return None, err
        fields["name"] = name

    if not partial or "url" in payload:
        url, err = clean_url(payload.get("url"))
        if err:
            return None, err
        fields["url"] = url

    if not partial or "icon" in payload:
        icon, err = clean_icon(payload.get("icon"))
        if err:
            return None, err
        fields["icon"] = icon

    if not partial or "category" in payload:
        cat, err = clean_text(payload.get("category"), "Category", 40, required=True)
        if err:
            return None, err
        if cat not in CATEGORIES:
            return None, "Category must be one of: " + ", ".join(CATEGORIES)
        fields["category"] = cat

    if not partial or "description" in payload:
        desc, err = clean_text(payload.get("description"), "Description", 200)
        if err:
            return None, err
        fields["description"] = desc

    if "pinned" in payload:
        fields["pinned"] = bool(payload.get("pinned"))

    return fields, None


# ---------------------------------------------------------------------- session
def make_session(username):
    import base64
    import hashlib
    import hmac

    ts = str(int(time.time()))
    payload = base64.urlsafe_b64encode((username + "|" + ts).encode()).decode()
    sig = hmac.new(session_secret(), payload.encode(), hashlib.sha256).hexdigest()
    return payload + "." + sig


def read_session(token):
    import base64
    import hashlib
    import hmac

    try:
        payload, sig = token.encode().split(b".", 1)
        expected = hmac.new(session_secret(), payload, hashlib.sha256).hexdigest().encode()
        if not hmac.compare_digest(sig, expected):
            return None
        decoded = base64.urlsafe_b64decode(payload).decode()
        username, ts_raw = decoded.split("|", 1)
        if (time.time() - int(ts_raw)) >= SESSION_TTL:
            return None
        user = admin.find_user(username)
        if not user or user.get("disabled"):
            return None
        return {"username": user["username"], "role": user.get("role", "viewer")}
    except Exception:
        return None


def cookie_for(username):
    return "%s=%s; Path=/; HttpOnly; Secure; SameSite=Strict; Max-Age=%d" % (
        SESSION_COOKIE, make_session(username), SESSION_TTL)


# ----------------------------------------------------------------------- seeds
def seed_links():
    """First run: import the tools already on the old dashboard, once."""
    presets = [
        ("GitHub", "https://github.com/palawan-collective", "Infrastructure",
         "All repositories consolidated in palawan-collective", True, "github"),
        ("Vercel", "https://vercel.com/merqatodigital", "Infrastructure",
         "Deployments and production URLs", True, "vercel"),
        ("Cloudflare", "https://dash.cloudflare.com/4322261acf290539b992fbd803039ca7",
         "Infrastructure", "DNS, Access, email routing", True, "cloudflare"),
        ("Coolify", "https://coolify.merqato.digital", "Infrastructure",
         "Deploy and control panel", True, "coolify"),
        ("UpCloud", "https://hub.upcloud.com/dashboard", "Infrastructure",
         "VPS control panel", True, "upcloud"),
        ("Bitwarden", "https://vault.bitwarden.com", "Agents",
         "Credential vault", False, "bitwarden"),
        ("Social Studio", "https://social.merqato.digital", "Projects",
         "Django social application", False, "social"),
        ("Projects Inventory", "https://projects-omega-neon.vercel.app/admin",
         "Projects", "Code inventory admin (login required)", True, "projects"),
        ("Uptime Kuma", "https://status.merqato.digital", "Infrastructure",
         "Uptime monitoring and alerts", True, "activity"),
        ("Resend", "https://resend.com/domains", "Email",
         "Email delivery and domains", False, "mail"),
        ("HyperLevel CRM", "https://crm.merqato.digital", "Projects",
         "Business OS: clients, deals, invoicing", False, "hyperlevel"),
        ("Workstation Repo", "https://github.com/palawan-collective/workstation-hub",
         "Projects", "This dashboard's source and docs", False, "github"),
        ("Palawan Collective", "https://palawancollective.com", "Clients",
         "Agency website", False, "globe"),
        ("BAIA Lodge", "https://baia.merqato.digital", "Clients",
         "Client site", False, "globe"),
        ("Golden Goose Eggs Work",
         "https://github.com/palawan-collective/golden-goose-eggs-work",
         "Clients", "Client project repository", False, "globe"),
        ("Azarraga Glass", "https://azarraga.merqato.digital", "Clients",
         "Client site", False, "globe"),
        ("SBI Medical Deck", "https://presentation.merqato.digital", "Clients",
         "Client presentation", False, "globe"),
    ]
    links = []
    for name, url, cat, desc, pinned, icon in presets:
        links.append({
            "id": uuid.uuid4().hex[:12],
            "name": name, "url": url, "category": cat,
            "description": desc, "pinned": pinned, "icon": icon,
            "createdAt": int(time.time()), "order": len(links),
        })
    return links


# ---------------------------------------------------------------------- handler
class Handler(BaseHTTPRequestHandler):
    server_version = "merqato-workstation/1.0"

    # ---- helpers
    def _send(self, payload, code=200, ctype="application/json; charset=utf-8",
              extra_headers=None):
        if isinstance(payload, (dict, list)):
            body = json.dumps(payload).encode()
        elif isinstance(payload, str):
            body = payload.encode()
        else:
            body = payload
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "same-origin")
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        if self.command != "HEAD":
            self.wfile.write(body)

    def _cookie_token(self):
        for part in (self.headers.get("Cookie") or "").split(";"):
            if "=" in part:
                key, value = part.split("=", 1)
                if key.strip() == SESSION_COOKIE:
                    return value.strip()
        return None

    def _authed(self):
        token = self._cookie_token()
        return read_session(token) if token else None

    def _admin(self):
        user = self._authed()
        if not user:
            self._send({"error": "Sign in required"}, 401)
            return None
        if user.get("role") != "admin":
            self._send({"error": "Admin access required"}, 403)
            return None
        return user

    def _writer(self):
        """Admins and operators may change operational data; viewers may not."""
        user = self._authed()
        if not user:
            self._send({"error": "Sign in required"}, 401)
            return None
        if user.get("role") not in ("admin", "operator"):
            self._send({"error": "Operator access required"}, 403)
            return None
        return user

    def _same_origin(self):
        """Reject cross-site writes even if a session cookie leaked."""
        origin = self.headers.get("Origin")
        if not origin:
            return True
        return urlparse(origin).netloc == self.headers.get("Host")

    def _body_json(self):
        length = int(self.headers.get("Content-Length") or 0)
        if length <= 0 or length > 65536:
            return None, "Empty or oversized body"
        raw = self.rfile.read(length)
        try:
            return json.loads(raw.decode("utf-8")), None
        except Exception:
            return None, "Body must be valid JSON"

    def _static(self, rel):
        safe = os.path.normpath(rel).lstrip("/\\")
        full = os.path.join(STATIC_DIR, safe)
        if not full.startswith(os.path.abspath(STATIC_DIR)):
            return self._send({"error": "forbidden"}, 403)
        if not os.path.isfile(full):
            return self._send({"error": "not found"}, 404)
        ext = os.path.splitext(full)[1].lower()
        with open(full, "rb") as fh:
            data = fh.read()
        # Code must revalidate so a deploy is visible immediately; fonts and
        # icons are content-stable and can be cached hard.
        if ext in (".css", ".js", ".html"):
            headers = {"Cache-Control": "no-cache"}
        else:
            headers = {"Cache-Control": "public, max-age=86400"}
        self._send(data, 200, MIME.get(ext, "application/octet-stream"), headers)

    # ---- routes
    def do_GET(self):  # noqa: N802
        parsed = urlparse(self.path)
        path = parsed.path
        if path in ("/", "/index.html"):
            return self._static("index.html")
        if path.startswith("/static/"):
            return self._static(path[len("/static/"):])
        if path == "/favicon.ico":
            return self._static("favicon.svg")
        if path == "/api/health":
            return self._send({"ok": True, "service": "workstation"})
        if path == "/api/session":
            user = self._authed()
            return self._send({"ok": bool(user), "user": user})
        if path == "/api/users":
            if not self._admin():
                return None
            return self._send({"users": admin.list_users(), "roles": list(admin.ROLES)})
        if path == "/api/agent/config":
            if not self._admin():
                return None
            return self._send(admin.public_config())
        if path == "/api/agent/models":
            if not self._admin():
                return None
            query = parse_qs(parsed.query)
            provider = (query.get("provider") or [None])[0]
            refresh = (query.get("refresh") or [""])[0] == "1"
            try:
                return self._send(admin.fetch_models(provider, refresh=refresh))
            except Exception as exc:
                return self._send({"error": str(exc)}, 502)

        user = self._authed()
        if not user:
            return self._send({"error": "Sign in required"}, 401)
        if path == "/api/scans":
            return self._send({"scans": scan.load_reports()[:24]})
        if path == "/api/links":
            links = sorted(load_links(), key=lambda l: (l.get("order", 0), l.get("name", "")))
            return self._send({"links": links, "categories": CATEGORIES, "user": user})
        if path == "/api/starter-links":
            return self._send({"links": seed_links(), "categories": CATEGORIES})
        if path == "/api/export":
            links = load_links()
            return self._send(
                {"links": links},
                200,
                extra_headers={"Content-Disposition": 'attachment; filename="merqato-links.json"'})
        return self._send({"error": "not found"}, 404)

    def do_HEAD(self):  # noqa: N802
        # Never run GET logic on HEAD: it can fetch models and mutate the cache.
        return self._send(b"", 200, "text/plain")

    def do_POST(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path in ("/api/login", "/api/logout"):
            if not self._same_origin():
                return self._send({"error": "Cross-site writes are not allowed"}, 403)
        else:
            if not self._authed():
                return self._send({"error": "Sign in required"}, 401)
            if not self._same_origin():
                return self._send({"error": "Cross-site writes are not allowed"}, 403)

        if path == "/api/login":
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            user = admin.authenticate(
                (payload or {}).get("username"), (payload or {}).get("password"))
            if not user:
                return self._send({"error": "Incorrect username or password"}, 401)
            return self._send(
                {"ok": True, "user": user}, 200,
                extra_headers={"Set-Cookie": cookie_for(user["username"])})

        if path == "/api/logout":
            return self._send({"ok": True}, 200, extra_headers={
                "Set-Cookie": SESSION_COOKIE + "=; Path=/; HttpOnly; SameSite=Strict; Max-Age=0"})

        if path == "/api/session":
            return self._send({"ok": True})

        user = self._authed()
        if path == "/api/password":
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            try:
                admin.change_password(
                    user["username"], (payload or {}).get("current"),
                    (payload or {}).get("new"))
            except (ValueError, PermissionError) as exc:
                return self._send({"error": str(exc)}, 400)
            return self._send({"ok": True, "message": "Password changed"})

        if path == "/api/users":
            if user.get("role") != "admin":
                return self._send({"error": "Admin access required"}, 403)
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            try:
                name = admin.add_user(
                    (payload or {}).get("username"), (payload or {}).get("password"),
                    (payload or {}).get("role", "viewer"))
            except (ValueError, PermissionError) as exc:
                return self._send({"error": str(exc)}, 400)
            return self._send({"ok": True, "username": name}, 201)

        if path == "/api/agent/scan":
            if not self._writer():
                return None
            try:
                return self._send(scan.run_and_save())
            except Exception as exc:
                return self._send({"error": str(exc)}, 400)

        if path == "/api/agent/chat":
            if user.get("role") not in ("admin", "operator"):
                return self._send({"error": "Operator access required"}, 403)
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            message = (payload or {}).get("message", "")
            if not isinstance(message, str) or not message.strip():
                return self._send({"error": "Message is required"}, 400)
            try:
                return self._send({"reply": admin.chat(message[:4000])})
            except Exception as exc:
                # 4xx keeps the real message visible through Cloudflare.
                return self._send({"error": str(exc)}, 400)

        if path == "/api/links" and not self._writer():
            return None
        if path == "/api/links":
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            fields, err = validate(payload)
            if err:
                return self._send({"error": err}, 400)
            links = load_links()
            order = max([l.get("order", 0) for l in links], default=-1) + 1
            link = {
                "id": uuid.uuid4().hex[:12],
                "name": fields["name"],
                "url": fields["url"],
                "category": fields["category"],
                "description": fields.get("description", ""),
                "pinned": fields.get("pinned", False),
                "icon": fields.get("icon", "link"),
                "createdAt": int(time.time()),
                "order": order,
            }
            links.append(link)
            save_links(links)
            return self._send({"ok": True, "link": link}, 201)

        if path == "/api/links/reorder" and not self._writer():
            return None
        if path == "/api/links/reorder":
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            order = payload.get("order") if isinstance(payload, dict) else None
            if not isinstance(order, list):
                return self._send({"error": "order must be a list of ids"}, 400)
            links = load_links()
            index = {l["id"]: l for l in links}
            for position, link_id in enumerate(order[: len(links)]):
                if link_id in index:
                    index[link_id]["order"] = position
            save_links(links)
            return self._send({"ok": True})

        if path == "/api/links/import" and not self._writer():
            return None
        if path == "/api/links/import":
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            incoming = payload.get("links") if isinstance(payload, dict) else None
            if not isinstance(incoming, list):
                return self._send({"error": "links must be a list"}, 400)
            if len(incoming) > 500:
                return self._send({"error": "Too many links (max 500)"}, 400)
            links = []
            used_ids = set()
            for item in incoming:
                fields, err = validate(item)
                if err:
                    return self._send({"error": "Row %d: %s" % (len(links) + 1, err)}, 400)
                link_id = str(item.get("id", ""))
                if not re.fullmatch(r"[a-f0-9]{12}", link_id) or link_id in used_ids:
                    link_id = uuid.uuid4().hex[:12]
                used_ids.add(link_id)
                try:
                    created_at = int(item.get("createdAt", 0)) or int(time.time())
                except (TypeError, ValueError):
                    created_at = int(time.time())
                try:
                    order = int(item.get("order", len(links)))
                except (TypeError, ValueError):
                    order = len(links)
                links.append({
                    "id": link_id,
                    "name": fields["name"], "url": fields["url"],
                    "category": fields["category"],
                    "description": fields.get("description", ""),
                    "pinned": fields.get("pinned", False),
                    "icon": fields.get("icon", "link"),
                    "createdAt": created_at,
                    "order": order,
                })
            save_links(links)
            return self._send({"ok": True, "count": len(links)})

        return self._send({"error": "not found"}, 404)

    def do_PUT(self):  # noqa: N802
        path = self.path.split("?")[0]
        user = self._admin()
        if not user:
            return None
        if not self._same_origin():
            return self._send({"error": "Cross-site writes are not allowed"}, 403)
        if path != "/api/agent/config":
            return self._send({"error": "not found"}, 404)
        payload, err = self._body_json()
        if err:
            return self._send({"error": err}, 400)
        try:
            cfg = admin.save_agent_config(payload or {})
        except (ValueError, PermissionError) as exc:
            return self._send({"error": str(exc)}, 400)
        return self._send(admin.public_config(cfg))

    def do_PATCH(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api/users/"):
            user = self._admin()
            if not user:
                return None
            if not self._same_origin():
                return self._send({"error": "Cross-site writes are not allowed"}, 403)
            payload, err = self._body_json()
            if err:
                return self._send({"error": err}, 400)
            try:
                name = admin.update_user(unquote(path[len("/api/users/"):]), payload or {})
            except (ValueError, PermissionError) as exc:
                return self._send({"error": str(exc)}, 400)
            return self._send({"ok": True, "username": name})
        if not path.startswith("/api/links/"):
            return self._send({"error": "not found"}, 404)
        if not self._writer():
            return None
        if not self._same_origin():
            return self._send({"error": "Cross-site writes are not allowed"}, 403)

        link_id = path[len("/api/links/"):]
        payload, err = self._body_json()
        if err:
            return self._send({"error": err}, 400)
        fields, err = validate(payload, partial=True)
        if err:
            return self._send({"error": err}, 400)

        links = load_links()
        for link in links:
            if link.get("id") == link_id:
                link.update(fields)
                save_links(links)
                return self._send({"ok": True, "link": link})
        return self._send({"error": "Link not found"}, 404)

    def do_DELETE(self):  # noqa: N802
        path = self.path.split("?")[0]
        if path.startswith("/api/users/"):
            user = self._admin()
            if not user:
                return None
            if not self._same_origin():
                return self._send({"error": "Cross-site writes are not allowed"}, 403)
            try:
                name = admin.delete_user(unquote(path[len("/api/users/"):]))
            except (ValueError, PermissionError) as exc:
                return self._send({"error": str(exc)}, 400)
            return self._send({"ok": True, "username": name})
        if not path.startswith("/api/links/"):
            return self._send({"error": "not found"}, 404)
        if not self._writer():
            return None
        if not self._same_origin():
            return self._send({"error": "Cross-site writes are not allowed"}, 403)

        link_id = path[len("/api/links/"):]
        links = load_links()
        remaining = [l for l in links if l.get("id") != link_id]
        if len(remaining) == len(links):
            return self._send({"error": "Link not found"}, 404)
        save_links(remaining)
        return self._send({"ok": True})

    def log_message(self, fmt, *args):
        pass


def main():
    ensure_dirs()
    admin.seed_users()
    if not os.path.exists(LINKS_FILE):
        save_links(seed_links())
        print("seeded starter links", flush=True)
    print("workstation listening on :%d (data=%s)" % (PORT, DATA_DIR), flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), Handler).serve_forever()


if __name__ == "__main__":
    main()