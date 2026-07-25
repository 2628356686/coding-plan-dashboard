---
name: Bug report
about: Report a bug in the dashboard
title: "[Bug] "
labels: bug
assignees: ''
---

**Describe the bug**
A clear and concise description of what the bug is.

**To reproduce**
Steps to reproduce the behaviour, with a redacted `curl` (remove any Token /
Cookie / Digest / proxy credentials before pasting).

**Expected behaviour**
What you expected to happen.

**Environment**

- Dashboard revision (`git rev-parse HEAD`):
- Deployment method (Docker Compose / Portainer Stack):
- Server OS:

**Logs / screenshots**
Paste `docker logs --tail=200 coding-plan-quota-dashboard` here, redacting
sensitive headers.
