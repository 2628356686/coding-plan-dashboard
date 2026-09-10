import base64
import hashlib
import hmac
import json
import os
import re
import shlex
import time
import random
import uuid
import math
from contextlib import contextmanager, ExitStack
from threading import RLock, Thread, Event
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException
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
CREDENTIAL_SOURCES = {"googleAi"}


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
    # Normalize line continuations: bash-style (\\) and cmd-style (^), both CRLF and LF
    normalized = command
    for sep in ("\r\n", "\n"):
        normalized = normalized.replace("\\" + sep, " ").replace("^" + sep, " ")
    normalized = normalized.strip()
    if not normalized or "\n" in normalized or "\r" in normalized:
        raise ValueError("curl 命令包含未处理的换行，请确认复制的是完整的 cURL (bash) 或 cURL (cmd) 格式")
    tokens = shlex.split(normalized)
    if not tokens or Path(tokens[0]).name.lower() not in ("curl", "curl.exe"):
        raise ValueError("命令必须以 curl 开头，请确认复制的是 cURL 格式而非 PowerShell / fetch 格式")
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
            value = tokens[index + 1]
            args.extend([token, value])
            if token == "--url":
                urls.append(value)
            index += 2
            continue
        if token.startswith("-"):
            raise ValueError(f"unsupported curl option: {token}")
        urls.append(token)
        index += 1
    if len(urls) == 0:
        raise ValueError("未找到请求 URL，请确认复制的是完整的 cURL 命令（包含 --url 或直接跟在 curl 后的 URL）")
    if len(urls) > 1:
        raise ValueError(f"找到 {len(urls)} 个 URL，cURL 命令只能包含一个请求 URL")
    parsed = urlparse(urls[0])
    if parsed.hostname in ALLOWED_HOSTS and parsed.scheme == "https":
        pass
    elif _newapi_request_allowed(parsed):
        pass
    else:
        raise ValueError(f"不支持的请求域名：{parsed.hostname or '(空)'}，仅支持已配置的平台域名")
    args.append(urls[0])
    return urls[0], args



def extract_cookie_expiry(curl_command):
    """Extract JWT expiry (Unix timestamp) from curl cookie. Returns int or None."""
    try:
        _, args = parse_curl(curl_command)
    except Exception:
        return None
    cookie_str = None
    for i, arg in enumerate(args):
        if arg in ("-b", "--cookie") and i + 1 < len(args):
            cookie_str = args[i + 1]
            break
    if not cookie_str:
        return None
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        _, value = part.split("=", 1)
        value = value.strip()
        if not value.startswith("eyJ") or value.count(".") < 2:
            continue
        try:
            payload_b64 = value.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
            exp = payload.get("exp")
            if exp and isinstance(exp, (int, float)):
                return int(exp)
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None


def extract_account_id(curl_command):
    """Extract AccountID from curl cookie (AccountID= field or JWT sub/acc_i). Returns str or None."""
    try:
        _, args = parse_curl(curl_command)
    except Exception:
        return None
    cookie_str = None
    for i, arg in enumerate(args):
        if arg in ("-b", "--cookie") and i + 1 < len(args):
            cookie_str = args[i + 1]
            break
    if not cookie_str:
        return None
    # Direct cookie field: AccountID=xxx
    for part in cookie_str.split(";"):
        part = part.strip()
        if part.startswith("AccountID="):
            value = part[len("AccountID="):].strip()
            if value:
                return value
    # Fallback: JWT payload sub / acc_i
    for part in cookie_str.split(";"):
        part = part.strip()
        if "=" not in part:
            continue
        _, value = part.split("=", 1)
        value = value.strip()
        if not value.startswith("eyJ") or value.count(".") < 2:
            continue
        try:
            payload_b64 = value.split(".")[1]
            padding = 4 - len(payload_b64) % 4
            if padding != 4:
                payload_b64 += "=" * padding
            payload_json = base64.urlsafe_b64decode(payload_b64).decode("utf-8")
            payload = json.loads(payload_json)
            for key in ("sub", "acc_i"):
                if payload.get(key):
                    return str(payload[key])
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError):
            continue
    return None


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



# ============================================================================
# Gateway: multi-protocol reverse proxy with quota-weighted routing
# ============================================================================

GATEWAY_CONFIG_PATH = Path(os.environ.get("GATEWAY_CONFIG_PATH", "/data/gateway.json"))
GATEWAY_STATS_PATH = Path(os.environ.get("GATEWAY_STATS_PATH", "/data/gateway_stats.json"))
GATEWAY_COOLDOWN_SECONDS = 60

# Map account source to upstream plan category
SOURCE_TO_CATEGORY = {
    "volcAgent": "agentPlan",
    "volcCoding": "codingPlan",
}


def gateway_category_for_account(acc):
    """Infer upstream category from account source; default agentPlan."""
    source = acc.get("source", "") if isinstance(acc, dict) else ""
    return SOURCE_TO_CATEGORY.get(source, "agentPlan")

_gateway_cooldowns = {}  # account_id -> expiry epoch
_gateway_stats = {"total_requests": 0, "by_account": {}}
_gateway_active = {}
_gateway_active_lock = RLock()
_gateway_quota_cache = {}


class GatewayRetry(Exception):
    """Upstream failed before any downstream headers were sent."""


def normalize_gateway_quota(payload):
    """Normalize the two supported Volcengine gateway quota responses."""
    value = payload.get("Result", payload.get("data", payload))
    periods = []
    for name in ("AFPFiveHour", "AFPWeekly", "AFPMonthly"):
        item = value.get(name)
        if isinstance(item, dict) and item.get("Quota") is not None and item.get("Used") is not None:
            quota, used = float(item["Quota"]), float(item["Used"])
            periods.append({"usedPercent": used / quota * 100 if quota > 0 else 100})
    if not periods:
        for item in value.get("QuotaUsage", []):
            if isinstance(item, dict) and item.get("Percent") is not None:
                periods.append({"usedPercent": float(str(item["Percent"]).rstrip("%"))})
    if not periods:
        raise ValueError("quota response has no supported periods")
    if any(not math.isfinite(p["usedPercent"]) or p["usedPercent"] < 0 for p in periods):
        raise ValueError("quota response contains invalid percentages")
    return {"periods": periods, "updatedAt": now_iso()}


