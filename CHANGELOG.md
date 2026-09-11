# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
- Responses gateway tool round trips: function/custom/namespace definitions,
  client tool search, tool history and screenshot results, incremental argument
  events, and tool calls in JSON responses for Chat and Anthropic upstreams.
  Unsupported hosted/stateful features now return explicit request errors.
- Initial public release.
- Single-page dashboard for Codex, MiniMax, Volcengine AgentPlan /
  CodingPlan, Kimi Code, LongCat, Qianwen AI and Google AI (Gemini 3.5 Flash).
- Per-account labels, reorder, lock / unlock and delete.
- Volcengine AK/SK authentication via HMAC-SHA256 V4 (no curl required).
- Codex NewAPI endpoint support that auto-skips the matching official request.
- Import-redacted curl through a Python HTTPS re-issuer (no shell).
- Docker Compose + Portainer Stack deployment recipes.
- GitHub Actions CI covering `node --test` and `unittest discover`.
- Configurable service port in the gateway settings page: rebinding takes effect
  immediately, persists in `data/gateway.json` and wins over the `PORT` env var.
- SMS verification (接码) management page proxying 号码盾 (smsnex) and EOMSG:
  platform config with masked keys, project/channel lookup, rent, first/second
  code polling and release, persisted in `data/sms.json`.
- "去登录" button on the accounts page that opens the Volcengine login page in a
  private/incognito browser window on the server host.
- Persistent rotating server log: default `<project>/log/dashboard.log`
  (override with `LOG_PATH`; the Docker stack pins `/data/dashboard.log`),
  5 MB × 5 backups. Captures every HTTP request line plus gateway routing
  events — account selection, cooldowns with reasons, quota-like upstream
  errors, quota refresh failures and per-account 503 diagnostics — viewable
  on a new "运行日志" page or via `GET /api/logs[?lines=N|download=1]`.
  Handler exceptions and client disconnects are captured instead of vanishing
  with the container's stderr.

### Changed
- Deleting an account from the dashboard is now a soft delete: the card is
  removed and the account's API key is cleared, but the record (phone, username,
  password, accountId, cookie expiry) stays on the accounts page, marked
  "已移出仪表盘" with a restore button. Refreshes skip hidden accounts; full
  deletion remains available from the accounts page.
- Account updates no longer wipe metadata fields (phone / username / password /
  accountId / apiKey) that the request omits — previously editing a label from
  the dashboard silently cleared the stored API key.

### Fixed
- Dashboard quotas now auto-refresh every 60 seconds and immediately when the
  tab becomes visible again, instead of requiring a manual refresh; overlapping
  refresh requests are skipped.
- Gateway toggle is rejected by the server and disabled in the accounts table
  for accounts without an API key; turning it off stays allowed.
