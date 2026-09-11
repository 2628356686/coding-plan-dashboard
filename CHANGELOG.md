# Changelog

All notable changes to this project are documented here. The format follows
[Keep a Changelog](https://keepachangelog.com/en/1.1.0/) and the project
adheres to [Semantic Versioning](https://semver.org/).

## [Unreleased]

### Added
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

### Fixed
- Dashboard quotas now auto-refresh every 60 seconds and immediately when the
  tab becomes visible again, instead of requiring a manual refresh; overlapping
  refresh requests are skipped.
- Gateway toggle is rejected by the server and disabled in the accounts table
  for accounts without an API key; turning it off stays allowed.
