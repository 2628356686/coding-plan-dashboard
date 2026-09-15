# Project Instructions

## Scope

- Keep the browser application as a single `index.html` file.
- Keep credentials out of source code, tests, README, and commits.
- Do not add real curl commands containing Token, Cookie, Digest, or proxy credentials to fixtures.

## Windows Computer Use

- For Windows desktop control, read the installed `computer-use:computer-use` skill and use `mcp__node_repl__js` to import `@oai/sky`. Discover the tool if it is not yet exposed.
- `mcp__cua_repl__js` is the browser runtime, not the desktop `node_repl` runtime. Do not import `@oai/sky` there or infer that Windows control is unavailable from its browser-only API.
- If `Trusted RPC service is not configured: sky` occurs in `cua_repl`, retry initialization in the correct `node_repl` tool before diagnosing the desktop service as unavailable.

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
