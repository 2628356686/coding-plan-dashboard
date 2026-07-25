# Security Policy

## Supported Versions

| Version | Supported          |
|---------|--------------------|
| main    | ✅ actively maintained |

Only the latest commit on `main` receives security fixes; older revisions
should be re-deployed from a fresh checkout.

## Reporting a Vulnerability

Please **do not** open a public GitHub issue for security problems. Instead
email the maintainer privately (see the GitHub profile) with:

1. A short description of the issue and its impact.
2. Reproduction steps (redact any real `curl`, Token, Cookie, AK/SK before
   sharing).
3. The commit SHA you reproduced against.

You can expect an acknowledgement within 72 hours and a fix or mitigation
plan within 14 days for critical issues.

## Credential Handling

This project is designed to store imported `curl` and Volcengine AK/SK in
plaintext inside `data/requests.json` and `data/credentials.json`. The data
directory is host-mounted and **never** included in the Docker image.

- Never commit `data/` or any file that contains real credentials.
- Never paste a real curl into a GitHub issue or pull request.
- Backups produced by `tar -czf ... data` must be treated as sensitive
  artifacts — encrypt them and store them off-site.
- Restrict the listening port (`8080` by default) to a trusted LAN only.
- Rotate credentials immediately if you suspect a leak.
