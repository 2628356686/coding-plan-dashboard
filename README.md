# Coding Plan Dashboard

[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](./LICENSE)
[![CI](https://github.com/margrop/coding-plan-dashboard/actions/workflows/test.yml/badge.svg)](./.github/workflows/test.yml)

A single-page dashboard that aggregates the coding-plan quotas of multiple AI providers
(Codex, MiniMax, Volcengine AgentPlan / CodingPlan, Kimi Code, LongCat, Qianwen AI and
Google AI Gemini 3.5 Flash) into one self-hosted UI. Each provider supports multiple
accounts with independent labels.

一个部署在局域网中的 Coding Plan 配额看板，支持 Codex、MiniMax、火山方舟 CodingPlan /
AgentPlan、Kimi Code、LongCat、千问 AI 和 Google AI (Gemini 3.5 Flash)。支持同一平台
多账号，每个账号可添加备注。

## Features / 功能

- Auto-detects the data source from the imported raw `curl` URL — no manual selection
  needed. / 根据原始 `curl` URL 自动识别数据来源，不需要手工选择产品。
- The server re-executes saved requests on page open, manual refresh, or auto refresh;
  results are persisted as JSON on the host so they survive container restarts.
  / 页面打开、刷新或点击刷新按钮时由服务器重新执行已保存的请求；配额结果保存到服务器端
  JSON 文件，容器重启后自动恢复。
- First paint shows the cached snapshot then silently refreshes to the latest data;
  successful refreshes do not pop up any dialog.
- Multi-account support: each imported curl is stored as a separate account with its
  own ID, label, edit, delete (with double confirm), and reorder controls.
- Per-account Volcengine AK/SK configuration (no curl required); uses HMAC-SHA256 V4
  signing against `GetCodingPlanUsage` / `GetAgentPlanAFPUsage`.
- Codex supports both the official `/usage` + `/rate-limit-reset-credits` endpoints
  and a NewAPI replacement (`/api/channel/{channelId}/codex/usage[/reset-credits]`).
  The server merges them into a single Codex card and skips the official requests
  when a NewAPI variant is configured.
- All progress bars show one-decimal usage percentage; each card shows a live
  countdown to the next reset in the top-right corner.
- Self-contained single-file browser UI (`index.html`) with a custom SVG logo and no
  external favicon dependency.

## File Layout

```text
index.html          Single-file frontend
server.py           Static file serving + JSON persistence + restricted curl execution
parser.js           Parser implementation used by Node tests
docker-compose.yml  Container deployment configuration
tests/              Parser and server unit tests
AGENTS.md           Coding-agent instructions for this repository
```

## Quick Start (Docker)

### Requirements

- Linux host / NAS with Docker Engine and Docker Compose v2.
- The host must be able to reach Codex, MiniMax and Volcengine APIs.
- If you import a Codex curl with `--proxy`, the proxy must be reachable from the
  Docker host.
- Default listen port is `8080`; change `PORT` in `docker-compose.yml` if occupied.
- You can also change the listen port at runtime in **网关管理 → 网关设置 → 服务端口**.
  The dashboard and the gateway share this port; the change takes effect immediately
  (the UI redirects to the new port) and persists in `data/gateway.json`. A port saved
  there wins over the `PORT` environment variable; clear the field to follow `PORT` again.

### 1. Prepare the host directory

```bash
sudo mkdir -p /docker/coding_plan_quota_dashboard/data
sudo chmod 700 /docker/coding_plan_quota_dashboard/data
sudo cp index.html server.py docker-compose.yml /docker/coding_plan_quota_dashboard/
```

### 2. Start the stack

```bash
cd /docker/coding_plan_quota_dashboard
sudo docker compose up -d
sudo docker compose ps
sudo docker logs --tail=100 coding-plan-quota-dashboard
curl -fsS -o /dev/null -w 'HTTP %{http_code}\n' http://127.0.0.1:8080/
```

### 3. Portainer Stack

In Portainer choose the target endpoint, **Stacks → Add stack**, name it
`coding-plan-quota-dashboard`, paste the contents of `docker-compose.yml` and click
**Deploy the stack**. Because Portainer uses host bind mounts, `index.html`,
`server.py` and an empty `data/` directory must already exist on the host.

### Updating

```bash
sudo cp index.html /docker/coding_plan_quota_dashboard/index.html
sudo cp server.py /docker/coding_plan_quota_dashboard/server.py
sudo docker restart coding-plan-quota-dashboard
```

For Compose-level changes use Portainer's **Update the stack**, or:

```bash
cd /docker/coding_plan_quota_dashboard
sudo docker compose up -d --force-recreate
```

## Persistent Data & Backups

```text
/docker/coding_plan_quota_dashboard/data/requests.json    # imported curl, contains secrets
/docker/coding_plan_quota_dashboard/data/credentials.json # per-account Volcengine AK/SK
/docker/coding_plan_quota_dashboard/data/snapshot.json    # latest quota snapshot
/docker/coding_plan_quota_dashboard/data/results.json     # per-account cached API responses
```

Backup:

```bash
sudo tar -czf "coding_plan_quota_dashboard-data-$(date +%Y%m%d-%H%M%S).tar.gz" \
  -C /docker/coding_plan_quota_dashboard data
```

Restore:

```bash
cd /docker/coding_plan_quota_dashboard
sudo docker compose down
sudo tar -xzf coding_plan_quota_dashboard-data-YYYYMMDD-HHMMSS.tar.gz
sudo docker compose up -d
```

Treat `requests.json` and `credentials.json` as secrets: do **not** commit them, and
never publish the backup file to a public download location.

## Importing Requests

Paste the full raw `curl` in the page and click **保存 curl 并刷新**. The server
auto-classifies by URL:

- `chatgpt.com/backend-api/wham/usage` → Codex usage
- `chatgpt.com/backend-api/wham/rate-limit-reset-credits` → Codex reset credits
- `<newapi-host>/api/channel/{channelId}/codex/usage` → NewAPI Codex usage
- `<newapi-host>/api/channel/{channelId}/codex/usage/reset-credits` → NewAPI Codex reset credits
- `www.minimaxi.com` → MiniMax
- `GetCodingPlanUsage` → Volcengine CodingPlan
- `GetAgentPlanAFPUsage` → Volcengine AgentPlan (current endpoint)
- `GetAgentPlanUsageDetails` → Volcengine AgentPlan (legacy endpoint, still supported)
- `www.kimi.com/.../GetSubscriptionStats` → Kimi Code
- `longcat.chat/api/pay/quota/metering/token-packs/summary` → LongCat
- `cs-data.qianwenai.com/.../data/api.json` (contains `tokenplan`) → Qianwen AI TokenPlan
- `fetchAvailableModels` (Gemini 3.5 Flash) → Google AI (Antigravity)

Codex official and NewAPI endpoints can coexist; the server merges them into one
card. `curl -sS`, `--proxy` and `--insecure` flags are translated into Python HTTPS
requests — the server never spawns a shell to run curl.

If `/usage` returns `secondary_window: null`, the page keeps the 5-hour row visible
and shows "接口未返回" instead of fabricating usage.

## Security Notes

- The server does not shell out; imported curl is parsed and re-issued via Python
  `urllib`-style HTTPS requests.
- The host allowlist defaults to `chatgpt.com`, `www.minimaxi.com`,
  `console.volcengine.com`, `www.kimi.com`, `longcat.chat`,
  `cs-data.qianwenai.com`; additional NewAPI hosts can be enabled via the
  `NEWAPI_HOSTS` (comma-separated) environment variable and are accepted only for
  the Codex `/usage` and `/usage/reset-credits` paths.
- Google AI uses OAuth client pairs loaded from the `GOOGLE_AI_CLIENTS`
  environment variable as a JSON array of `[client_id, client_secret]` pairs.
  When unset, the server boots with three `REDACTED_*` placeholders and the
  Google AI refresh call will fail until you provide real values; this keeps
  credentials out of source code.
- NewAPI is reached over plain HTTP because it is a LAN service — do **not**
  extend this rule to arbitrary external HTTP endpoints.
- When a `codexNewApi` request is saved the refresh automatically skips the official
  `codexUsage` request, and likewise for `codexNewApiCredits` → `codexCredits`. This
  avoids repeated `401` noise when the official token is stale.
- Raw curl (Token / Cookie / Digest) is stored in `requests.json` in plaintext, and
  Volcengine AK/SK is stored in `credentials.json` in plaintext. Never commit real
  credentials; the README and tests use redacted samples only.
- Restrict `8080` to a trusted LAN and rotate credentials immediately on leak.

## Tests

```bash
node --test tests/parser.test.js
PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'
python3 -m py_compile server.py
```

CI runs the same commands on every push and pull request; see
[`.github/workflows/test.yml`](./.github/workflows/test.yml).

## License

[MIT](./LICENSE) — see [`LICENSE`](./LICENSE) for the full text.