def refresh_gateway_quotas(accounts):
    for aid, account in accounts.items():
        if not isinstance(account, dict):
            continue
        if (account.get("source") not in SOURCE_TO_CATEGORY or not account.get("apiKey")
                or (account.get("gateway") or {}).get("enabled") is False or not account.get("curl")):
            continue
        try:
            status, body, _ = execute_curl(account["curl"])
            if not is_success_status(status):
                continue
            quota = normalize_gateway_quota(json.loads(body))
            with _gateway_active_lock:
                _gateway_quota_cache[aid] = quota
        except (ValueError, TypeError, AttributeError, OSError):
            # Keep the last known quota when credentials or the endpoint fail.
            continue


def gateway_routing_snapshot(snapshot):
    result = dict(snapshot)
    def updated(value):
        try:
            return datetime.fromisoformat(value.get("updatedAt", "").replace("Z", "+00:00")).timestamp()
        except (ValueError, TypeError, AttributeError):
            return 0
    with _gateway_active_lock:
        for aid, quota in _gateway_quota_cache.items():
            if updated(quota) >= updated(result.get(aid, {})):
                result[aid] = quota
    return result


def gateway_quota_worker(requests_path, stop):
    while not stop.is_set():
        try:
            if load_gateway_config().get("enabled"):
                refresh_gateway_quotas(load_requests(requests_path))
        except (ValueError, OSError):
            pass
        stop.wait(60)


@contextmanager
def reserve_gateway_account(accounts, snapshot, model, protocol, stream):
    # Selection and reservation share a lock so simultaneous requests see each other.
    with ExitStack() as stack:
        with _gateway_active_lock:
            aid, account = select_gateway_account(accounts, gateway_routing_snapshot(snapshot))
            if aid:
                stack.enter_context(track_gateway_request(aid, account, model, protocol, stream))
        yield aid, account


@contextmanager
def track_gateway_request(account_id, account, model, protocol, stream):
    request_id = uuid.uuid4().hex
    entry = {
        "id": request_id, "accountId": account_id,
        "accountLabel": account.get("label") or account.get("accountId") or account_id,
        "model": model, "protocol": protocol, "stream": bool(stream),
        "startedAt": now_iso(), "startedMonotonic": time.monotonic(),
    }
    with _gateway_active_lock:
        _gateway_active[request_id] = entry
    try:
        yield
    finally:
        with _gateway_active_lock:
            _gateway_active.pop(request_id, None)


def gateway_active_snapshot():
    with _gateway_active_lock:
        now = time.monotonic()
        active = [dict(
            {k: v for k, v in entry.items() if k != "startedMonotonic"},
            elapsedSeconds=round(max(0, now - entry["startedMonotonic"]), 1),
        ) for entry in _gateway_active.values()]
    return {"activeCount": len(active), "activeRequests": active}


def load_gateway_config():
    default = {
        "enabled": False,
        "apiKey": "",
        "defaultModel": "",
        "maxRetries": 3,
        "upstreams": {
            "agentPlan": {
                "anthropicBaseUrl": "https://ark.cn-beijing.volces.com/api/plan",
                "openaiBaseUrl": "https://ark.cn-beijing.volces.com/api/plan/v3",
            },
            "codingPlan": {
                "anthropicBaseUrl": "https://ark.cn-beijing.volces.com/api/coding",
                "openaiBaseUrl": "https://ark.cn-beijing.volces.com/api/coding/v3",
            },
        },
    }
    try:
        data = json.loads(GATEWAY_CONFIG_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            merged = dict(default)
            merged.update({k: v for k, v in data.items() if k != "upstreams"})
            upstreams = {k: dict(v) for k, v in default["upstreams"].items()}
            if isinstance(data.get("upstreams"), dict):
                for k, v in data["upstreams"].items():
                    if k in upstreams and isinstance(v, dict):
                        upstreams[k].update(v)
            merged["upstreams"] = upstreams
            return merged
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return default


def save_gateway_config(config):
    save_snapshot(str(GATEWAY_CONFIG_PATH), config)


def load_gateway_stats():
    try:
        data = json.loads(GATEWAY_STATS_PATH.read_text(encoding="utf-8"))
        if isinstance(data, dict):
            return data
    except (FileNotFoundError, json.JSONDecodeError):
        pass
    return {"total_requests": 0, "by_account": {}}


def save_gateway_stats(stats):
    save_snapshot(str(GATEWAY_STATS_PATH), stats)


def account_remaining_percent(account_id, snapshot):
    """Return remaining-quota percentage (0-100); 50 when no data."""
    data = snapshot.get(account_id)
    if not isinstance(data, dict):
        return 50.0
    used_percents = []
    for period in data.get("periods") or []:
        if isinstance(period, dict):
            p = period.get("usedPercent")
            if isinstance(p, (int, float)):
                used_percents.append(p)
    for window in data.get("windows") or []:
        if isinstance(window, dict):
            p = window.get("usedPercent")
            if isinstance(p, (int, float)):
                used_percents.append(p)
    p = data.get("usedPercent")
    if isinstance(p, (int, float)):
        used_percents.append(p)
    if used_percents:
        used = max(used_percents)
    elif (isinstance(data.get("used"), (int, float))
          and isinstance(data.get("limit"), (int, float))
          and data["limit"] > 0):
        used = data["used"] / data["limit"] * 100
    else:
        return 50.0
    return max(0.0, min(100.0, 100.0 - used))


def gateway_concurrency_limit(account):
    value = (account.get("gateway") or {}).get("maxConcurrency", 1)
    return value if type(value) is int and 1 <= value <= 10 else 1


def select_gateway_account(accounts, snapshot):
    """Weighted-random pick by remaining quota. Returns (aid, acc) or (None, None)."""
    now = time.time()
    candidates = []
    weights = []
    with _gateway_active_lock:
        active_counts = {}
        for entry in _gateway_active.values():
            aid = entry["accountId"]
            active_counts[aid] = active_counts.get(aid, 0) + 1
    for aid, acc in accounts.items():
        if not isinstance(acc, dict):
            continue
        gw = acc.get("gateway") or {}
        if gw.get("enabled") is False:
            continue
        if not str(acc.get("apiKey", "")).strip():
            continue
        if now < _gateway_cooldowns.get(aid, 0):
            continue
        if active_counts.get(aid, 0) >= gateway_concurrency_limit(acc):
            continue
        remaining = account_remaining_percent(aid, snapshot)
        if remaining <= 0:
            continue
        weight = (remaining / 100.0) * (gateway_concurrency_limit(acc) - active_counts.get(aid, 0))
        candidates.append(aid)
        weights.append(weight)
    if not candidates:
        return None, None
    chosen = random.choices(candidates, weights=weights, k=1)[0]
    return chosen, accounts[chosen]


def cooldown_account(account_id, seconds=GATEWAY_COOLDOWN_SECONDS):
    with _gateway_active_lock:
        _gateway_cooldowns[account_id] = time.time() + seconds


def record_gateway_request(account_id):
    with _gateway_active_lock:
        _gateway_stats["total_requests"] = _gateway_stats.get("total_requests", 0) + 1
        by_acc = _gateway_stats.setdefault("by_account", {})
        by_acc[account_id] = by_acc.get(account_id, 0) + 1


# ---- Protocol conversion (inbound -> OpenAI upstream) ----

def anthropic_image_to_openai_url(source):
    """Anthropic image source -> URL for OpenAI image_url parts."""
    source = source or {}
    if source.get("type") == "base64":
        media = str(source.get("media_type") or "image/png")
        data = source.get("data", "")
        if data:
            return "data:%s;base64,%s" % (media, data)
        return ""
    return str(source.get("url") or "")


def anthropic_to_openai(body):
    messages = []
    if body.get("system"):
        sys_content = body["system"]
        if isinstance(sys_content, list):
            sys_text = "".join(
                item.get("text", "") for item in sys_content if isinstance(item, dict)
            )
        else:
            sys_text = str(sys_content)
        messages.append({"role": "system", "content": sys_text})
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        if isinstance(content, list):
            parts = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    parts.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "image":
                    url = anthropic_image_to_openai_url(part.get("source"))
                    if url:
                        parts.append({"type": "image_url", "image_url": {"url": url}})
            if all(part.get("type") == "text" for part in parts):
                content = "".join(part.get("text", "") for part in parts)
            else:
                content = parts
        messages.append({"role": role, "content": content})
    result = {"messages": messages}
    for key in ("model", "max_tokens", "temperature", "top_p", "stream"):
        if body.get(key) is not None:
            result[key] = body[key]
    return result


def openai_to_anthropic(body, model):
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "") or ""
    return {
        "id": body.get("id", "msg_" + uuid.uuid4().hex[:16]),
        "type": "message",
        "role": "assistant",
        "model": model,
        "content": [{"type": "text", "text": content}],
        "stop_reason": "end_turn",
        "usage": {
            "input_tokens": body.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": body.get("usage", {}).get("completion_tokens", 0),
        },
    }


