# Project Instructions

## Scope

- Keep the browser application as a single `index.html` file.
- Keep credentials out of source code, tests, README, and commits.
- Do not add real curl commands containing Token, Cookie, Digest, or proxy credentials to fixtures.

## Validation

- Run `node --test tests/parser.test.js` after parser changes.
- Run `PYTHONPATH=. python3 -m unittest discover -s tests -p 'test_*.py'` after server changes.
- Run `python3 -m py_compile server.py` and a JavaScript syntax check for `index.html` before pushing.

## Deployment

- Deploy through the Portainer Stack named `coding-plan-quota-dashboard`.
- Keep persistent runtime data under `/docker/coding_plan_quota_dashboard/data/`.
- Verify `http://<DEPLOY_HOST>:8080/` returns HTTP 200 after deployment (substitute your actual Docker host IP).
- Verify both local and remote commit SHAs are non-empty before declaring a push complete.

## Security

- Execute imported curl requests without a shell.
- Preserve the endpoint allowlist unless the user explicitly approves an additional provider.
- Treat `requests.json` as sensitive because it contains the imported raw curl text.
