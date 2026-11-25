# MerQato Workstation — Operations

Canonical URL: **https://work.merqato.digital** (single Basic Auth login)
`home.merqato.digital` and `dash.merqato.digital` 301-redirect to `work.merqato.digital`.
There is only one dashboard; the legacy Homepage on port 3003 is stopped.

## Access control

One entry point: `https://work.merqato.digital`. Caddy no longer has `basic_auth`;
the app handles login in `app/admin.py` with PBKDF2 password hashes and a signed
session. The first admin (`david@merqato.digital`) is seeded from
`WS_ADMIN_USER` / `WS_ADMIN_PASSWORD` in `/opt/homepage/.env`. Cloudflare Access
was deleted. Do not recreate the Access app, re-enable the legacy Homepage, or
add a second host/UI.

Settings → Account changes your own password; Settings → Users & roles adds,
reassigns and removes accounts with `admin`, `operator` or `viewer` roles.
Settings → Agent engine selects OpenRouter (free-tier models only, synced from
`/api/v1/models`) or Ollama (models synced from the configured machine's
`/api/tags`); Refresh models bypasses the 10-minute cache. The Agents view has
a live test chat. No model runs until the agent is enabled and a model chosen.

Rotate the password in two places when the permanent scheme is decided:
Workstation (Settings → Account) and the `WS_ADMIN_PASSWORD` seed value in
`/opt/homepage/.env`. Uptime Kuma and both `status.` hostnames were retired on
21 Nov 2026 — Workstation is now the single status page.

## Services (docker compose, /opt/homepage/docker-compose.yml)

| Container | Port | Purpose |
| --- | --- | --- |
| `workstation` | 3005 | Workstation UI + Quick Links Manager (`/opt/homepage/app/server.py`) |
| `homepage` | 3003 | Legacy Homepage dashboard kept as the `work.` fallback |
| `hubapi` | 3010 | Aggregation API (python stdlib, `/opt/homepage/hub/hub.py`) |
| `dockerproxy` | 2375 (internal) | Read-only Docker API (tecnativa/docker-socket-proxy) |
| `hub-files` | 3004 | Static downloads, e.g. `/files/workstation-hub.zip` |

`social-caddy-1` (in `/opt/social`) terminates TLS and routes:

```
work.merqato.digital   (Caddy basic_auth; Cloudflare passes through)
  /files/*  -> host.docker.internal:3004
  /hub/*    -> host.docker.internal:3010
  *         -> host.docker.internal:3005

home.merqato.digital -> 301 https://work.merqato.digital{uri}
dash.merqato.digital -> 301 https://work.merqato.digital{uri}
```

The Caddyfile is bind-mounted from `/opt/social/Caddyfile`. Edit it in place
with a text editor or Python — `sed -i` replaces the inode and the container
keeps serving the old file. After any edit:

```
docker exec social-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```

## Hub API endpoints

| Path | Contents |
| --- | --- |
| `/health` | liveness |
| `/summary.json` | every tile's numbers (used by Homepage customapi widgets) |
| `/domains.json` | per-host HTTPS probe results + certificate days |
| `/cloudflare.json` | zones, DNS record count |
| `/docker.json` | container fleet counts |
| `/github.json` | per-account public repo counts |
| `/coolify.json` | Coolify projects/apps/servers/version |
| `/vps.json` | UpCloud instance metadata + live host resources |
| `/vercel.json` | Vercel projects, deployment states, and error details |
| `/resend.json` | Resend domains and the latest delivery outcomes |
| `/inventory.json` | Project-inventory summary and normalized project rows |
| `/projects.json` | Normalized project rows used by the Workstation Projects view |
| `/full.json` | everything |

Responses are cached 60s and refreshed in a background thread, so a request
never waits on a full rebuild. The first build also runs at container start, so
the first visitor already gets a warm cache (sub-millisecond responses). No
secret is ever returned; the Cloudflare and Coolify tokens live only in
`/opt/homepage/.env` (chmod 600).

## Data sources

| Integration | How it gets data | Credential |
| --- | --- | --- |
| Cloudflare | REST API, DNS-scoped token | `/opt/homepage/.env` → `CF_API_TOKEN` |
| Docker | read-only socket proxy (`CONTAINERS`,`INFO`,`VERSION` only) | none |
| Site scan | cron → `app/scan.py` reads `/hub/domains.json`, writes `data/scans.json` | none |
| GitHub | REST API, `palawan-collective` org; token adds private-repo and inventory access | `GITHUB_TOKEN` when private inventory access is required |
| Vercel | REST API, latest 20 deployments | `/opt/homepage/.env` → `VERCEL_TOKEN` |
| Resend | REST API, latest 20 delivery records | `/opt/homepage/.env` → `RESEND_API_KEY` |
| Project inventory | `inventory.json` in the `palawan-collective/projects` GitHub repository (repo not yet recreated) | `GITHUB_TOKEN` |
| Coolify | REST API v1, read-only PAT | `COOLIFY_TOKEN` (token file `/opt/homepage/.coolify-token`) |
| UpCloud | instance metadata `169.254.169.254` + `/proc`, written by cron | none |

Coolify token abilities are `["read"]`; a `POST /api/v1/projects` returns 403.
The token is scoped to team `0` (Root Team), which is where the projects and
servers live — teams 1 and 2 are empty.

`COOLIFY_URL` must point at the local API, not the public hostname:
`http://host.docker.internal:8000`. `coolify.merqato.digital` sits behind
Cloudflare Access, so any request from the hub gets the Access sign-in HTML
instead of JSON (this is what put a 69 KB HTML page in the Coolify tile). Both
`hubapi` and `workstation` carry an `extra_hosts: host.docker.internal:host-gateway`
entry for this. `hub.py`'s `clean_text()` now rejects HTML/oversized values, so a
repeat shows as `ok: false` with an error instead of raw markup on the dashboard.

