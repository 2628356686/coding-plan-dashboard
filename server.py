import hashlib
import hmac
import json
import os
import re
import shlex
import uuid
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import quote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen


def now_iso():
    return datetime.now(timezone.utc).isoformat()


def load_snapshot(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return {}


def save_snapshot(path, snapshot):
    target = Path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_suffix(target.suffix + ".tmp")
    temporary.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
    os.replace(temporary, target)


def load_results(path):
    value = load_snapshot(path)
    return value if isinstance(value, dict) else {}


def save_results(path, results):
    save_snapshot(path, results)


VOLC_OPENAPI_HOST = "open.volcengineapi.com"
VOLC_REGION = "cn-beijing"
VOLC_SERVICE = "ark"
VOLC_ACTIONS = {"volcAgent": "GetAgentPlanAFPUsage", "volcCoding": "GetCodingPlanUsage"}


def _volc_norm_query(params):
    query = ""
    for key in sorted(params.keys()):
        value = params[key]
        if isinstance(value, list):
            for item in value:
                query += quote(key, safe="-_.~") + "=" + quote(str(item), safe="-_.~") + "&"
        else:
            query += quote(key, safe="-_.~") + "=" + quote(str(value), safe="-_.~") + "&"
    return query[:-1].replace("+", "%20")


def _volc_hmac(key, content):
    if isinstance(key, str):
        key = key.encode("utf-8")
    return hmac.new(key, content.encode("utf-8"), hashlib.sha256).digest()


def volcengine_sign(ak, sk, method, path, query, body, region=VOLC_REGION, service=VOLC_SERVICE, content_type="application/json"):
    now = datetime.now(timezone.utc)
    x_date = now.strftime("%Y%m%dT%H%M%SZ")
    x_date_short = x_date[:8]
    if isinstance(body, str):
        body = body.encode("utf-8")
    body_hash = hashlib.sha256(body or b"").hexdigest()
    headers = {"Host": VOLC_OPENAPI_HOST, "X-Date": x_date, "X-Content-Sha256": body_hash, "Content-Type": content_type}
    signed_headers = {}
    for key, value in headers.items():
        if key in ("Content-Type", "Content-Md5", "Host") or key.startswith("X-"):
            signed_headers[key.lower()] = value
    signed_str = "".join(f"{key}:{signed_headers[key]}\n" for key in sorted(signed_headers.keys()))
    sh = ";".join(sorted(signed_headers.keys()))
    canonical_request = "\n".join([method, quote(path, safe="/-_.~").replace("%2F", "/").replace("+", "%20"), _volc_norm_query(query), signed_str, sh, body_hash])
    credential_scope = f"{x_date_short}/{region}/{service}/request"
    hashed_canonical = hashlib.sha256(canonical_request.encode("utf-8")).hexdigest()
    string_to_sign = "\n".join(["HMAC-SHA256", x_date, credential_scope, hashed_canonical])
    k_date = _volc_hmac(sk, x_date_short)
    k_region = _volc_hmac(k_date, region)
    k_service = _volc_hmac(k_region, service)
    k_signing = _volc_hmac(k_service, "request")
    signature = hmac.new(k_signing, string_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    headers["Authorization"] = f"HMAC-SHA256 Credential={ak}/{credential_scope}, SignedHeaders={sh}, Signature={signature}"
    return headers


def execute_volcengine_openapi(credentials, action):
    ak = credentials.get("accessKeyId", "")
    sk = credentials.get("secretAccessKey", "")
    if not ak or not sk:
        raise ValueError("missing volcengine credentials")
    query = {"Action": action, "Version": "2024-01-01"}
    body = b"{}"
    headers = volcengine_sign(ak, sk, "POST", "/", query, body)
    url = f"https://{VOLC_OPENAPI_HOST}/?{_volc_norm_query(query)}"
    request = Request(url, data=body, headers=headers, method="POST")
    try:
        with urlopen(request, timeout=35) as response:
            return response.status, response.read().decode("utf-8", errors="replace"), ""
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace"), f"HTTP {error.code}"
    except URLError as error:
        return 1, "", str(error.reason)


GOOGLE_OAUTH_URL = "https://oauth2.googleapis.com/token"
GOOGLE_UA = "antigravity-tools/1.1.5"
GOOGLE_BASES = [
    "https://cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.googleapis.com",
    "https://daily-cloudcode-pa.sandbox.googleapis.com",
]
def _load_google_clients():
    """Resolve Google AI OAuth client pairs.

    Reads the ``GOOGLE_AI_CLIENTS`` environment variable as a JSON array of
    ``[client_id, client_secret]`` pairs. When unset, falls back to three
    REDACTED placeholders so the module imports cleanly; the refresh call
    will then fail with an explicit error until the operator provides real
    values, keeping credentials out of source code.
    """
    raw = os.environ.get("GOOGLE_AI_CLIENTS", "").strip()
    if raw:
        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ValueError(
                "GOOGLE_AI_CLIENTS must be a JSON array of [client_id, client_secret] pairs: "
                f"{exc}"
            ) from exc
        pairs = []
        for item in parsed:
            if (
                isinstance(item, (list, tuple))
                and len(item) == 2
                and isinstance(item[0], str)
                and isinstance(item[1], str)
            ):
                pairs.append((item[0], item[1]))
        if not pairs:
            raise ValueError("GOOGLE_AI_CLIENTS did not contain any valid pairs")
        return pairs
    return [
        ("REDACTED_GOOGLE_CLIENT_ID_1", "REDACTED_GOOGLE_CLIENT_SECRET_1"),
        ("REDACTED_GOOGLE_CLIENT_ID_2", "REDACTED_GOOGLE_CLIENT_SECRET_2"),
        ("REDACTED_GOOGLE_CLIENT_ID_3", "REDACTED_GOOGLE_CLIENT_SECRET_3"),
    ]


GOOGLE_CLIENTS = _load_google_clients()
CREDENTIAL_SOURCES = set(VOLC_ACTIONS) | {"googleAi"}


def _google_opener(proxy):
    if proxy:
        return build_opener(ProxyHandler({"http": proxy, "https": proxy}))
    return None


def execute_google_ai(credentials):
    refresh_token = credentials.get("refreshToken", "")
    proxy = credentials.get("proxy") or os.environ.get("GOOGLE_AI_PROXY", "")
    if not refresh_token:
        raise ValueError("missing google ai refresh token")
    access_token = None
    last_error = None
    for client_id, client_secret in GOOGLE_CLIENTS:
        data = urlencode({
            "client_id": client_id,
            "client_secret": client_secret,
            "refresh_token": refresh_token,
            "grant_type": "refresh_token",
        }).encode("utf-8")
        request = Request(GOOGLE_OAUTH_URL, data=data, method="POST")
        try:
            opener = _google_opener(proxy)
            context = opener.open(request, timeout=35) if opener else urlopen(request, timeout=35)
            with context as response:
                access_token = json.loads(response.read().decode("utf-8", errors="replace")).get("access_token")
            if access_token:
                break
        except HTTPError as error:
            last_error = f"HTTP {error.code}: {error.read().decode('utf-8', errors='replace')[:120]}"
        except URLError as error:
            last_error = str(error.reason)
    if not access_token:
        return 1, "", f"oauth refresh failed: {last_error}"
    body = b"{}"
    last_error = None
    for base in GOOGLE_BASES:
        request = Request(
            f"{base}/v1internal:fetchAvailableModels",
            data=body,
            method="POST",
            headers={"Authorization": f"Bearer {access_token}", "Content-Type": "application/json", "User-Agent": GOOGLE_UA},
        )
        try:
            opener = _google_opener(proxy)
            context = opener.open(request, timeout=35) if opener else urlopen(request, timeout=35)
            with context as response:
                return response.status, response.read().decode("utf-8", errors="replace"), ""
        except HTTPError as error:
            last_error = f"HTTP {error.code} {error.reason} @ {base}"
        except URLError as error:
            last_error = f"{error.reason} @ {base}"
    return 1, "", f"fetchAvailableModels failed: {last_error}"


def mask_secret(secret):
    if not secret:
        return ""
    if len(secret) <= 8:
        return "****"
    return secret[:4] + "****" + secret[-4:]


def load_credentials(path):
    value = load_snapshot(path)
    return value if isinstance(value, dict) else {}


def save_credentials(path, credentials):
    save_snapshot(path, credentials)


def load_order(path):
    value = load_snapshot(path)
    if not isinstance(value, dict):
        return []
    order = value.get("order")
    return [str(item) for item in order if isinstance(item, (str, int))] if isinstance(order, list) else []


def save_order(path, order):
    save_snapshot(path, {"order": list(order)})


def is_success_status(status):
    return status == 200


def should_skip_source(source, accounts):
    sources = {acc.get("source", "") for acc in accounts.values() if isinstance(acc, dict)}
    skip_usage = "codexNewApi" in sources and source == "codexUsage"
    skip_credits = "codexNewApiCredits" in sources and source == "codexCredits"
    return skip_usage or skip_credits


ALLOWED_HOSTS = {
    "chatgpt.com",
    "www.minimaxi.com",
    "console.volcengine.com",
    "www.kimi.com",
    "longcat.chat",
    "cs-data.qianwenai.com",
}
# NewAPI gateway hosts allowed to serve /api/channel/{id}/codex/{usage,usage/reset-credits}.
# Configurable via NEWAPI_HOSTS env var (comma-separated hostnames). Defaults to the
# empty set; deployments that import NewAPI Codex curls must set this to the host
# portion of the gateway URL (e.g. NEWAPI_HOSTS=newapi.example.lan,newapi.other).
NEWAPI_HOSTS = {item.strip() for item in os.environ.get("NEWAPI_HOSTS", "").split(",") if item.strip()}
# Pattern matching any NewAPI Codex channel URL path: /api/channel/{digits}/codex/usage
# or /api/channel/{digits}/codex/usage/reset-credits. channelId is intentionally
# not pinned so any NewAPI channel with Codex usage is recognized.
NEWAPI_CODEX_USAGE_PATTERN = re.compile(r"^/api/channel/\d+/codex/usage(?:/reset-credits)?/?$")
NEWAPI_CODEX_USAGE_PATH = "codexNewApi"
NEWAPI_CODEX_CREDITS_PATH = "codexNewApiCredits"


def _newapi_request_allowed(parsed):
    """Return True if the URL targets a whitelisted NewAPI host with a known Codex path."""
    if parsed.hostname not in NEWAPI_HOSTS:
        return False
    if parsed.scheme not in ("http", "https"):
        return False
    return bool(NEWAPI_CODEX_USAGE_PATTERN.match(parsed.path))
ALLOWED_OPTIONS_WITH_VALUE = {
    "-H", "--header", "-b", "--cookie", "-d", "--data", "--data-raw", "--data-binary",
    "-X", "--request", "-A", "--user-agent", "--connect-timeout", "--max-time", "--url", "-x", "--proxy",
}
ALLOWED_OPTIONS = {"--compressed", "-s", "--silent", "-S", "--show-error", "-f", "--fail", "--fail-with-body", "-G", "--get", "-k", "--insecure"}
COMBINED_FLAGS = {"-sS", "-Ss", "-fsS", "-sSf"}


def parse_curl(command):
    normalized = command.replace("\\\n", " ").strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("curl command contains unsupported shell syntax")
    tokens = shlex.split(normalized)
    if not tokens or Path(tokens[0]).name != "curl":
        raise ValueError("command must start with curl")
    args = ["curl"]
    urls = []
    index = 1
    while index < len(tokens):
        token = tokens[index]
        if token in COMBINED_FLAGS:
            args.extend(["-s", "-S"])
            index += 1
            continue
        if token in ALLOWED_OPTIONS:
            args.append(token)
            index += 1
            continue
        if token in ALLOWED_OPTIONS_WITH_VALUE:
            if index + 1 >= len(tokens):
                raise ValueError(f"missing value for {token}")
            args.extend([token, tokens[index + 1]])
            index += 2
            continue
        if token.startswith("-"):
            raise ValueError(f"unsupported curl option: {token}")
        urls.append(token)
        index += 1
    if len(urls) != 1:
        raise ValueError("curl command must contain exactly one URL")
    parsed = urlparse(urls[0])
    if parsed.hostname in ALLOWED_HOSTS and parsed.scheme == "https":
        pass
    elif _newapi_request_allowed(parsed):
        pass
    else:
        raise ValueError("URL host is not allowed")
    args.append(urls[0])
    return urls[0], args


def infer_source(command):
    url, _ = parse_curl(command)
    parsed = urlparse(url)
    path = parsed.path
    if parsed.hostname == "chatgpt.com":
        if path.endswith("/rate-limit-reset-credits"):
            return "codexCredits"
        if path.endswith("/usage"):
            return "codexUsage"
        return "codex"
    if parsed.hostname in NEWAPI_HOSTS and NEWAPI_CODEX_USAGE_PATTERN.match(path):
        if path.endswith("/reset-credits"):
            return NEWAPI_CODEX_CREDITS_PATH
        return NEWAPI_CODEX_USAGE_PATH
    if parsed.hostname == "www.kimi.com" and path.endswith("GetSubscriptionStats"):
        return "kimi"
    if parsed.hostname == "longcat.chat" and path.endswith("token-packs/summary"):
        return "longcat"
    if parsed.hostname == "cs-data.qianwenai.com" and path.endswith("/data/api.json") and "tokenplan" in parsed.query:
        return "qianwen"
    if parsed.hostname == "www.minimaxi.com":
        return "minimax"
    if parsed.hostname == "console.volcengine.com" and path.endswith("GetCodingPlanUsage"):
        return "volcCoding"
    if parsed.hostname == "console.volcengine.com" and path.endswith(("GetAgentPlanUsageDetails", "GetAgentPlanAFPUsage")):
        return "volcAgent"
    raise ValueError("unable to infer source from URL")


def load_requests(path):
    value = load_snapshot(path)
    if not isinstance(value, dict):
        return {}
    migrated = {}
    needs_migration = False
    for key, definition in value.items():
        if not isinstance(definition, dict):
            continue
        if "source" not in definition and "curl" in definition:
            needs_migration = True
            account_id = f"acc_{uuid.uuid4().hex[:12]}"
            migrated[account_id] = {"source": key, "label": "", "curl": definition.get("curl", ""), "updatedAt": definition.get("updatedAt")}
        else:
            migrated[key] = definition
    if needs_migration:
        save_requests(path, migrated)
    return migrated


def save_requests(path, requests):
    save_snapshot(path, requests)


def build_http_request(args):
    url = args[-1]
    headers = {}
    body = None
    method = None
    use_get = False
    proxy = None
    index = 1
    while index < len(args) - 1:
        token = args[index]
        if token in {"-H", "--header"}:
            raw = args[index + 1]
            key, separator, value = raw.partition(":")
            if separator:
                headers[key.strip()] = value.strip()
            elif raw.strip().rstrip(";").strip():
                headers[raw.strip().rstrip(";").strip()] = ""
            else:
                raise ValueError("invalid header")
            index += 2
            continue
        if token in {"-b", "--cookie"}:
            headers["Cookie"] = args[index + 1]
            index += 2
            continue
        if token in {"-A", "--user-agent"}:
            headers["User-Agent"] = args[index + 1]
            index += 2
            continue
        if token in {"-x", "--proxy"}:
            proxy = args[index + 1]
            index += 2
            continue
        if token in {"-X", "--request"}:
            method = args[index + 1].upper()
            index += 2
            continue
        if token in {"-d", "--data", "--data-raw", "--data-binary"}:
            body = args[index + 1].encode("utf-8")
            index += 2
            continue
        if token in {"-G", "--get"}:
            use_get = True
        index += 1
    if use_get and body is not None:
        url += ("&" if "?" in url else "?") + body.decode("utf-8")
        body = None
        method = "GET"
    elif body is not None and method is None:
        method = "POST"
    return Request(url, data=body, headers=headers, method=method or "GET"), proxy


def execute_curl(command):
    _, args = parse_curl(command)
    request, proxy = build_http_request(args)
    try:
        opener = build_opener(ProxyHandler({"http": proxy, "https": proxy})) if proxy else None
        response_context = opener.open(request, timeout=35) if opener else urlopen(request, timeout=35)
        with response_context as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body, ""
    except HTTPError as error:
        return error.code, error.read().decode("utf-8", errors="replace"), f"HTTP {error.code}"
    except URLError as error:
        return 1, "", str(error.reason)


class DashboardHandler(SimpleHTTPRequestHandler):
    snapshot_path = Path(os.environ.get("SNAPSHOT_PATH", "/data/snapshot.json"))
    requests_path = Path(os.environ.get("REQUESTS_PATH", "/data/requests.json"))
    results_path = Path(os.environ.get("RESULTS_PATH", "/data/results.json"))
    credentials_path = Path(os.environ.get("CREDENTIALS_PATH", "/data/credentials.json"))
    order_path = Path(os.environ.get("ORDER_PATH", "/data/order.json"))

    def do_GET(self):
        if self.path == "/api/snapshot":
            self.send_json(load_snapshot(self.snapshot_path))
            return
        if self.path == "/api/requests":
            accounts = load_requests(self.requests_path)
            masked = {}
            for aid, acc in accounts.items():
                item = dict(acc)
                if "sk" in item:
                    item["sk"] = mask_secret(item["sk"])
                    item["hasCreds"] = True
                if "refreshToken" in item:
                    item["refreshToken"] = mask_secret(item["refreshToken"])
                    item["hasCreds"] = True
                masked[aid] = item
            self.send_json(masked)
            return
        if self.path == "/api/results":
            self.send_json(load_results(self.results_path))
            return
        if self.path == "/api/order":
            self.send_json({"order": load_order(self.order_path)})
            return
        if self.path == "/api/credentials":
            creds = load_credentials(self.credentials_path)
            masked = {}
            for source, cred in creds.items():
                if isinstance(cred, dict):
                    masked[source] = {"accessKeyId": cred.get("accessKeyId", ""), "secretAccessKey": mask_secret(cred.get("secretAccessKey", "")), "configured": bool(cred.get("accessKeyId") and cred.get("secretAccessKey"))}
            self.send_json(masked)
            return
        if self.path in ("", "/", "/index.html"):
            html_path = Path("/srv/index.html")
            if html_path.exists():
                body = html_path.read_bytes()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Cache-Control", "no-cache, no-store, must-revalidate")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)
                return
        super().do_GET()

    def do_POST(self):
        if self.path not in {"/api/snapshot", "/api/requests", "/api/refresh", "/api/credentials", "/api/order"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 524288:
                raise ValueError("invalid payload size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("snapshot must be an object")
            if self.path == "/api/snapshot":
                save_snapshot(self.snapshot_path, payload)
                self.send_json({"ok": True})
                return
            if self.path == "/api/requests":
                account_id = str(payload.get("id", "")).strip()
                curl = str(payload.get("curl", "")).strip()
                label = str(payload.get("label", "")).strip()
                ak = str(payload.get("ak", "")).strip()
                sk_input = str(payload.get("sk", "")).strip()
                refresh_token = str(payload.get("refreshToken", "")).strip()
                proxy_input = str(payload.get("proxy", "")).strip()
                explicit_source = str(payload.get("source", "")).strip()
                requests = load_requests(self.requests_path)
                existing = requests.get(account_id, {}) if account_id else {}
                if not curl and existing:
                    curl = existing.get("curl", "")
                if curl:
                    source = infer_source(curl)
                elif explicit_source in CREDENTIAL_SOURCES and not existing:
                    source = explicit_source
                    if source in VOLC_ACTIONS and not (ak and sk_input):
                        raise ValueError("ak and sk are required for volcengine accounts")
                    if source == "googleAi" and not refresh_token:
                        raise ValueError("refreshToken is required for google ai accounts")
                    curl = ""
                elif existing.get("source") in CREDENTIAL_SOURCES:
                    source = existing["source"]
                    curl = ""
                else:
                    raise ValueError("curl is required")
                account_id = account_id or f"acc_{uuid.uuid4().hex[:12]}"
                definition = {"source": source, "label": label, "curl": curl, "updatedAt": payload.get("updatedAt")}
                if source in VOLC_ACTIONS:
                    definition["ak"] = ak or existing.get("ak", "")
                    definition["sk"] = sk_input or existing.get("sk", "")
                if source == "googleAi":
                    definition["refreshToken"] = refresh_token or existing.get("refreshToken", "")
                    definition["proxy"] = proxy_input or existing.get("proxy", "")
                requests[account_id] = definition
                save_requests(self.requests_path, requests)
                self.send_json({"ok": True, "id": account_id, "source": source})
                return
            if self.path == "/api/credentials":
                source = str(payload.get("source", "volc")).strip()
                ak = str(payload.get("accessKeyId", "")).strip()
                sk = str(payload.get("secretAccessKey", "")).strip()
                creds = load_credentials(self.credentials_path)
                if not ak and not sk:
                    creds.pop(source, None)
                else:
                    creds[source] = {"accessKeyId": ak, "secretAccessKey": sk}
                save_credentials(self.credentials_path, creds)
                self.send_json({"ok": True, "source": source})
                return
            if self.path == "/api/order":
                order = payload.get("order")
                if not isinstance(order, list):
                    raise ValueError("order must be an array")
                cleaned = [str(item).strip() for item in order if isinstance(item, (str, int)) and str(item).strip()]
                save_order(self.order_path, cleaned)
                self.send_json({"ok": True, "order": cleaned})
                return
            results = {}
            cached_results = load_results(self.results_path)
            credentials = load_credentials(self.credentials_path)
            accounts = load_requests(self.requests_path)
            for account_id, definition in accounts.items():
                source = definition.get("source", "")
                if should_skip_source(source, accounts):
                    continue
                try:
                    if source in VOLC_ACTIONS:
                        ak = definition.get("ak") or credentials.get("volc", {}).get("accessKeyId", "")
                        sk = definition.get("sk") or credentials.get("volc", {}).get("secretAccessKey", "")
                        if ak and sk:
                            code, stdout, stderr = execute_volcengine_openapi({"accessKeyId": ak, "secretAccessKey": sk}, VOLC_ACTIONS[source])
                        else:
                            code, stdout, stderr = execute_curl(definition.get("curl", ""))
                    elif source == "googleAi":
                        refresh_token = definition.get("refreshToken") or credentials.get("googleAi", {}).get("refreshToken", "")
                        proxy = definition.get("proxy") or os.environ.get("GOOGLE_AI_PROXY", "")
                        if refresh_token:
                            code, stdout, stderr = execute_google_ai({"refreshToken": refresh_token, "proxy": proxy})
                        else:
                            code, stdout, stderr = execute_curl(definition.get("curl", ""))
                    else:
                        code, stdout, stderr = execute_curl(definition.get("curl", ""))
                    updated_at = now_iso() if is_success_status(code) else None
                    result = {"ok": is_success_status(code), "status": code, "body": stdout, "error": stderr, "updatedAt": updated_at}
                    results[account_id] = result
                    if is_success_status(code):
                        cached_results[account_id] = result
                except (ValueError, OSError, TimeoutError) as error:
                    results[account_id] = {"ok": False, "status": None, "body": "", "error": str(error)}
            save_results(self.results_path, cached_results)
            self.send_json(results)
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))

    def do_DELETE(self):
        if self.path != "/api/requests":
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 524288:
                raise ValueError("invalid payload size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            account_id = str(payload.get("id", "")).strip()
            if not account_id:
                raise ValueError("id is required")
            requests = load_requests(self.requests_path)
            requests.pop(account_id, None)
            save_requests(self.requests_path, requests)
            results = load_results(self.results_path)
            results.pop(account_id, None)
            save_results(self.results_path, results)
            self.send_json({"ok": True, "id": account_id})
        except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
            self.send_error(400, str(error))

    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.chdir("/srv")
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "80"))), DashboardHandler)
    server.serve_forever()
