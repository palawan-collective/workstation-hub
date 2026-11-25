"""Workstation admin: users, roles, agent config and free-model catalog.

Standard library only. All state lives in WS_DATA_DIR with atomic writes and
backups, matching the links store. No secret is ever returned to the browser.
"""

import hashlib
import hmac
import json
import os
import secrets
import threading
import time
import urllib.parse
import urllib.request

ROLES = ("admin", "operator", "viewer")
PBKDF2_ROUNDS = 200_000
MODEL_CACHE_TTL = 600

_lock = threading.RLock()
_model_cache = {}


def data_dir():
    return os.environ.get("WS_DATA_DIR", "/data")


def _path(name):
    return os.path.join(data_dir(), name)


def _atomic_write(path, obj):
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as fh:
        json.dump(obj, fh, indent=2, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, path)


def _backup(path, prefix):
    try:
        from shutil import copy2
        folder = os.path.join(data_dir(), "backups")
        os.makedirs(folder, exist_ok=True)
        copy2(path, os.path.join(folder, "%s-%s.json" % (prefix, time.strftime("%Y%m%d-%H%M%S"))))
    except Exception:
        pass


# ----------------------------------------------------------------- passwords
def hash_password(password):
    salt = secrets.token_bytes(16)
    digest = hashlib.pbkdf2_hmac("sha256", password.encode("utf-8"), salt, PBKDF2_ROUNDS)
    return "pbkdf2$%d$%s$%s" % (PBKDF2_ROUNDS, salt.hex(), digest.hex())


def verify_password(password, stored):
    try:
        algo, rounds, salt, digest = str(stored).split("$")
        if algo != "pbkdf2":
            return False
        candidate = hashlib.pbkdf2_hmac(
            "sha256", password.encode("utf-8"), bytes.fromhex(salt), int(rounds))
        return hmac.compare_digest(candidate.hex(), digest)
    except Exception:
        return False


# --------------------------------------------------------------------- users
def load_users():
    path = _path("users.json")
    if not os.path.exists(path):
        return {"users": []}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            return json.load(fh)
    except Exception:
        return {"users": []}


def _save_users(store):
    path = _path("users.json")
    if os.path.exists(path):
        _backup(path, "users")
    _atomic_write(path, store)


def seed_users():
    """Create the first admin once. Password comes from WS_ADMIN_PASSWORD."""
    with _lock:
        store = load_users()
        if store.get("users"):
            return None
        username = os.environ.get("WS_ADMIN_USER", "david@merqato.digital").strip()
        password = os.environ.get("WS_ADMIN_PASSWORD", "").strip()
        if not password:
            password = secrets.token_urlsafe(18)
            notice = _path(".initial_password")
            with open(notice, "w", encoding="utf-8") as fh:
                fh.write(username + "\n" + password + "\n")
            os.chmod(notice, 0o600)
            print("WORKSTATION: initial admin password written to %s" % notice, flush=True)
        _save_users({"users": [{
            "username": username,
            "password": hash_password(password),
            "role": "admin",
            "createdAt": int(time.time()),
            "disabled": False,
        }]})
        return username


def find_user(username):
    for user in load_users().get("users", []):
        if user.get("username") == username:
            return user
    return None


def authenticate(username, password):
    user = find_user(str(username or "").strip().lower())
    if not user or user.get("disabled"):
        return None
    if not verify_password(password or "", user.get("password", "")):
        return None
    return {"username": user["username"], "role": user.get("role", "viewer")}


def require_role(user, *roles):
    if not user:
        raise PermissionError("Sign in required")
    if user.get("role") not in roles:
        raise PermissionError("This action needs %s access" % " or ".join(roles))


def validate_username(value):
    name = str(value or "").strip().lower()
    if not (3 <= len(name) <= 120) or "@" not in name or "." not in name.split("@")[-1]:
        raise ValueError("Username must be an email address")
    return name


def validate_password(value):
    value = str(value or "")
    if len(value) < 12:
        raise ValueError("Password must be at least 12 characters")
    return value


def list_users():
    return [{
        "username": u.get("username"),
        "role": u.get("role", "viewer"),
        "disabled": bool(u.get("disabled")),
        "createdAt": u.get("createdAt"),
    } for u in load_users().get("users", [])]


def add_user(username, password, role):
    name = validate_username(username)
    secret = validate_password(password)
    if role not in ROLES:
        raise ValueError("Role must be admin, operator or viewer")
    with _lock:
        store = load_users()
        if any(u.get("username") == name for u in store["users"]):
            raise ValueError("That user already exists")
        store["users"].append({
            "username": name, "password": hash_password(secret), "role": role,
            "createdAt": int(time.time()), "disabled": False,
        })
        _save_users(store)
    return name


def update_user(username, changes):
    name = validate_username(username)
    with _lock:
        store = load_users()
        user = next((u for u in store["users"] if u.get("username") == name), None)
        if not user:
            raise ValueError("User not found")
        if "role" in changes:
            if changes["role"] not in ROLES:
                raise ValueError("Role must be admin, operator or viewer")
            if (user["role"] == "admin" and changes["role"] != "admin"
                    and sum(1 for u in store["users"] if u.get("role") == "admin") == 1):
                raise ValueError("Cannot demote the last admin")
            user["role"] = changes["role"]
        if "disabled" in changes:
            if user.get("role") == "admin" and changes["disabled"]:
                raise ValueError("Cannot disable an admin")
            user["disabled"] = bool(changes["disabled"])
        if changes.get("password"):
            user["password"] = hash_password(validate_password(changes["password"]))
        _save_users(store)
    return name