def responses_image_url(part):
    """Responses input_image part -> image URL string, or None."""
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    return str(url) if url else None


def responses_to_openai(body):
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": str(body["instructions"])})

    def content_to_openai(content):
        """Message content list -> OpenAI content (string, or list with images)."""
        if not isinstance(content, list):
            return content
        text_parts = []
        image_parts = []
        for c in content:
            if not isinstance(c, dict):
                continue
            if c.get("type") in ("input_text", "output_text"):
                text_parts.append(c.get("text", ""))
            elif c.get("type") == "input_image":
                url = responses_image_url(c)
                if url:
                    image_parts.append({"type": "image_url", "image_url": {"url": url}})
        if image_parts:
            return ([{"type": "text", "text": text} for text in text_parts if text] + image_parts)
        return "".join(text_parts)

    user_input = body.get("input", "")
    if isinstance(user_input, list):
        text_parts = []
        image_parts = []
        for item in user_input:
            if isinstance(item, dict):
                if item.get("type") == "message" or "role" in item:
                    messages.append({
                        "role": item.get("role", "user"),
                        "content": content_to_openai(item.get("content", [])),
                    })
                elif item.get("type") == "input_text":
                    text_parts.append(item.get("text", ""))
                elif item.get("type") == "input_image":
                    url = responses_image_url(item)
                    if url:
                        image_parts.append({"type": "image_url", "image_url": {"url": url}})
        if image_parts:
            user_input = [{"type": "text", "text": text} for text in text_parts if text] + image_parts
        else:
            user_input = "".join(text_parts)
    if user_input:
        messages.append({"role": "user", "content": user_input})
    result = {"messages": messages}
    for key in ("model", "temperature", "top_p", "stream"):
        if body.get(key) is not None:
            result[key] = body[key]
    if body.get("max_output_tokens"):
        result["max_tokens"] = body["max_output_tokens"]
    return result


def openai_to_responses(body, model):
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "") or ""
    return {
        "id": body.get("id", "resp_" + uuid.uuid4().hex[:16]),
        "object": "response",
        "model": model,
        "created_at": int(time.time()),
        "status": "completed",
        "output": [{
            "id": "msg_" + uuid.uuid4().hex,
            "type": "message",
            "status": "completed",
            "role": "assistant",
            "content": [{"type": "output_text", "text": content, "annotations": []}],
        }],
        "usage": {
            "input_tokens": body.get("usage", {}).get("prompt_tokens", 0),
            "output_tokens": body.get("usage", {}).get("completion_tokens", 0),
            "total_tokens": body.get("usage", {}).get("total_tokens", 0),
        },
    }


def iter_sse_data(stream):
    """Read complete SSE frames without waiting for the upstream body to end."""
    data = []
    for raw_line in stream:
        line = raw_line.decode("utf-8").rstrip("\r\n")
        if not line:
            if data:
                yield "\n".join(data)
                data = []
        elif line.startswith("data:"):
            value = line[5:]
            data.append(value[1:] if value.startswith(" ") else value)