The hub alert webhook requires `ALERT_KEY` and fails closed when the key is
missing or incorrect. Keep `ALERT_KEY` in `/opt/homepage/.env` and never in the
repository. Any sender must use the `/hub/alert` endpoint with a custom
`X-Hub-Key` header matching `ALERT_KEY`; a missing or incorrect header returns
403 before the request body is read. With Uptime Kuma retired there is no
configured sender today.

## Exposure boundary

Caddy terminates public traffic on ports 80/443. UFW permits only 22/80/443;
direct connections to the host-published 3004, 3005, and 3010 ports timed out
from outside the VPS on 20 Nov 2026. Keep those ports off the public firewall.

The Workstation application's signed session is an additional CSRF/session
layer, not a replacement for the edge Basic Auth boundary. Do not publish port
3005 directly. Any firewall, Compose port-binding, or Caddy change requires
owner approval and a rollback snapshot.

VPS metrics are produced by `/opt/homepage/vps-agent.sh` every minute into
`/opt/homepage/hub/vps.json` (root crontab). It reads the UpCloud metadata
service, `/proc/meminfo`, `/proc/loadavg` and `df`. No API key required.

## Known failing endpoints

`Site Health` probes every public hostname in the Cloudflare zone (20s timeout,
follows redirects). As of 21 Nov 2026, **19 up / 0 failing — ALL CLEAR**.

Fixed on 21 Nov 2026:
- `www.palawancollective.com` — Cloudflare Single Redirect rule sends `www.*`
  to the apex, where the site works. No Vercel account changes.
- `susi.merqato.digital` — SUSIBooks restored from the private
  `palawan-collective/palawan-books` repo (`bir-compliance` branch) at
  `/opt/susibooks`. The GHCR images were unavailable to the deployment token,
  so both images were built locally under a 4 GB memory cap. The repo's HEAD has
  a JSX syntax error in `app/(dashboard)/payroll/employees/page.tsx` (two root
  elements in one return); a local fragment wrapper was added to build it. No
  GitHub changes were pushed. Fresh Postgres + MinIO volumes mean **no old
  accounting data was restored** — create a new account/sign in at
  `https://susi.merqato.digital/sign-up`.

Retired on 21 Nov 2026, with their Cloudflare DNS records deleted and Caddy
routes removed: both `status.merqato.digital` hostnames (plus the Uptime Kuma
stack), `command.merqato.digital` (freed for later use), and both
`sanvic.palawancollective.com` hostnames (freed for later use). No GitHub or
Vercel accounts were touched.

A response at or above 80% of the 20s timeout is reported as *slow* rather than
*down*. These are monitored HTTPS endpoints, not DNS-only health checks.

## Automation agent identity

| Item | Value |
| --- | --- |
| User | `deploy` (key-only, password locked) |
| Key | `~/.ssh/merqato_deploy` on the operator machine |
| Allowed | read-only docker/systemctl/journalctl, `/opt/homepage` compose stack |
| Denied | everything else, incl. `docker run`, other compose projects |
| Audit log | `/var/log/agent-actions.log` (root:deploy 660) |

Use the logging wrapper for anything that should be recorded:

```
sudo /usr/local/sbin/agentcmd /usr/bin/docker ps
```

Root access remains with the owner's key `~/.ssh/merqato_vps`.

## Rollback

The nightly `/opt/backups/backup.sh` also archives `/opt/homepage/data`
(links, users/roles, agent config and the session secret) to
`workstation_<stamp>.tar.gz` in R2, with 7-day retention. It ran successfully on
21 Nov 2026 with HTTP 200. Individual changes are still snapshotted in
`/opt/homepage/backups/`:

| File pattern | Restores |
| --- | --- |
| `Caddyfile.<stamp>` | edge routing |
| `services.yaml.<stamp>` | dashboard tiles |
| `settings.yaml.<stamp>` | layout |
| `hub.py.<stamp>` | hub API logic |
| `server.py.<stamp>` | Workstation API and link validation |
| `app.js.<stamp>` | Workstation front end |
| `docker-compose.yml.<stamp>` | container topology |
| `env.<stamp>` | integration secrets |

Example — restore the Caddyfile:

```
cp /opt/homepage/backups/Caddyfile.<stamp> /opt/social/Caddyfile
docker exec social-caddy-1 caddy reload --config /etc/caddy/Caddyfile
```

Example — restore the hub:

```
cp /opt/homepage/backups/hub.py.<stamp> /opt/homepage/hub/hub.py
cd /opt/homepage && docker compose restart hubapi
```

## Verification

```
curl -s -o /dev/null -w '%{http_code}\n' https://work.merqato.digital/api/health            # 200
curl -s -o /dev/null -w '%{http_code}\n' https://home.merqato.digital/                      # 301 -> work.
curl -s -o /dev/null -w '%{http_code}\n' https://dash.merqato.digital/                     # 301 -> work.
curl -s -o /dev/null -w '%{http_code}\n' https://work.merqato.digital/api/session           # ok:false
curl -s -o /dev/null -w '%{http_code}\n' https://work.merqato.digital/hub/summary.json      # 200
```

Scheduled scan (manual run and verification):

```
WS_DATA_DIR=/opt/homepage/data HUB_URL=http://127.0.0.1:3010 \
  /usr/bin/python3 /opt/homepage/app/scan.py
cat /opt/homepage/data/scans.json | head -20
```

Scans run at 16:00, 19:00, 21:00, 01:00, 04:00, 07:00, 10:00 and 13:00 UTC —
12AM, 3AM, 5AM, 9AM, 12PM, 3PM, 6PM and 9PM Manila time.