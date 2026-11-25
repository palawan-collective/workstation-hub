# MerQato Workstation Hub

A single operational dashboard for a small cloud stack: a purpose-built UI with a
Quick Links Manager, a small read-only aggregation API for services that have no
native dashboard, and a restricted Docker proxy so the UI can see container health
without being able to change anything.

**No secrets reach the browser.** Tokens live in a root-owned `.env`, are mounted
into the API container only, and every response is measured data.

## Layout

```text
app/server.py            UI + Quick Links API (Python stdlib, no framework)
app/static/              index.html, app.css, app.js, vendored Inter + Lucide
hub/hub.py               aggregation API (Cloudflare, GitHub, Vercel, Coolify,
                         Resend, UpCloud, Docker, project inventory)
hub/vps-agent.sh         writes vps.json every minute from the host
nginx.conf               static-download container configuration
config/                  legacy Homepage config (kept for the fallback host)
tests/                   Python and JavaScript regression tests
WORKSTATION.md           operations, rollback, verification
```

## The host

| Host | Serves |
| --- | --- |
| `work.merqato.digital` | the Workstation app (`app/`, port 3005) — the only dashboard |

`home.merqato.digital` and `dash.merqato.digital` permanently redirect to
`work.merqato.digital`. The legacy Homepage container (port 3003) is stopped;
do not re-enable it or split the dashboard across hosts again.

## Quick Links Manager

Add, edit, delete, pin, drag-reorder, instant search, category filters, grid or
list layout, and JSON export/import. Links live server-side in `data/links.json`
with an atomic write and a timestamped backup on every change. The same card
controls are available in Infrastructure, Projects and Clients, each with its
own Add button preset to that category. Export/import preserves link ids.

Writes require `admin` or `operator`; `viewer` is read-only. Role changes and
user removal work with full email usernames (the API decodes URL-escaped paths).
The link editor also sets the Lucide icon name.

Writes need a signed session cookie plus a same-origin check; URLs must be plain
HTTP(S) with no embedded credentials. Saved link names remain stable when Lucide
does not provide a matching brand icon; the UI resolves them to supported icons
without rewriting the saved data.

## What it shows

| Card | Source | Data |
| --- | --- | --- |
| VPS | UpCloud metadata + `/proc` | CPU / memory / disk / load / uptime |
| Containers | Docker socket proxy (read-only) | running, healthy, total |
| Sites | hub HTTPS probes | complete up/down lists, latency, certificate summary |
| GitHub | REST API (optional token) | public + private repo counts |
| Vercel | REST API | projects, ready states, deployment errors with dates |
| Coolify | REST API v1 (read-only PAT) | apps, version, servers |
| Cloudflare | REST API (DNS-scoped token) | zones, DNS records, TLS days |
| Resend | REST API | delivered, bounced, verified domains |
| Projects | GitHub inventory snapshot | normalized rows with live URL, repo, state |

## Production layout

`docker-compose.yml` follows the live production layout under `/opt/homepage`:

| Service | Port | Purpose |
| --- | --- | --- |
| `workstation` | 3005 | Workstation UI + Quick Links Manager |
| `hubapi` | 3010 | aggregation API |
| `dockerproxy` | internal | read-only Docker API |
| `homepage` | 3003 | legacy Homepage fallback |
| `files` | 3004 | static downloads |

The edge-routing example is in `Caddyfile.example`. The `work` host is the only
dashboard and routes to port 3005; `home` and `dash` redirect to it. Cloudflare
Access was removed. Login is handled by the app itself (`app/admin.py`): the
first admin is seeded from `WS_ADMIN_USER` / `WS_ADMIN_PASSWORD`, and Settings
provides password change, user management with roles (admin/operator/viewer),
and the Agent engine. It exposes `/hub/*` on port 3010 and `/files/*` on port 3004.

## Run and verify

The Compose file uses the production paths under `/opt/homepage`. For a local
checkout, adjust the bind mounts to that checkout before starting the stack.

```bash
cp .env.example .env       # fill in tokens; chmod 600
docker compose config --quiet
docker compose up -d
```

Run the local regression checks before deployment:

```bash
python -m unittest discover -s tests -v
node --check app/static/app.js
node tests/test_frontend.js
python -m py_compile app/server.py hub/hub.py
```

## Credentials

| Variable | Scope | Notes |
| --- | --- | --- |
| `CF_API_TOKEN` | Zone:Read, DNS:Read | certificate days come from a live TLS handshake |
| `COOLIFY_TOKEN` | Coolify PAT, ability `read` | must belong to the team that owns the resources |
| `GITHUB_TOKEN` | optional | needed for private repos and the project inventory |
| `GITHUB_ACCOUNTS` | none | comma-separated logins used for public counts |
| `VERCEL_TOKEN` | read-only REST access | project and deployment counts |
| `RESEND_API_KEY` | read-only REST access | domain and delivery-event counts |
| `ALERT_KEY` | webhook secret | required for `/alert`; the sender must include `X-Hub-Key` with this value |
| `INVENTORY_REPO` | GitHub repository | defaults to `palawan-collective/projects` |
| `CACHE_TTL` | none | seconds, default 60 |
| `PROBE_TIMEOUT` | none | HTTPS probe timeout in seconds, default 20 |

## Hub API

`/summary.json` backs the Workstation and legacy custom-API tiles. Also
available: `/domains.json`, `/cloudflare.json`, `/docker.json`, `/github.json`,
`/coolify.json`, `/vps.json`, `/vercel.json`, `/resend.json`, `/inventory.json`,
`/projects.json`, `/full.json`, and `/health`.

## Security notes

- `dockerproxy` exposes only `CONTAINERS`, `VERSION`, and `INFO`. `EXEC=0` and
  `POST=0` mean a compromised dashboard cannot start a container.
- The `/alert` webhook requires `ALERT_KEY` and fails closed when it is absent.
  Senders must supply the same value in the `X-Hub-Key` header; a missing or
  incorrect header returns 403 before the body is read. No sender is configured
  since Uptime Kuma was retired.
- Host ports 3004/3005/3010 must stay off the public firewall; Caddy and its
  edge authentication are the public boundary.
- The hub never returns a token, only counts and normalized rows derived from one.
- Automation uses a dedicated key-only account with a narrow sudo allowlist and
  an audit log, never a shared root credential.