def gateway_responses_stream(stream, protocol, model):
    """Translate upstream text deltas immediately into Responses SSE events."""
    response = {
        "id": "resp_" + uuid.uuid4().hex, "object": "response",
        "created_at": int(time.time()), "model": model, "status": "in_progress",
        "output": [], "error": None, "incomplete_details": None, "usage": None,
    }
    sequence = 0

    def emit(kind, **fields):
        nonlocal sequence
        payload = dict(type=kind, sequence_number=sequence, **fields)
        sequence += 1
        return ("event: " + kind + "\ndata: " + json.dumps(payload, ensure_ascii=False) + "\n\n").encode("utf-8")

    yield emit("response.created", response=response)
    yield emit("response.in_progress", response=response)
    item = None
    part = None
    fields = None
    terminal = False
    finish_reason = None
    usage = {}
    try:
        for data in iter_sse_data(stream):
            if protocol == "openai" and data == "[DONE]":
                terminal = True
                break
            chunk = json.loads(data)
            if not isinstance(chunk, dict) or chunk.get("error") or chunk.get("type") == "error":
                raise ValueError("upstream stream error")
            text_delta = ""
            if protocol == "openai":
                if chunk.get("usage"):
                    usage.update(chunk["usage"])
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    text_delta += delta.get("content") or ""
                    if delta.get("tool_calls") or delta.get("refusal"):
                        raise ValueError("unsupported upstream output")
                    finish_reason = choice.get("finish_reason") or finish_reason
            else:
                kind = chunk.get("type")
                if kind == "message_start":
                    usage.update(chunk.get("message", {}).get("usage") or {})
                elif kind == "content_block_start":
                    block = chunk.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        raise ValueError("unsupported upstream output")
                    if block.get("type") == "text":
                        text_delta = block.get("text", "")
                elif kind == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_delta = delta.get("text", "")
                elif kind == "message_delta":
                    usage.update(chunk.get("usage") or {})
                    finish_reason = chunk.get("delta", {}).get("stop_reason") or finish_reason
                elif kind == "message_stop":
                    terminal = True
                    break
            if text_delta:
                if item is None:
                    item = {"id": "msg_" + uuid.uuid4().hex, "type": "message",
                            "role": "assistant", "status": "in_progress", "content": []}
                    response["output"].append(item)
                    yield emit("response.output_item.added", output_index=0, item=item)
                    part = {"type": "output_text", "text": "", "annotations": []}
                    item["content"].append(part)
                    fields = dict(item_id=item["id"], output_index=0, content_index=0)
                    yield emit("response.content_part.added", **fields, part=part)
                part["text"] += text_delta
                yield emit("response.output_text.delta", **fields, delta=text_delta)
        if not terminal:
            raise ValueError("upstream stream ended before its terminal event")
    except (ValueError, OSError, HTTPException):
        # Headers have already been sent; failures must remain SSE events.
        response["status"] = "failed"
        response["error"] = {"code": "upstream_stream_error", "message": "Upstream stream failed or ended unexpectedly"}
        yield emit("response.failed", response=response)
        return

    incomplete = finish_reason in ("length", "max_tokens", "content_filter")
    response["status"] = "incomplete" if incomplete else "completed"
    if incomplete:
        response["incomplete_details"] = {"reason": "content_filter" if finish_reason == "content_filter" else "max_output_tokens"}
    if item is not None:
        item["status"] = response["status"]
        yield emit("response.output_text.done", **fields, text=part["text"])
        yield emit("response.content_part.done", **fields, part=part)
        yield emit("response.output_item.done", output_index=0, item=item)
    input_tokens = usage.get("prompt_tokens", usage.get("input_tokens", 0))
    output_tokens = usage.get("completion_tokens", usage.get("output_tokens", 0))
    response["usage"] = {"input_tokens": input_tokens, "output_tokens": output_tokens,
                         "total_tokens": usage.get("total_tokens", input_tokens + output_tokens)}
    yield emit("response." + response["status"], response=response)


def gateway_message_events(message, protocol):
    """Encode a buffered converted reply using the caller's SSE protocol."""
    events = []

    def emit(kind, payload):
        prefix = ("event: " + kind + "\n") if kind else ""
        events.append(prefix + "data: " + json.dumps(payload, ensure_ascii=False) + "\n\n")

    if protocol == "anthropic":
        start = dict(message, content=[], stop_reason=None, stop_sequence=None)
        emit("message_start", {"type": "message_start", "message": start})
        for index, block in enumerate(message.get("content", [])):
            emit("content_block_start", {"type": "content_block_start", "index": index,
                 "content_block": {"type": "text", "text": ""}})
            emit("content_block_delta", {"type": "content_block_delta", "index": index,
                 "delta": {"type": "text_delta", "text": block.get("text", "")}})
            emit("content_block_stop", {"type": "content_block_stop", "index": index})
        emit("message_delta", {"type": "message_delta", "delta": {
            "stop_reason": message.get("stop_reason", "end_turn"), "stop_sequence": None},
            "usage": {"output_tokens": message.get("usage", {}).get("output_tokens", 0)}})
        emit("message_stop", {"type": "message_stop"})
    elif protocol == "responses":
        def response_event(kind, **fields):
            emit(kind, dict(type=kind, sequence_number=len(events), **fields))
        response_event("response.created", response=dict(message, status="in_progress", output=[]))
        response_event("response.in_progress", response=dict(message, status="in_progress", output=[]))
        for index, item in enumerate(message.get("output", [])):
            item_id = item["id"]
            response_event("response.output_item.added", output_index=index,
                           item=dict(item, status="in_progress", content=[]))
            for part_index, part in enumerate(item.get("content", [])):
                fields = dict(item_id=item_id, output_index=index, content_index=part_index)
                response_event("response.content_part.added", **fields, part=dict(part, text=""))
                response_event("response.output_text.delta", **fields, delta=part.get("text", ""))
                response_event("response.output_text.done", **fields, text=part.get("text", ""))
                response_event("response.content_part.done", **fields, part=part)
            response_event("response.output_item.done", output_index=index, item=item)
        response_event("response.completed", response=message)
    else:
        common = {k: message[k] for k in ("id", "model")}
        common.update(object="chat.completion.chunk", created=message.get("created", int(time.time())))
        for choice in message.get("choices", []):
            emit(None, dict(common, choices=[{"index": choice.get("index", 0),
                "delta": choice["message"], "finish_reason": None}]))
            emit(None, dict(common, choices=[{"index": choice.get("index", 0),
                "delta": {}, "finish_reason": choice.get("finish_reason", "stop")}]))
        events.append("data: [DONE]\n\n")
    return "".join(events).encode("utf-8")


# ---- Upstream forwarding ----