def delete_user(username):
    name = validate_username(username)
    with _lock:
        store = load_users()
        user = next((u for u in store["users"] if u.get("username") == name), None)
        if not user:
            raise ValueError("User not found")
        if user.get("role") == "admin":
            raise ValueError("Admins cannot be deleted")
        store["users"] = [u for u in store["users"] if u.get("username") != name]
        _save_users(store)
    return name


def change_password(username, current, new):
    name = validate_username(username)
    user = find_user(name)
    if not user or not verify_password(current or "", user.get("password", "")):
        raise ValueError("Current password is incorrect")
    update_user(name, {"password": new})
    return True


# ------------------------------------------------------------- agent config
DEFAULT_AGENT = {
    "provider": "openrouter",       # openrouter | ollama
    "model": "",
    "openrouter_key": "",
    "ollama_url": "http://127.0.0.1:11434",
    "temperature": 0.2,
    "system_prompt": "You are the MerQato Workstation assistant. Be concise and practical.",
    "enabled": False,
}


def load_agent_config():
    path = _path("agent.json")
    cfg = dict(DEFAULT_AGENT)
    if os.path.exists(path):
        try:
            with open(path, "r", encoding="utf-8") as fh:
                cfg.update(json.load(fh))
        except Exception:
            pass
    return cfg


def save_agent_config(changes):
    with _lock:
        cfg = load_agent_config()
        for key in DEFAULT_AGENT:
            if key in changes:
                cfg[key] = changes[key]
        if cfg["provider"] not in ("openrouter", "ollama"):
            raise ValueError("Provider must be openrouter or ollama")
        if not str(cfg.get("ollama_url", "")).startswith(("http://", "https://")):
            raise ValueError("Ollama URL must be http:// or https://")
        cfg["temperature"] = max(0.0, min(1.0, float(cfg.get("temperature", 0.2))))
        cfg["enabled"] = bool(cfg.get("enabled"))
        path = _path("agent.json")
        if os.path.exists(path):
            _backup(path, "agent")
        _atomic_write(path, cfg)
    return cfg


def public_config(cfg=None):
    cfg = cfg or load_agent_config()
    out = dict(cfg)
    key = out.get("openrouter_key", "")
    out["openrouter_key_set"] = bool(key)
    out["openrouter_key"] = (key[:6] + "…" + key[-4:]) if len(key) > 12 else ("***" if key else "")
    return out


# ------------------------------------------------------------- model catalog
def _get_json(url, headers=None, timeout=12):
    req = urllib.request.Request(url, headers=headers or {})
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _post_json(url, payload, headers=None, timeout=30):
    body = json.dumps(payload).encode("utf-8")
    headers = {"Content-Type": "application/json"}
    headers.update(headers or {})
    req = urllib.request.Request(url, data=body, headers=headers, method="POST")
    with urllib.request.urlopen(req, timeout=timeout) as resp:
        return json.loads(resp.read().decode("utf-8"))


def _is_free(model):
    pricing = model.get("pricing") or {}
    for key in ("prompt", "completion", "image", "audio", "request"):
        try:
            if float(pricing.get(key, 0) or 0) > 0:
                return False
        except (TypeError, ValueError):
            return False
    return True


def fetch_models(provider=None, refresh=False):
    """Free OpenRouter models or models on the configured Ollama machine."""
    cfg = load_agent_config()
    provider = provider or cfg["provider"]
    key = provider + ":" + cfg.get("ollama_url", "")
    now = time.time()
    if not refresh and key in _model_cache:
        stamp, models = _model_cache[key]
        if now - stamp < MODEL_CACHE_TTL:
            return {"provider": provider, "models": models, "cached": True}

    if provider == "openrouter":
        data = _get_json("https://openrouter.ai/api/v1/models")
        models = [{
            "id": m.get("id"),
            "name": m.get("name") or m.get("id"),
            "context": m.get("context_length"),
            "free": True,
        } for m in data.get("data", []) if _is_free(m) and m.get("id")]
        models.sort(key=lambda m: m["name"].lower())
    elif provider == "ollama":
        data = _get_json(cfg["ollama_url"].rstrip("/") + "/api/tags")
        models = [{
            "id": m.get("name"),
            "name": m.get("name"),
            "size": m.get("size"),
            "free": True,
        } for m in data.get("models", []) if m.get("name")]
        models.sort(key=lambda m: m["name"].lower())
    else:
        raise ValueError("Unknown provider: %s" % provider)

    _model_cache[key] = (now, models)
    return {"provider": provider, "models": models, "cached": False}


def chat(message):
    cfg = load_agent_config()
    if not cfg.get("enabled"):
        raise ValueError("Agent is not enabled in Settings")
    if not cfg.get("model"):
        raise ValueError("Choose a model in Settings first")
    messages = [
        {"role": "system", "content": cfg.get("system_prompt", "")},
        {"role": "user", "content": message},
    ]
    if cfg["provider"] == "openrouter":
        if not cfg.get("openrouter_key"):
            raise ValueError("Add an OpenRouter API key in Settings")
        data = _post_json(
            "https://openrouter.ai/api/v1/chat/completions",
            {"model": cfg["model"], "messages": messages,
             "temperature": cfg.get("temperature", 0.2)},
            {"Authorization": "Bearer " + cfg["openrouter_key"],
             "HTTP-Referer": "https://work.merqato.digital",
             "X-Title": "MerQato Workstation"},
        )
        return data["choices"][0]["message"]["content"]
    data = _post_json(
        cfg["ollama_url"].rstrip("/") + "/api/chat",
        {"model": cfg["model"], "messages": messages, "stream": False,
         "options": {"temperature": cfg.get("temperature", 0.2)}},
    )
    return data.get("message", {}).get("content", "")