def openai_image_url_to_anthropic_source(url):
    """OpenAI image_url value -> Anthropic image source, or None if unsupported."""
    url = str(url or "")
    if url.startswith("data:"):
        header, _, data = url.partition(",")
        if ";base64" not in header or not data:
            return None
        media = header[5:].split(";", 1)[0] or "image/png"
        return {"type": "base64", "media_type": media, "data": data}
    if url.startswith("http://") or url.startswith("https://"):
        return {"type": "url", "url": url}
    return None


def openai_to_anthropic_request(body):
    """OpenAI chat/completions request -> Anthropic messages request."""
    messages = []
    system_text = ""
    for msg in body.get("messages", []):
        if not isinstance(msg, dict):
            continue
        role = msg.get("role", "user")
        content = msg.get("content", "")
        blocks = None
        if isinstance(content, list):
            blocks = []
            for part in content:
                if not isinstance(part, dict):
                    continue
                if part.get("type") == "text":
                    blocks.append({"type": "text", "text": part.get("text", "")})
                elif part.get("type") == "image_url":
                    image_url = part.get("image_url")
                    if isinstance(image_url, dict):
                        image_url = image_url.get("url")
                    source = openai_image_url_to_anthropic_source(image_url)
                    if source:
                        blocks.append({"type": "image", "source": source})
            if all(block.get("type") == "text" for block in blocks):
                content = "".join(block.get("text", "") for block in blocks)
            else:
                content = blocks
        if role == "system":
            if blocks is not None:
                system_text = "".join(
                    block.get("text", "") for block in blocks if block.get("type") == "text"
                )
            else:
                system_text = str(content)
        else:
            messages.append({"role": role, "content": content})
    result = {"messages": messages}
    if system_text:
        result["system"] = system_text
    for key in ("model", "max_tokens", "temperature", "top_p", "stream"):
        if body.get(key) is not None:
            result[key] = body[key]
    return result


def anthropic_response_to_openai(body, model):
    """Anthropic messages response -> OpenAI chat/completions response."""
    content = body.get("content", [])
    if isinstance(content, list):
        text = "".join(
            c.get("text", "") for c in content
            if isinstance(c, dict) and c.get("type") == "text"
        )
    else:
        text = str(content or "")
    stop_reason = body.get("stop_reason", "stop")
    finish = "stop"
    if stop_reason == "max_tokens":
        finish = "length"
    elif stop_reason == "tool_use":
        finish = "tool_calls"
    return {
        "id": body.get("id", "chatcmpl-" + uuid.uuid4().hex[:16]),
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": {"role": "assistant", "content": text},
            "finish_reason": finish,
        }],
        "usage": {
            "prompt_tokens": body.get("usage", {}).get("input_tokens", 0),
            "completion_tokens": body.get("usage", {}).get("output_tokens", 0),
            "total_tokens": sum(body.get("usage", {}).get(k, 0) for k in ("input_tokens", "output_tokens")),
        },
    }


def gateway_send_upstream(base_url, path, api_key, body_bytes, timeout=120, protocol="openai"):
    """Return (status, response_file_or_None, error_msg)."""
    url = base_url.rstrip("/") + path
    if protocol == "anthropic":
        headers = {
            "x-api-key": api_key,
            "anthropic-version": "2023-06-01",
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
    else:
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "application/json, text/event-stream",
        }
    req = Request(url, data=body_bytes, headers=headers, method="POST")
    try:
        resp = urlopen(req, timeout=timeout)
        return resp.status, resp, ""
    except HTTPError as e:
        return e.code, e, "HTTP %d" % e.code
    except URLError as e:
        return 0, None, str(e.reason)
    except (OSError, TimeoutError) as e:
        return 0, None, str(e)


def is_quota_exhausted(status, body_text):
    if status in (401, 403, 429) or status >= 500:
        return True
    lowered = body_text.lower()
    return any(kw in lowered for kw in (
        "quota", "exceed", "insufficient", "rate limit",
        "too many", "余额不足", "额度", "exhausted",
    ))



class DashboardHandler(SimpleHTTPRequestHandler):
    snapshot_path = Path(os.environ.get("SNAPSHOT_PATH", "/data/snapshot.json"))
    requests_path = Path(os.environ.get("REQUESTS_PATH", "/data/requests.json"))
    results_path = Path(os.environ.get("RESULTS_PATH", "/data/results.json"))
    order_path = Path(os.environ.get("ORDER_PATH", "/data/order.json"))
    index_path = Path(os.environ.get("INDEX_HTML_PATH", "/srv/index.html"))

    def send_error(self, code, message=None, explain=None):
        body = json.dumps({"error": message or explain or str(code)}, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

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
        if self.path.split("?")[0] == "/api/gateway/config":
            self.handle_gateway_config_get()
            return
        if self.path == "/api/gateway/status":
            self.handle_gateway_status()
            return
        if self.path == "/api/gateway/active":
            self.send_json(gateway_active_snapshot())
            return
        if self.path in ("/v1/models", "/models"):
            self.handle_gateway_models()
            return
        if self.path in ("", "/", "/index.html"):
            html_path = self.index_path
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
        if self.path == "/v1/chat/completions":
            self.handle_gateway_chat("openai")
            return
        if self.path == "/v1/messages":
            self.handle_gateway_chat("anthropic")
            return
        if self.path == "/v1/responses":
            self.handle_gateway_chat("responses")
            return
        if self.path == "/chat/completions":
            self.handle_gateway_chat("openai")
            return
        if self.path == "/messages":
            self.handle_gateway_chat("anthropic")
            return
        if self.path == "/responses":
            self.handle_gateway_chat("responses")
            return
        if self.path == "/api/gateway/config":
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length <= 0 or length > 524288:
                    raise ValueError("invalid payload size")
                payload = json.loads(self.rfile.read(length).decode("utf-8"))
                self.handle_gateway_config_post(payload)
            except (ValueError, UnicodeDecodeError, json.JSONDecodeError) as error:
                self.send_error(400, str(error))
            return
        if self.path not in {"/api/snapshot", "/api/requests", "/api/refresh", "/api/order", "/api/requests/gateway"}:
            self.send_error(404)
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if length <= 0 or length > 524288:
                raise ValueError("invalid payload size")
            payload = json.loads(self.rfile.read(length).decode("utf-8"))
            if not isinstance(payload, dict):
                raise ValueError("snapshot must be an object")
            if self.path == "/api/requests/gateway":
                account_id = str(payload.get("id", "")).strip()
                enabled = payload.get("enabled")
                if type(enabled) is not bool:
                    raise ValueError("enabled must be a boolean")
                accounts = load_requests(self.requests_path)
                if account_id not in accounts:
                    self.send_error(404, "account not found")
                    return
                account = accounts[account_id]
                gateway = dict(account.get("gateway") or {})
                gateway["enabled"] = enabled
                account["gateway"] = gateway
                save_requests(self.requests_path, accounts)
                self.send_json({"ok": True, "id": account_id, "gateway": gateway})
                return
            if self.path == "/api/snapshot":
                save_snapshot(self.snapshot_path, payload)
                self.send_json({"ok": True})
                return
            if self.path == "/api/requests":
                account_id = str(payload.get("id", "")).strip()
                curl = str(payload.get("curl", "")).strip()
                label = str(payload.get("label", "")).strip()
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
                if curl:
                    cookie_expires = extract_cookie_expiry(curl)
                    if cookie_expires:
                        definition["cookieExpires"] = cookie_expires
                    auto_account_id = extract_account_id(curl)
                    if auto_account_id:
                        definition["accountId"] = auto_account_id
                if source == "googleAi":
                    definition["refreshToken"] = refresh_token or existing.get("refreshToken", "")
                    definition["proxy"] = proxy_input or existing.get("proxy", "")
                for _field in ("phone", "username", "password", "accountId", "apiKey"):
                    if _field in payload:
                        _value = str(payload[_field] or "").strip()
                        if _value:
                            definition[_field] = _value
                        elif _field in definition:
                            del definition[_field]
                if isinstance(existing.get("gateway"), dict):
                    definition["gateway"] = dict(existing["gateway"])
                if "gateway" in payload and isinstance(payload["gateway"], dict):
                    gw_in = payload["gateway"]
                    gw_existing = definition.get("gateway", {}) if isinstance(definition.get("gateway"), dict) else {}
                    gw = dict(gw_existing)
                    if "enabled" in gw_in:
                        gw["enabled"] = bool(gw_in["enabled"])
                    if "maxConcurrency" in gw_in:
                        limit = gw_in["maxConcurrency"]
                        if type(limit) is not int or not 1 <= limit <= 10:
                            raise ValueError("每个账号的并发上限必须是 1–10 的整数")
                        gw["maxConcurrency"] = limit
                    if gw:
                        definition["gateway"] = gw
                    elif "gateway" in definition:
                        del definition["gateway"]
                requests[account_id] = definition
                save_requests(self.requests_path, requests)
                _save_result = {"ok": True, "id": account_id, "source": source}
                if "accountId" in definition:
                    _save_result["accountId"] = definition["accountId"]
                if "cookieExpires" in definition:
                    _save_result["cookieExpires"] = definition["cookieExpires"]
                self.send_json(_save_result)
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
            accounts = load_requests(self.requests_path)
            for account_id, definition in accounts.items():
                source = definition.get("source", "")
                if should_skip_source(source, accounts):
                    continue
                try:
                    if source in VOLC_ACTIONS:
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


    def _gateway_authenticate(self, config):
        auth_header = self.headers.get("Authorization", "")
        api_key_header = self.headers.get("x-api-key", "")
        provided = ""
        if auth_header.startswith("Bearer "):
            provided = auth_header[7:].strip()
        elif api_key_header:
            provided = api_key_header.strip()
        expected = str(config.get("apiKey", "")).strip()
        return bool(expected) and provided == expected

    def _gateway_read_body(self):
        length = int(self.headers.get("Content-Length", "0"))
        # 20 MB: base64 image blocks from coding agents easily exceed 5 MB.
        if length <= 0 or length > 20971520:
            raise ValueError("invalid payload size")
        raw = self.rfile.read(length)
        return json.loads(raw.decode("utf-8")), raw

    def _gateway_write_json(self, status, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _gateway_stream_openai(self, base_url, path, api_key, body_bytes, protocol="openai", output_protocol=None, model=""):
        """Make upstream request and stream SSE response back to client in real-time."""
        url = base_url.rstrip("/") + path
        headers = {
            "Authorization": "Bearer " + api_key,
            "Content-Type": "application/json",
            "Accept": "text/event-stream",
        }
        if protocol == "anthropic":
            headers.pop("Authorization")
            headers.update({"x-api-key": api_key, "anthropic-version": "2023-06-01"})
        req = Request(url, data=body_bytes, headers=headers, method="POST")
        try:
            resp = urlopen(req, timeout=120)
        except HTTPError as e:
            try:
                body = e.read()
            finally:
                e.close()
            if is_quota_exhausted(e.code, body.decode("utf-8", errors="replace")):
                raise GatewayRetry("upstream HTTP %d" % e.code) from None
            self.send_response(e.code)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return True
        except URLError as e:
            raise GatewayRetry("upstream connection failed") from None
        except (OSError, TimeoutError) as e:
            raise GatewayRetry("upstream connection failed") from None

        if "text/event-stream" not in resp.headers.get("Content-Type", "").lower():
            resp.close()
            self._gateway_write_json(502, {"error": {"message": "upstream did not return an event stream"}})
            return True
        # Send SSE headers immediately
        self.send_response(200)
        self.send_header("Content-Type", "text/event-stream; charset=utf-8")
        self.send_header("Cache-Control", "no-cache")
        self.send_header("X-Accel-Buffering", "no")
        self.send_header("Connection", "close")
        self.end_headers()
        self.close_connection = True

        # Stream chunks from upstream to client
        try:
            if output_protocol == "responses":
                for event in gateway_responses_stream(resp, protocol, model):
                    self.wfile.write(event)
                    self.wfile.flush()
                return True
            while True:
                chunk = resp.read1(4096)
                if not chunk:
                    break
                self.wfile.write(chunk)
                self.wfile.flush()
        except (OSError, ConnectionError):
            pass
        finally:
            resp.close()
        return True

    def handle_gateway_chat(self, inbound_format):
        """inbound_format: 'openai' | 'anthropic' | 'responses'"""
        config = load_gateway_config()
        if not config.get("enabled"):
            self.send_error(503, "gateway disabled")
            return
        if not self._gateway_authenticate(config):
            self.send_error(401, "invalid gateway api key")
            return
        try:
            body, _raw = self._gateway_read_body()
            if not isinstance(body, dict):
                raise ValueError("request body must be a JSON object")
        except (ValueError, json.JSONDecodeError, UnicodeDecodeError) as exc:
            self.send_error(400, str(exc))
            return

        accounts = load_requests(self.requests_path)
        snapshot = load_snapshot(self.snapshot_path)
        max_retries = int(config.get("maxRetries", 3))
        stream = bool(body.get("stream", False))
        model = body.get("model") or config.get("defaultModel", "")
        last_error = ""

        for _attempt in range(max_retries):
            with reserve_gateway_account(accounts, snapshot, model, inbound_format, stream) as (aid, acc):
                if not aid:
                    d = self._gateway_diagnostics(accounts)
                    msg = "no available gateway accounts (accounts may be at concurrency limit, cooling down, or out of quota; total %d, disabled %d, missing apiKey %d)" % (
                        d["total"], d["disabled"], d["noApiKey"]
                    )
                    if last_error:
                        msg += "; last: " + last_error
                    self.send_error(503, msg)
                    return
                gw = acc.get("gateway") or {}
                category = gateway_category_for_account(acc)
                plan_cfg = config.get("upstreams", {}).get(category, {})
                # Desired upstream protocol matches inbound (responses uses openai endpoint)
                desired_protocol = "anthropic" if inbound_format == "anthropic" else "openai"
                url_key = "anthropicBaseUrl" if desired_protocol == "anthropic" else "openaiBaseUrl"
                base_url = str(plan_cfg.get(url_key, "")).strip()
                upstream_protocol = desired_protocol
                # Fallback: if desired protocol URL missing, use the other one with conversion
                if not base_url:
                    alt_key = "openaiBaseUrl" if desired_protocol == "anthropic" else "anthropicBaseUrl"
                    base_url = str(plan_cfg.get(alt_key, "")).strip()
                    upstream_protocol = "openai" if desired_protocol == "anthropic" else "anthropic"
                if not base_url:
                    last_error = "upstream %s has no baseUrl configured" % category
                    cooldown_account(aid, 30)
                    continue
                upstream_key = str(acc.get("apiKey", "")).strip()

                # ---- Request conversion: inbound -> upstream protocol ----
                if inbound_format == upstream_protocol:
                    upstream_body = dict(body)
                elif inbound_format == "openai" and upstream_protocol == "anthropic":
                    upstream_body = openai_to_anthropic_request(body)
                elif inbound_format == "anthropic" and upstream_protocol == "openai":
                    upstream_body = anthropic_to_openai(body)
                elif inbound_format == "responses" and upstream_protocol == "openai":
                    upstream_body = responses_to_openai(body)
                elif inbound_format == "responses" and upstream_protocol == "anthropic":
                    upstream_body = openai_to_anthropic_request(responses_to_openai(body))
                else:
                    upstream_body = dict(body)
                if not upstream_body.get("model") and model:
                    upstream_body["model"] = model
                # Upstream path
                if upstream_protocol == "anthropic":
                    upstream_path = "/v1/messages"
                else:
                    upstream_path = "/chat/completions"
                    if not stream:
                        upstream_body.pop("stream", None)

                # Responses translates upstream SSE incrementally; other cross-
                # protocol routes still convert complete upstream messages.
                native_stream = stream and inbound_format == upstream_protocol
                responses_stream = stream and inbound_format == "responses"
                upstream_body["stream"] = native_stream or responses_stream
                upstream_bytes = json.dumps(upstream_body, ensure_ascii=False).encode("utf-8")

                # True SSE streaming: bypass buffering, stream directly
                if native_stream or responses_stream:
                    record_gateway_request(aid)
                    try:
                        self._gateway_stream_openai(base_url, upstream_path, upstream_key, upstream_bytes,
                                                    upstream_protocol, inbound_format, model)
                    except GatewayRetry as error:
                        cooldown_account(aid)
                        last_error = str(error)
                        continue
                    return

                status, resp, err = gateway_send_upstream(
                    base_url, upstream_path, upstream_key, upstream_bytes,
                    protocol=upstream_protocol,
                )
                if resp is None:
                    last_error = err or "upstream connection failed"
                    cooldown_account(aid, 30)
                    continue

                try:
                    resp_body = resp.read()
                except (OSError, HTTPException):
                    cooldown_account(aid, 30)
                    last_error = "upstream response interrupted"
                    continue
                finally:
                    resp.close()
                resp_text = resp_body.decode("utf-8", errors="replace")

                if status != 200:
                    if is_quota_exhausted(status, resp_text):
                        cooldown_account(aid)
                        last_error = "account %s quota exhausted (HTTP %d)" % (aid, status)
                        continue
                    self.send_response(status)
                    self.send_header("Content-Type", "application/json; charset=utf-8")
                    self.send_header("Content-Length", str(len(resp_body)))
                    self.end_headers()
                    self.wfile.write(resp_body)
                    return

                record_gateway_request(aid)

                # ---- Response conversion: upstream -> inbound protocol ----
                try:
                    upstream_json = json.loads(resp_text)
                except json.JSONDecodeError:
                    upstream_json = None
                if not isinstance(upstream_json, dict) or upstream_json.get("error"):
                    self._gateway_write_json(502, {"error": {"message": "upstream returned an invalid model response"}})
                    return

                out_model = model or upstream_body.get("model", "")
                if upstream_protocol == inbound_format:
                    out_bytes = resp_body
                elif upstream_protocol == "openai" and inbound_format == "anthropic":
                    out = openai_to_anthropic(upstream_json, out_model) if isinstance(upstream_json, dict) else None
                    out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8") if out else resp_body
                elif upstream_protocol == "openai" and inbound_format == "responses":
                    out = openai_to_responses(upstream_json, out_model) if isinstance(upstream_json, dict) else None
                    out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8") if out else resp_body
                elif upstream_protocol == "anthropic" and inbound_format == "openai":
                    out = anthropic_response_to_openai(upstream_json, out_model) if isinstance(upstream_json, dict) else None
                    out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8") if out else resp_body
                elif upstream_protocol == "anthropic" and inbound_format == "responses":
                    if isinstance(upstream_json, dict):
                        oa = anthropic_response_to_openai(upstream_json, out_model)
                        out = openai_to_responses(oa, out_model)
                        out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8")
                    else:
                        out_bytes = resp_body
                elif upstream_protocol == "anthropic" and inbound_format == "anthropic":
                    out_bytes = resp_body
                else:
                    out_bytes = resp_body

                if stream:
                    out_bytes = gateway_message_events(json.loads(out_bytes), inbound_format)
                self.send_response(200)
                self.send_header("Content-Type", "text/event-stream; charset=utf-8" if stream else "application/json; charset=utf-8")
                self.send_header("Content-Length", str(len(out_bytes)))
                self.end_headers()
                self.wfile.write(out_bytes)
                return

        self.send_error(503, "all gateway accounts exhausted: " + last_error)

    def _gateway_diagnostics(self, accounts):
        """Count why accounts are not eligible for gateway routing."""
        total = 0
        no_gateway = 0
        disabled = 0
        no_key = 0
        for acc in accounts.values():
            if not isinstance(acc, dict):
                continue
            total += 1
            gw = acc.get("gateway") or {}
            if gw.get("enabled") is False:
                disabled += 1
                continue
            if not str(acc.get("apiKey", "")).strip():
                no_key += 1
                continue
        return {
            "total": total, "noGateway": no_gateway, "disabled": disabled,
            "noApiKey": no_key,
        }

    def handle_gateway_models(self):
        config = load_gateway_config()
        if not config.get("enabled"):
            self.send_error(503, "gateway disabled")
            return
        if not self._gateway_authenticate(config):
            self.send_error(401, "invalid gateway api key")
            return
        accounts = load_requests(self.requests_path)
        snapshot = load_snapshot(self.snapshot_path)
        aid, acc = select_gateway_account(accounts, snapshot)
        if not aid:
            d = self._gateway_diagnostics(accounts)
            msg = "no available gateway accounts (total %d, disabled %d, missing apiKey %d)" % (
                d["total"], d["disabled"], d["noApiKey"]
            )
            self.send_error(503, msg)
            return
        gw = acc.get("gateway") or {}
        category = gateway_category_for_account(acc)
        upstream = config.get("upstreams", {}).get(category, {})
        base_url = str(upstream.get("openaiBaseUrl", "")).strip()
        if not base_url:
            self.send_error(503, "upstream %s openaiBaseUrl not configured" % category)
            return
        status, resp, err = gateway_send_upstream(
            base_url, "/models", str(acc.get("apiKey", "")).strip(), b"{}"
        )
        if resp is not None and status == 200:
            body = resp.read()
            self.send_response(200)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        # Upstream does not support /models (e.g. Volcengine plan endpoints);
        # return a static list based on configured default model.
        default_model = str(config.get("defaultModel", "")).strip()
        models = []
        if default_model:
            models.append({"id": default_model, "object": "model", "created": 0, "owned_by": "gateway"})
        static = {"object": "list", "data": models}
        body = json.dumps(static, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def handle_gateway_config_get(self):
        config = load_gateway_config()
        reveal = "reveal=1" in self.path or "reveal=true" in self.path
        masked = dict(config)
        if masked.get("apiKey"):
            if not reveal:
                masked["apiKey"] = mask_secret(masked["apiKey"])
            masked["hasApiKey"] = True
        self.send_json(masked)

    def handle_gateway_config_post(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        config = load_gateway_config()
        for key in ("enabled", "apiKey", "defaultModel", "maxRetries"):
            if key in payload:
                config[key] = payload[key]
        if isinstance(payload.get("upstreams"), dict):
            for cat in ("agentPlan", "codingPlan"):
                if cat in payload["upstreams"] and isinstance(payload["upstreams"][cat], dict):
                    config.setdefault("upstreams", {}).setdefault(cat, {}).update(
                        payload["upstreams"][cat]
                    )
        save_gateway_config(config)
        self.send_json({"ok": True})

    def handle_gateway_status(self):
        config = load_gateway_config()
        accounts = load_requests(self.requests_path)
        snapshot = gateway_routing_snapshot(load_snapshot(self.snapshot_path))
        active_counts = {}
        for entry in gateway_active_snapshot()["activeRequests"]:
            aid = entry["accountId"]
            active_counts[aid] = active_counts.get(aid, 0) + 1
        now = time.time()
        account_status = []
        for aid, acc in accounts.items():
            if not isinstance(acc, dict):
                continue
            gw = acc.get("gateway") or {}
            remaining = account_remaining_percent(aid, snapshot)
            cooling_until = _gateway_cooldowns.get(aid, 0)
            enabled = gw.get("enabled") is not False and bool(acc.get("apiKey"))
            concurrency_limit = gateway_concurrency_limit(acc)
            active_count = active_counts.get(aid, 0)
            weight = (remaining / 100.0) * max(0, concurrency_limit - active_count) if enabled and now >= cooling_until else 0
            account_status.append({
                "id": aid,
                "label": acc.get("label", ""),
                "source": acc.get("source", ""),
                "gatewayEnabled": enabled,
                "category": gateway_category_for_account(acc),
                "remainingPercent": round(remaining, 1),
                "weight": round(weight, 4),
                "maxConcurrency": concurrency_limit,
                "activeRequests": active_count,
                "cooling": now < cooling_until,
                "cooldownRemaining": max(0, int(cooling_until - now)) if now < cooling_until else 0,
                "requests": _gateway_stats.get("by_account", {}).get(aid, 0),
            })
        self.send_json({
            "enabled": bool(config.get("enabled")),
            "totalRequests": _gateway_stats.get("total_requests", 0),
            "accounts": account_status,
        })


    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


if __name__ == "__main__":
    os.chdir(os.environ.get("APP_DIR", "/srv"))
    server = ThreadingHTTPServer(("0.0.0.0", int(os.environ.get("PORT", "80"))), DashboardHandler)
    quota_stop = Event()
    Thread(target=gateway_quota_worker, args=(DashboardHandler.requests_path, quota_stop), daemon=True).start()
    try:
        server.serve_forever()
    finally:
        quota_stop.set()
        server.server_close()
