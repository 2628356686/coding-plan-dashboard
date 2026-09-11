import base64
import hashlib
import hmac
import json
import logging
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import random
import uuid
import math
from contextlib import contextmanager, ExitStack
from logging.handlers import RotatingFileHandler
from threading import RLock, Thread, Event
from datetime import datetime, timezone
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from http.client import HTTPException
from pathlib import Path
from urllib.parse import parse_qsl, quote, urlencode, urlparse
from urllib.error import HTTPError, URLError
from urllib.request import ProxyHandler, Request, build_opener, urlopen


_DEFAULT_LOG_PATH = Path(__file__).resolve().parent / "log" / "dashboard.log"
LOG_PATH = Path(os.environ.get("LOG_PATH") or _DEFAULT_LOG_PATH)
LOG_MAX_BYTES = 5 * 1024 * 1024
LOG_BACKUP_COUNT = 5


def build_file_logger(path, name="dashboard"):
    """Persistent rotating log; falls back to stderr when the path is unwritable."""
    logger = logging.getLogger(name)
    logger.setLevel(logging.INFO)
    logger.propagate = False
    if logger.handlers:
        return logger
    formatter = logging.Formatter("%(asctime)s %(levelname)s [%(threadName)s] %(message)s")
    try:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        handler = RotatingFileHandler(path, maxBytes=LOG_MAX_BYTES,
                                      backupCount=LOG_BACKUP_COUNT, encoding="utf-8")
    except OSError:
        handler = logging.StreamHandler(sys.stderr)
        logger.warning("log file %s unavailable, logging to stderr instead", path)
    handler.setFormatter(formatter)
    logger.addHandler(handler)
    return logger


log = build_file_logger(LOG_PATH)


def tail_log_file(path, limit):
    """Return the last `limit` lines of a text log file, or "" when unavailable."""
    try:
        data = Path(path).read_bytes()
    except OSError:
        return ""
    lines = data.decode("utf-8", errors="replace").splitlines()
    return "\n".join(lines[-limit:])


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
                log.warning("gw quota refresh for %s failed: HTTP %s", aid, status)
                continue
            quota = normalize_gateway_quota(json.loads(body))
            with _gateway_active_lock:
                _gateway_quota_cache[aid] = quota
        except (ValueError, TypeError, AttributeError, OSError) as error:
            # Keep the last known quota when credentials or the endpoint fail.
            log.warning("gw quota refresh for %s failed: %s", aid, error)
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
def reserve_gateway_account(accounts, snapshot, model, protocol, stream, platform="unknown"):
    # Selection and reservation share a lock so simultaneous requests see each other.
    with ExitStack() as stack:
        with _gateway_active_lock:
            aid, account = select_gateway_account(accounts, gateway_routing_snapshot(snapshot))
            if aid:
                stack.enter_context(track_gateway_request(aid, account, model, protocol, stream, platform))
        yield aid, account


@contextmanager
def track_gateway_request(account_id, account, model, protocol, stream, platform="unknown"):
    request_id = uuid.uuid4().hex
    entry = {
        "id": request_id, "accountId": account_id,
        "accountLabel": account.get("label") or account.get("accountId") or account_id,
        "model": model, "protocol": protocol, "stream": bool(stream),
        "platform": platform,
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


def normalize_gateway_port(value):
    """Coerce a config or payload port to a valid integer, else None (follow PORT env)."""
    if value is None or value == "" or isinstance(value, bool):
        return None
    try:
        port = int(value)
    except (TypeError, ValueError):
        return None
    return port if 1 <= port <= 65535 else None


def load_gateway_config():
    default = {
        "enabled": False,
        "port": None,
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
            merged["port"] = normalize_gateway_port(merged.get("port"))
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


_gateway_stats.update(load_gateway_stats())


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


def cooldown_account(account_id, seconds=GATEWAY_COOLDOWN_SECONDS, reason=""):
    with _gateway_active_lock:
        _gateway_cooldowns[account_id] = time.time() + seconds
    log.warning("gw account %s cooldown %ss: %s", account_id, seconds, reason or "unspecified")


def record_gateway_request(account_id, platform="unknown"):
    with _gateway_active_lock:
        _gateway_stats["total_requests"] = _gateway_stats.get("total_requests", 0) + 1
        by_acc = _gateway_stats.setdefault("by_account", {})
        by_acc[account_id] = by_acc.get(account_id, 0) + 1
        by_platform = _gateway_stats.setdefault("by_platform", {})
        by_platform[platform] = by_platform.get(platform, 0) + 1
    try:
        save_gateway_stats(_gateway_stats)
    except OSError:
        pass


def detect_gateway_agent_platform(headers):
    """Best-effort client platform from inbound gateway headers."""
    originator = str(headers.get("Originator") or "").strip()
    if originator:
        return originator
    user_agent = str(headers.get("User-Agent") or "").strip()
    if user_agent:
        return user_agent.split("/", 1)[0].strip() or "unknown"
    return "unknown"


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


def normalize_volcengine_image_detail(body):
    """Copy Chat image parts and adapt Codex's original detail for Volcengine."""
    result = dict(body)
    messages = body.get("messages")
    if not isinstance(messages, list):
        return result
    result["messages"] = []
    for message in messages:
        if not isinstance(message, dict) or not isinstance(message.get("content"), list):
            result["messages"].append(message)
            continue
        parts = []
        for part in message["content"]:
            if isinstance(part, dict) and part.get("type") == "image_url":
                image_url = part.get("image_url")
                if isinstance(image_url, dict) and image_url.get("detail") == "original":
                    part = {**part, "image_url": {**image_url, "detail": "high"}}
            parts.append(part)
        result["messages"].append({**message, "content": parts})
    return result


def responses_image_url(part):
    """Responses input_image part -> image URL string, or None."""
    url = part.get("image_url")
    if isinstance(url, dict):
        url = url.get("url")
    return str(url) if url else None


def responses_tool_alias(name, namespace=None):
    """Stable, Chat-compatible names; keep a per-request reverse map."""
    if not namespace and re.fullmatch(r"[A-Za-z0-9_-]{1,64}", name):
        return name
    identity = json.dumps([namespace, name], ensure_ascii=False)
    return "tool_" + hashlib.sha256(identity.encode()).hexdigest()[:40]


def responses_tools_to_openai(tools, context):
    result = []

    def add(tool, namespace=None):
        kind = tool.get("type")
        if kind == "namespace":
            for child in tool.get("tools", []):
                add(child, tool["name"])
            return
        if kind == "tool_search" and tool.get("execution") != "client":
            raise ValueError("Responses bridge supports only client-executed tool_search")
        if kind not in ("function", "custom", "tool_search"):
            raise ValueError("Responses bridge does not support tool type: " + str(kind))
        name = "__gateway_tool_search" if kind == "tool_search" else tool.get("name")
        if not isinstance(name, str) or not name:
            raise ValueError("tool name is required")
        alias = responses_tool_alias(name, namespace)
        if alias in context:
            raise ValueError("duplicate tool name")
        context[alias] = {"name": name, "type": kind, "namespace": namespace}
        function = {"name": alias, "description": tool.get("description", "")}
        if kind == "custom":
            function["parameters"] = {"type": "object", "properties": {
                "input": {"type": "string", "description": "The complete raw input for this tool."}},
                "required": ["input"], "additionalProperties": False}
            if tool.get("format"):
                function["description"] += "\nInput format: " + json.dumps(tool["format"], ensure_ascii=False)
        else:
            function["parameters"] = tool.get("parameters") or {"type": "object", "properties": {}}
            if "strict" in tool:
                function["strict"] = tool["strict"]
        result.append({"type": "function", "function": function})

    for tool in tools:
        add(tool)
    return result


def responses_content_to_openai(content):
    """Keep ordered text/image parts, including MCP image results."""
    if isinstance(content, str):
        return content
    if isinstance(content, dict) and "content" in content:
        content = content["content"]
    if not isinstance(content, list):
        return json.dumps(content, ensure_ascii=False)
    parts = []
    for part in content:
        if not isinstance(part, dict):
            parts.append({"type": "text", "text": str(part)})
            continue
        kind = part.get("type")
        if kind in ("text", "input_text", "output_text"):
            parts.append({"type": "text", "text": part.get("text", "")})
        elif kind in ("input_image", "image_url", "image"):
            url = responses_image_url(part)
            if not url and part.get("data"):
                url = "data:%s;base64,%s" % (part.get("mimeType", "image/png"), part["data"])
            if not url and part.get("source"):
                url = anthropic_image_to_openai_url(part["source"])
            if not url:
                raise ValueError("image requires inline data or an image URL; file_id is not supported")
            image = {"url": url}
            detail = part.get("detail")
            if isinstance(part.get("image_url"), dict):
                detail = part["image_url"].get("detail", detail)
            if detail is not None:
                image["detail"] = detail
            parts.append({"type": "image_url", "image_url": image})
        else:
            raise ValueError("unsupported Responses content type: " + str(kind))
    if all(p["type"] == "text" for p in parts):
        return "".join(p["text"] for p in parts)
    return parts


def responses_to_openai(body, tool_context=None):
    if body.get("previous_response_id"):
        raise ValueError("Responses bridge is stateless; send full input history instead of previous_response_id")
    context = tool_context if tool_context is not None else {}
    tools = responses_tools_to_openai(body.get("tools") or [], context)
    # Tool search can declare additional tools in input history rather than tools.
    for entry in body.get("input", []) if isinstance(body.get("input"), list) else []:
        if isinstance(entry, dict) and entry.get("type") == "tool_search_output":
            loaded_context = {}
            for tool in responses_tools_to_openai(entry.get("tools") or [], loaded_context):
                alias = tool["function"]["name"]
                if alias not in context:
                    tools.append(tool)
                    context[alias] = loaded_context[alias]
    messages = []
    if body.get("instructions"):
        messages.append({"role": "system", "content": str(body["instructions"])})

    user_input = body.get("input", "")
    if isinstance(user_input, list):
        pending = set()
        images = []
        for item in user_input:
            if isinstance(item, dict):
                if item.get("type") == "message" or "role" in item:
                    messages.append({
                        "role": item.get("role", "user"),
                        "content": responses_content_to_openai(item.get("content", [])),
                    })
                elif item.get("type") in ("function_call", "custom_tool_call", "tool_search_call"):
                    call_id = item["call_id"]
                    search = item["type"] == "tool_search_call"
                    if search and item.get("execution") != "client":
                        raise ValueError("server-executed tool search history is not supported")
                    alias = "__gateway_tool_search" if search else responses_tool_alias(item["name"], item.get("namespace"))
                    arguments = (json.dumps({"input": item.get("input", "")}, ensure_ascii=False)
                                 if item["type"] == "custom_tool_call" else item.get("arguments", "{}"))
                    if search:
                        arguments = json.dumps(item.get("arguments", {}), ensure_ascii=False)
                    if not isinstance(json.loads(arguments), dict):
                        raise ValueError("tool arguments must be a JSON object")
                    call = {"id": call_id, "type": "function", "function": {"name": alias, "arguments": arguments}}
                    if messages and messages[-1]["role"] == "assistant":
                        messages[-1].setdefault("tool_calls", []).append(call)
                    else:
                        messages.append({"role": "assistant", "content": None, "tool_calls": [call]})
                    pending.add(call_id)
                elif item.get("type") in ("function_call_output", "custom_tool_call_output", "tool_search_output"):
                    if item.get("call_id") not in pending:
                        raise ValueError("tool result has no matching pending tool call")
                    output = (json.dumps({"tools": item.get("tools", [])}, ensure_ascii=False)
                              if item["type"] == "tool_search_output" else item.get("output", ""))
                    # Some MCP clients serialize their content wrapper as JSON.
                    if isinstance(output, str):
                        try:
                            decoded = json.loads(output)
                            if isinstance(decoded, dict) and isinstance(decoded.get("content"), list):
                                output = decoded
                        except (ValueError, TypeError):
                            pass
                    content = responses_content_to_openai(output)
                    if isinstance(content, list):
                        images.extend(p for p in content if p["type"] == "image_url")
                        content = "".join(p["text"] for p in content if p["type"] == "text")
                    messages.append({"role": "tool", "tool_call_id": item["call_id"], "content": content})
                    pending.discard(item["call_id"])
                    # Chat tool messages cannot carry images. Insert them after all
                    # parallel results, so assistant/tool adjacency remains valid.
                    if not pending and images:
                        messages.append({"role": "user", "content": images})
                        images = []
                elif item.get("type") in ("input_text", "input_image"):
                    messages.append({"role": "user", "content": responses_content_to_openai([item])})
                elif item.get("type") == "reasoning":
                    continue  # Opaque provider reasoning cannot be replayed to Chat.
                else:
                    raise ValueError("unsupported Responses input type: " + str(item.get("type")))
        if pending:
            raise ValueError("tool call history is missing tool results")
        user_input = ""
    if user_input:
        messages.append({"role": "user", "content": user_input})
    result = {"messages": messages}
    if tools:
        result["tools"] = tools
    choice = body.get("tool_choice")
    if isinstance(choice, dict):
        if choice.get("type") not in ("function", "custom", "tool_search"):
            raise ValueError("unsupported Responses tool_choice")
        alias = ("__gateway_tool_search" if choice["type"] == "tool_search"
                 else responses_tool_alias(choice["name"], choice.get("namespace")))
        if alias not in context:
            raise ValueError("tool_choice must name a declared tool")
        result["tool_choice"] = {"type": "function", "function": {"name": alias}}
    elif choice is not None:
        result["tool_choice"] = choice
    for key in ("model", "temperature", "top_p", "stream", "parallel_tool_calls"):
        if body.get(key) is not None:
            result[key] = body[key]
    if body.get("max_output_tokens"):
        result["max_tokens"] = body["max_output_tokens"]
    return result


def response_tool_item(call, context=None):
    function = call.get("function") or {}
    name = function.get("name", "")
    if not name or not call.get("id"):
        raise ValueError("upstream tool call is missing its name or id")
    spec = (context or {}).get(name, {"name": name, "type": "function"})
    if context is not None and name not in context:
        raise ValueError("upstream called an undeclared tool")
    item = {"id": "fc_" + uuid.uuid4().hex, "call_id": call["id"],
            "name": spec["name"], "status": "completed"}
    if spec.get("namespace"):
        item["namespace"] = spec["namespace"]
    arguments = function.get("arguments") or "{}"
    if spec["type"] == "tool_search":
        arguments = json.loads(arguments)
        if not isinstance(arguments, dict):
            raise ValueError("tool search arguments must be an object")
        item.pop("name")
        item.update(type="tool_search_call", execution="client", arguments=arguments)
    elif spec["type"] == "custom":
        payload = json.loads(arguments)
        if not isinstance(payload, dict) or not isinstance(payload.get("input"), str):
            raise ValueError("custom tool arguments require a string input")
        item.update(type="custom_tool_call", input=payload["input"])
    else:
        if not isinstance(json.loads(arguments), dict):
            raise ValueError("tool arguments must be a JSON object")
        item.update(type="function_call", arguments=arguments)
    return item


def openai_to_responses(body, model, tool_context=None):
    choice = (body.get("choices") or [{}])[0]
    message = choice.get("message", {})
    content = message.get("content", "") or ""
    output = []
    if content:
        output.append({"id": "msg_" + uuid.uuid4().hex, "type": "message", "status": "completed",
                       "role": "assistant", "content": [{"type": "output_text", "text": content, "annotations": []}]})
    incomplete = choice.get("finish_reason") in ("length", "content_filter")
    if not incomplete:
        output.extend(response_tool_item(call, tool_context) for call in message.get("tool_calls") or [])
    return {
        "id": body.get("id", "resp_" + uuid.uuid4().hex[:16]),
        "object": "response",
        "model": model,
        "created_at": int(time.time()),
        "status": "incomplete" if incomplete else "completed",
        "error": None,
        "incomplete_details": {"reason": "max_output_tokens" if choice.get("finish_reason") == "length" else "content_filter"} if incomplete else None,
        "output": output,
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
    if data:
        yield "\n".join(data)


def gateway_responses_stream(stream, protocol, model, tool_context=None):
    """Translate text and indexed tool argument deltas into Responses events."""
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
    calls = {}
    text_index = None

    def tool_events(index, call_delta):
        state = calls.setdefault(index, {"id": "", "name": "", "arguments": "", "item": None})
        function = call_delta.get("function") or {}
        state["id"] += call_delta.get("id") or ""
        state["name"] += function.get("name") or ""
        fragment = function.get("arguments") or ""
        if not isinstance(fragment, str):
            raise ValueError("upstream tool arguments must be strings")
        state["arguments"] += fragment
        # Name/id may themselves arrive in fragments. Wait for arguments before
        # publishing immutable item metadata. Empty-argument tools start at EOF.
        if state["item"] is None and state["name"] and state["id"] and state["arguments"]:
            spec = (tool_context or {}).get(state["name"], {"name": state["name"], "type": "function"})
            if tool_context is not None and state["name"] not in tool_context:
                raise ValueError("upstream called an undeclared tool")
            custom = spec["type"] == "custom"
            state["custom"] = custom
            state["search"] = spec["type"] == "tool_search"
            state["published_name"] = state["name"]
            state["published_id"] = state["id"]
            state["item"] = {"id": "fc_" + uuid.uuid4().hex, "type": "custom_tool_call" if custom else "function_call",
                             "call_id": state["id"], "name": spec["name"], "status": "in_progress",
                             "input" if custom else "arguments": ""}
            if spec.get("namespace"):
                state["item"]["namespace"] = spec["namespace"]
            if state["search"]:
                state["item"].pop("name")
                state["item"].update(type="tool_search_call", execution="client", arguments={})
            state["output_index"] = len(response["output"])
            response["output"].append(state["item"])
            yield emit("response.output_item.added", output_index=state["output_index"], item=state["item"])
            fragment = state["arguments"]
        if state["item"] is not None:
            if state["name"] != state["published_name"] or state["id"] != state["published_id"]:
                raise ValueError("upstream tool identity changed after arguments began")
            if fragment and not state["custom"] and not state["search"]:
                state["item"]["arguments"] += fragment
                yield emit("response.function_call_arguments.delta", item_id=state["item"]["id"],
                           output_index=state["output_index"], delta=fragment)

    try:
        for data in iter_sse_data(stream):
            if protocol == "openai" and data == "[DONE]":
                terminal = True
                break
            chunk = json.loads(data)
            if not isinstance(chunk, dict) or chunk.get("error") or chunk.get("type") == "error":
                raise ValueError("upstream stream error")
            text_delta = ""
            tool_deltas = []
            if protocol == "openai":
                if chunk.get("usage"):
                    usage.update(chunk["usage"])
                for choice in chunk.get("choices", []):
                    if choice.get("index", 0) != 0:
                        continue
                    delta = choice.get("delta") or {}
                    text_delta += delta.get("content") or ""
                    tool_deltas.extend((c.get("index", i), c) for i, c in enumerate(delta.get("tool_calls") or []))
                    if delta.get("refusal"):
                        raise ValueError("unsupported upstream output")
                    finish_reason = choice.get("finish_reason") or finish_reason
            else:
                kind = chunk.get("type")
                if kind == "message_start":
                    usage.update(chunk.get("message", {}).get("usage") or {})
                elif kind == "content_block_start":
                    block = chunk.get("content_block") or {}
                    if block.get("type") == "tool_use":
                        initial = block.get("input")
                        tool_deltas.append((chunk["index"], {"id": block["id"], "function": {
                            "name": block["name"], "arguments": json.dumps(initial, ensure_ascii=False) if initial else ""}}))
                    if block.get("type") == "text":
                        text_delta = block.get("text", "")
                elif kind == "content_block_delta":
                    delta = chunk.get("delta") or {}
                    if delta.get("type") == "text_delta":
                        text_delta = delta.get("text", "")
                    elif delta.get("type") == "input_json_delta":
                        tool_deltas.append((chunk["index"], {"function": {"arguments": delta.get("partial_json", "")}}))
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
                    text_index = len(response["output"])
                    response["output"].append(item)
                    yield emit("response.output_item.added", output_index=text_index, item=item)
                    part = {"type": "output_text", "text": "", "annotations": []}
                    item["content"].append(part)
                    fields = dict(item_id=item["id"], output_index=text_index, content_index=0)
                    yield emit("response.content_part.added", **fields, part=part)
                part["text"] += text_delta
                yield emit("response.output_text.delta", **fields, delta=text_delta)
            for index, call_delta in tool_deltas:
                yield from tool_events(index, call_delta)
        if not terminal:
            raise ValueError("upstream stream ended before its terminal event")
        if calls and finish_reason in ("length", "max_tokens", "content_filter"):
            # Do not emit completed executable calls when arguments are truncated.
            response["status"] = "incomplete"
            response["incomplete_details"] = {"reason": "content_filter" if finish_reason == "content_filter" else "max_output_tokens"}
            for output in response["output"]:
                output["status"] = "incomplete"
            yield emit("response.incomplete", response=response)
            return
        # Validate ALL calls before publishing any executable completed item.
        finished = {}
        for index, state in calls.items():
            finished[index] = response_tool_item({"id": state["id"], "function": {
                "name": state["name"], "arguments": state["arguments"] or "{}"}}, tool_context)
        for index, state in calls.items():
            if state["item"] is None:
                yield from tool_events(index, {"function": {"arguments": "{}"}})
            final = finished[index]
            target = state["item"]
            call_fields = {"item_id": target["id"], "output_index": state["output_index"]}
            if state["search"]:
                target["arguments"] = final["arguments"]
            elif state["custom"]:
                target["input"] = final["input"]
                yield emit("response.custom_tool_call_input.delta", **call_fields, delta=target["input"])
                yield emit("response.custom_tool_call_input.done", **call_fields, input=target["input"])
            else:
                target["arguments"] = final["arguments"]
                yield emit("response.function_call_arguments.done", **call_fields,
                           name=target["name"], arguments=target["arguments"])
            target["status"] = "completed"
            yield emit("response.output_item.done", output_index=state["output_index"], item=target)
    except (ValueError, TypeError, KeyError, OSError, HTTPException):
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
        yield emit("response.output_item.done", output_index=text_index, item=item)
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
        if role == "tool":
            block = {"type": "tool_result", "tool_use_id": msg["tool_call_id"], "content": content or ""}
            if messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].append(block)
            else:
                messages.append({"role": "user", "content": [block]})
            continue
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
        if msg.get("tool_calls"):
            content = ([{"type": "text", "text": content}] if content else []) if isinstance(content, str) or content is None else list(content)
            for call in msg["tool_calls"]:
                content.append({"type": "tool_use", "id": call["id"], "name": call["function"]["name"],
                                "input": json.loads(call["function"].get("arguments") or "{}")})
        if role in ("system", "developer"):
            if blocks is not None:
                system_text += "".join(
                    block.get("text", "") for block in blocks if block.get("type") == "text"
                )
            else:
                system_text += str(content)
        else:
            if role == "user" and messages and messages[-1]["role"] == "user" and isinstance(messages[-1]["content"], list):
                messages[-1]["content"].extend(content if isinstance(content, list) else [{"type": "text", "text": content}])
            else:
                messages.append({"role": role, "content": content})
    result = {"messages": messages}
    if system_text:
        result["system"] = system_text
    for key in ("model", "max_tokens", "temperature", "top_p", "stream"):
        if body.get(key) is not None:
            result[key] = body[key]
    if body.get("tools"):
        result["tools"] = [{"name": t["function"]["name"], "description": t["function"].get("description", ""),
                            "input_schema": t["function"]["parameters"]} for t in body["tools"]]
        choice = body.get("tool_choice", "auto")
        if isinstance(choice, dict):
            result["tool_choice"] = {"type": "tool", "name": choice["function"]["name"]}
        else:
            result["tool_choice"] = {"type": "any" if choice == "required" else choice}
        if body.get("parallel_tool_calls") is False:
            result["tool_choice"]["disable_parallel_tool_use"] = True
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
    message = {"role": "assistant", "content": text}
    if isinstance(content, list):
        calls = [{"id": c["id"], "type": "function", "function": {
            "name": c["name"], "arguments": json.dumps(c.get("input", {}), ensure_ascii=False)}}
            for c in content if isinstance(c, dict) and c.get("type") == "tool_use"]
        if calls:
            message["tool_calls"] = calls
    return {
        "id": body.get("id", "chatcmpl-" + uuid.uuid4().hex[:16]),
        "object": "chat.completion",
        "model": model,
        "choices": [{
            "index": 0,
            "message": message,
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


# ============================================================================
# SMS verification platforms proxy (号码盾 smsnex + EOMSG 易码)
# ============================================================================

SMS_CONFIG_PATH = Path(os.environ.get("SMS_CONFIG_PATH", "/data/sms.json"))
EOMSG_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
            "Chrome/140 Safari/537.36")


class SmsError(Exception):
    """Validation or upstream failure carrying an HTTP status for the API."""

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status
        self.message = message


def load_sms_config():
    default = {
        "smsnex": {"enabled": False, "origin": "https://www.smsnex.com",
                   "apiKey": "", "pollIntervalMs": 3000, "maxWaitMs": 300000},
        "eomsg": {"enabled": False, "origin": "https://api.eomsg.com/zc/data.php",
                  "apiKey": "", "pollIntervalMs": 3000, "maxWaitMs": 300000},
    }
    try:
        data = json.loads(SMS_CONFIG_PATH.read_text(encoding="utf-8"))
    except (FileNotFoundError, json.JSONDecodeError):
        return default
    if not isinstance(data, dict):
        return default
    for provider, cfg in default.items():
        stored = data.get(provider)
        if isinstance(stored, dict):
            cfg.update({k: v for k, v in stored.items() if k in cfg})
    return default


def save_sms_config(config):
    save_snapshot(str(SMS_CONFIG_PATH), config)


def sms_normalize(value):
    """Recursively convert snake_case keys to camelCase (smsnex protocol)."""
    if isinstance(value, list):
        return [sms_normalize(item) for item in value]
    if isinstance(value, dict):
        result = {}
        for key, item in value.items():
            parts = key.split("_")
            name = parts[0] + "".join(p[:1].upper() + p[1:] for p in parts[1:] if p)
            result[name] = sms_normalize(item)
        return result
    return value


def sms_verification_code(message):
    """Extract the longest 4-8 digit run from an SMS body, if any."""
    parts = [part for part in re.split(r"\D", message) if 4 <= len(part) <= 8]
    return max(parts, key=len) if parts else None


_sms_code_gate = {"next": 0.0}
_sms_code_lock = RLock()


def smsnex_upstream(cfg, method, path, query=None, body=None):
    if path.endswith("/code"):
        # smsnex rejects tighter /code polling; keep a global minimum interval.
        with _sms_code_lock:
            wait = _sms_code_gate["next"] - time.time()
            if wait > 0:
                time.sleep(wait)
            _sms_code_gate["next"] = time.time() + max(cfg.get("pollIntervalMs", 3000), 2100) / 1000.0
    url = cfg["origin"].rstrip("/") + "/openapi/v1" + path
    if query:
        url += "?" + urlencode(query)
    request = Request(url, method=method, headers={"Authorization": "Bearer " + cfg["apiKey"]})
    data = None
    if body is not None:
        data = json.dumps(body).encode("utf-8")
        request.add_header("Content-Type", "application/json")
    try:
        with urlopen(request, data=data, timeout=30) as response:
            status = response.status
            raw = response.read().decode("utf-8")
    except HTTPError as error:
        message = "接码平台请求失败"
        try:
            payload = json.loads(error.read().decode("utf-8"))
            if isinstance(payload.get("message"), str) and payload["message"]:
                message = payload["message"]
        except (ValueError, OSError):
            pass
        raise SmsError(error.code, message) from None
    except (URLError, TimeoutError, OSError):
        raise SmsError(502, "接码平台连接失败") from None
    try:
        payload = json.loads(raw)
    except json.JSONDecodeError:
        raise SmsError(502, "接码平台返回了无效数据") from None
    if not (200 <= status < 300) or payload.get("code") != 0:
        message = payload.get("message")
        if not isinstance(message, str) or not message:
            message = "接码平台请求失败"
        raise SmsError(status if not (200 <= status < 300) else 502, message)
    return sms_normalize(payload.get("data"))


def eomsg_upstream(cfg, code, params=()):
    url = cfg["origin"] + ("&" if "?" in cfg["origin"] else "?") + urlencode(
        [("code", code), ("token", cfg["apiKey"])] + list(params))
    retryable = code in ("leftAmount", "getMsg", "queryUsed")
    for attempt in range(3):
        try:
            request = Request(url, headers={
                "User-Agent": EOMSG_UA,
                "Accept": "text/plain, */*",
                "Referer": "https://www.eomsg.com/",
            })
            with urlopen(request, timeout=30) as response:
                status = response.status
                text = response.read().decode("utf-8", "replace").strip()
        except HTTPError as error:
            status = error.code
            try:
                text = error.read().decode("utf-8", "replace").strip()
            except OSError:
                text = ""
        except (URLError, TimeoutError, OSError):
            if retryable and attempt < 2:
                time.sleep(0.35 * (attempt + 1))
                continue
            raise SmsError(502, "EOMSG 连接失败") from None
        upper = text.upper()
        transient = status >= 500 or "ERROR CODE: 520" in upper
        if retryable and transient and attempt < 2:
            time.sleep(0.35 * (attempt + 1))
            continue
        if not (200 <= status < 300) or upper.startswith("ERR"):
            raise SmsError(status if not (200 <= status < 300) else 502,
                           text or "EOMSG 请求失败")
        return text
    raise SmsError(502, "EOMSG 暂时不可用，请稍后重试")


# ============================================================================
# Private-window login helper (账号管理 → 去登录)
# ============================================================================

VOLC_LOGIN_URL = "https://signin.volcengine.com/auth/login"


def find_private_browser():
    """Locate a locally installed browser that supports private windows.

    Returns the command prefix [executable, private-mode-flag], or None when no
    usable browser exists on the server host (e.g. inside a container)."""
    candidates = []
    if sys.platform == "win32":
        dirs = [os.environ.get("ProgramFiles", r"C:\Program Files"),
                os.environ.get("ProgramFiles(x86)", r"C:\Program Files (x86)"),
                os.environ.get("LocalAppData", "")]
        for base in dirs:
            if not base:
                continue
            candidates += [
                (os.path.join(base, "Google", "Chrome", "Application", "chrome.exe"), "--incognito"),
                (os.path.join(base, "Microsoft", "Edge", "Application", "msedge.exe"), "--inprivate"),
                (os.path.join(base, "Mozilla Firefox", "firefox.exe"), "-private-window"),
            ]
        for name, flag in (("chrome.exe", "--incognito"),
                           ("msedge.exe", "--inprivate"),
                           ("firefox.exe", "-private-window")):
            found = shutil.which(name)
            if found:
                candidates.append((found, flag))
    elif sys.platform == "darwin":
        candidates = [
            ("/Applications/Google Chrome.app/Contents/MacOS/Google Chrome", "--incognito"),
            ("/Applications/Microsoft Edge.app/Contents/MacOS/Microsoft Edge", "--inprivate"),
            ("/Applications/Firefox.app/Contents/MacOS/firefox", "-private-window"),
        ]
    else:
        for name, flag in (("google-chrome", "--incognito"),
                           ("chromium", "--incognito"),
                           ("chromium-browser", "--incognito"),
                           ("microsoft-edge", "--inprivate"),
                           ("firefox", "-private-window")):
            found = shutil.which(name)
            if found:
                candidates.append((found, flag))
    for path, flag in candidates:
        if path and os.path.isfile(path):
            return [path, flag]
    return None


def open_private_login():
    """Launch a private/incognito window at VOLC_LOGIN_URL on the server host.

    Returns the browser executable name, or None when no browser is available.
    The URL is a server-side constant and the command is a plain argv list, so
    no user input ever reaches the subprocess."""
    cmd = find_private_browser()
    if not cmd:
        return None
    subprocess.Popen(cmd + [VOLC_LOGIN_URL], stdin=subprocess.DEVNULL,
                     stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                     close_fds=True)
    return cmd[0]


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

    def log_message(self, fmt, *args):
        log.info("%s %s", self.address_string(), fmt % args)

    def handle_logs_get(self):
        query = dict(parse_qsl(urlparse(self.path).query))
        if query.get("download"):
            try:
                body = LOG_PATH.read_bytes()
            except OSError:
                body = b""
            self.send_response(200)
            self.send_header("Content-Type", "text/plain; charset=utf-8")
            self.send_header("Content-Disposition", 'attachment; filename="dashboard.log"')
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        try:
            limit = int(query.get("lines", "500"))
        except ValueError:
            limit = 500
        limit = max(1, min(limit, 5000))
        body = tail_log_file(LOG_PATH, limit).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "text/plain; charset=utf-8")
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
        if self.path.split("?")[0] == "/api/logs":
            self.handle_logs_get()
            return
        if self.path.startswith("/api/sms/"):
            self.handle_sms_request("GET")
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
        if self.path.startswith("/api/sms/"):
            self.handle_sms_request("POST")
            return
        if self.path == "/api/open-login":
            self.handle_open_login()
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
                if enabled and not str(account.get("apiKey", "")).strip():
                    self.send_error(400, "account has no apiKey; cannot enable gateway")
                    return
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
                    elif _field in existing:
                        # Fields the request omits keep their stored value, so a
                        # label-only update cannot wipe sensitive account data.
                        definition[_field] = existing[_field]
                if isinstance(existing.get("dashboardHidden"), bool) and existing["dashboardHidden"]:
                    definition["dashboardHidden"] = True
                if payload.get("dashboardHidden") is True:
                    definition["dashboardHidden"] = True
                elif payload.get("dashboardHidden") is False and "dashboardHidden" in definition:
                    del definition["dashboardHidden"]
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
                if definition.get("dashboardHidden") is True or should_skip_source(source, accounts):
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

    def _gateway_stream_openai(self, base_url, path, api_key, body_bytes, protocol="openai", output_protocol=None, model="", tool_context=None):
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
                for event in gateway_responses_stream(resp, protocol, model, tool_context):
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
            tool_context = {} if inbound_format == "responses" else None
            responses_body = responses_to_openai(body, tool_context) if inbound_format == "responses" else None
        except (ValueError, TypeError, KeyError, AttributeError) as exc:
            self._gateway_write_json(400, {"error": {"type": "invalid_request_error", "message": str(exc)}})
            return
        platform = detect_gateway_agent_platform(self.headers)

        accounts = load_requests(self.requests_path)
        snapshot = load_snapshot(self.snapshot_path)
        max_retries = max(1, min(10, int(config.get("maxRetries", 3))))
        stream = bool(body.get("stream", False))
        model = body.get("model") or config.get("defaultModel", "")
        last_error = ""
        started = time.monotonic()
        log.info("gw request %s model=%s stream=%s protocol=%s platform=%s",
                 self.path, model or "-", stream, inbound_format, platform or "-")

        for _account_attempt in range(max(1, len(accounts))):
            with reserve_gateway_account(accounts, snapshot, model, inbound_format, stream, platform) as (aid, acc):
                if not aid:
                    d = self._gateway_diagnostics(accounts)
                    msg = "no available gateway accounts (accounts may be at concurrency limit, cooling down, or out of quota; total %d, disabled %d, missing apiKey %d)" % (
                        d["total"], d["disabled"], d["noApiKey"]
                    )
                    if last_error:
                        msg += "; last: " + last_error
                    self.log_gateway_unavailable(accounts, snapshot, last_error)
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
                    cooldown_account(aid, 30, "upstream %s has no baseUrl configured" % category)
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
                    upstream_body = dict(responses_body)
                elif inbound_format == "responses" and upstream_protocol == "anthropic":
                    upstream_body = openai_to_anthropic_request(responses_body)
                else:
                    upstream_body = dict(body)
                if not upstream_body.get("model") and model:
                    upstream_body["model"] = model
                if platform and "codex" in platform.lower() \
                        and upstream_protocol == "openai" and category in {"agentPlan", "codingPlan"}:
                    upstream_body = normalize_volcengine_image_detail(upstream_body)
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

                # Keep the same account and concurrency reservation across retries.
                for attempt in range(max_retries):
                    # True SSE streaming: bypass buffering, stream directly
                    if native_stream or responses_stream:
                        record_gateway_request(aid, platform)
                        try:
                            self._gateway_stream_openai(base_url, upstream_path, upstream_key, upstream_bytes,
                                                        upstream_protocol, inbound_format, model, tool_context)
                        except GatewayRetry as error:
                            self._gateway_retry_account(aid, attempt, max_retries, str(error))
                            last_error = str(error)
                            continue
                        log.info("gw stream complete via %s in %.1fs", aid, time.monotonic() - started)
                        return

                    status, resp, err = gateway_send_upstream(
                        base_url, upstream_path, upstream_key, upstream_bytes,
                        protocol=upstream_protocol,
                    )
                    if resp is None:
                        last_error = err or "upstream connection failed"
                        self._gateway_retry_account(aid, attempt, max_retries, last_error)
                        continue

                    try:
                        resp_body = resp.read()
                    except (OSError, HTTPException):
                        self._gateway_retry_account(aid, attempt, max_retries, "upstream response interrupted")
                        last_error = "upstream response interrupted"
                        continue
                    finally:
                        resp.close()
                    resp_text = resp_body.decode("utf-8", errors="replace")

                    if status != 200:
                        if is_quota_exhausted(status, resp_text):
                            log.warning("gw account %s upstream HTTP %d quota-like: %.200s", aid, status, resp_text)
                            self._gateway_retry_account(aid, attempt, max_retries, "upstream HTTP %d quota-like" % status)
                            last_error = "account %s quota exhausted (HTTP %d)" % (aid, status)
                            continue
                        log.info("gw upstream HTTP %d passthrough (account %s)", status, aid)
                        self.send_response(status)
                        self.send_header("Content-Type", "application/json; charset=utf-8")
                        self.send_header("Content-Length", str(len(resp_body)))
                        self.end_headers()
                        self.wfile.write(resp_body)
                        return

                    record_gateway_request(aid, platform)

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
                        try:
                            out = openai_to_responses(upstream_json, out_model, tool_context) if isinstance(upstream_json, dict) else None
                        except (ValueError, TypeError, KeyError):
                            self._gateway_write_json(502, {"error": {"message": "invalid upstream tool call"}})
                            return
                        out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8") if out else resp_body
                    elif upstream_protocol == "anthropic" and inbound_format == "openai":
                        out = anthropic_response_to_openai(upstream_json, out_model) if isinstance(upstream_json, dict) else None
                        out_bytes = json.dumps(out, ensure_ascii=False).encode("utf-8") if out else resp_body
                    elif upstream_protocol == "anthropic" and inbound_format == "responses":
                        if isinstance(upstream_json, dict):
                            oa = anthropic_response_to_openai(upstream_json, out_model)
                            try:
                                out = openai_to_responses(oa, out_model, tool_context)
                            except (ValueError, TypeError, KeyError):
                                self._gateway_write_json(502, {"error": {"message": "invalid upstream tool call"}})
                                return
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
                    log.info("gw request complete via %s (upstream %s) in %.1fs",
                             aid, upstream_protocol, time.monotonic() - started)
                    return

        self.send_error(503, "all gateway accounts exhausted: " + last_error)

    def _gateway_retry_account(self, aid, attempt, max_attempts, reason):
        if attempt + 1 >= max_attempts:
            cooldown_account(aid, GATEWAY_COOLDOWN_SECONDS, reason)
        else:
            log.warning("gw account %s retry same account attempt %d/%d: %s",
                        aid, attempt + 2, max_attempts, reason)

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

    def log_gateway_unavailable(self, accounts, snapshot, last_error):
        """Persist per-account routing state so recurring 503s can be diagnosed from logs."""
        routing = gateway_routing_snapshot(snapshot)
        now = time.time()
        with _gateway_active_lock:
            active_counts = {}
            for entry in _gateway_active.values():
                active_counts[entry["accountId"]] = active_counts.get(entry["accountId"], 0) + 1
        details = []
        for aid, acc in accounts.items():
            if not isinstance(acc, dict):
                continue
            gw = acc.get("gateway") or {}
            if gw.get("enabled") is False:
                details.append("%s: gateway disabled" % aid)
                continue
            if not str(acc.get("apiKey", "")).strip():
                details.append("%s: missing apiKey" % aid)
                continue
            cooling = _gateway_cooldowns.get(aid, 0)
            if now < cooling:
                details.append("%s: cooling down %.0fs" % (aid, cooling - now))
                continue
            active = active_counts.get(aid, 0)
            limit = gateway_concurrency_limit(acc)
            if active >= limit:
                details.append("%s: at concurrency limit %d/%d" % (aid, active, limit))
                continue
            remaining = account_remaining_percent(aid, routing)
            if remaining <= 0:
                details.append("%s: out of quota" % aid)
                continue
            details.append("%s: available (%.0f%% quota, %d active)" % (aid, remaining, active))
        log.warning("gw 503 no available accounts [%s]; last error: %s",
                    "; ".join(details), last_error or "none")

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
            self.log_gateway_unavailable(accounts, snapshot, "")
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
        masked["effectivePort"] = self.server.server_address[1]
        if masked.get("apiKey"):
            if not reveal:
                masked["apiKey"] = mask_secret(masked["apiKey"])
            masked["hasApiKey"] = True
        self.send_json(masked)

    def handle_gateway_config_post(self, payload):
        if not isinstance(payload, dict):
            raise ValueError("payload must be an object")
        config = load_gateway_config()
        new_listener = None
        if "port" in payload:
            port = normalize_gateway_port(payload["port"])
            if port is None and payload["port"] not in (None, ""):
                raise ValueError("port must be an integer between 1 and 65535, or null to follow PORT")
            target = port if port is not None else int(os.environ.get("PORT", "80"))
            if target != self.server.server_address[1]:
                try:
                    new_listener = bind_dashboard_server(target)
                except OSError as error:
                    raise ValueError("port %d cannot be bound: %s" % (target, error)) from None
            config["port"] = port
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
        serving_port = new_listener.server_address[1] if new_listener else self.server.server_address[1]
        self._gateway_write_json(200, {"ok": True, "port": config.get("port"), "effectivePort": serving_port})
        if new_listener is not None:
            activate_dashboard_server(new_listener, retire=self.server)

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

    def handle_open_login(self):
        browser = open_private_login()
        if browser is None:
            self.send_error(503, "no usable browser found on the server host")
            return
        self.send_json({"ok": True, "browser": os.path.basename(browser)})

    def _sms_read_json(self):
        length = int(self.headers.get("Content-Length", "0"))
        if length <= 0 or length > 16384:
            raise SmsError(400, "请求体过大")
        try:
            return json.loads(self.rfile.read(length).decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError):
            raise SmsError(400, "请求体必须是有效 JSON") from None

    def handle_sms_platform_get(self):
        config = load_sms_config()
        platforms = []
        for pid, name, fixed_console in (
                ("smsnex", "号码盾", None),
                ("eomsg", "EOMSG 易码", "https://www.eomsg.com/appweb/t1.html")):
            cfg = config[pid]
            key = cfg.get("apiKey", "")
            console_url = fixed_console or cfg["origin"].rstrip("/") + "/console/#/phone/domestic"
            platforms.append({
                "id": pid,
                "name": name,
                "origin": cfg["origin"],
                "consoleUrl": console_url,
                "configured": bool(key),
                "keyPreview": (key[:8] + "…") if key else "",
                "enabled": cfg.get("enabled", False),
            })
        self._gateway_write_json(200, {"platforms": platforms})

    def handle_sms_platform_post(self, payload):
        provider = payload.get("provider", "smsnex")
        if provider not in ("smsnex", "eomsg"):
            raise SmsError(400, "未知接码平台")
        config = load_sms_config()
        cfg = config[provider]
        if isinstance(payload.get("enabled"), bool):
            cfg["enabled"] = payload["enabled"]
        origin = payload.get("origin")
        if isinstance(origin, str) and origin.strip():
            origin = origin.strip().rstrip("/")
            parsed = urlparse(origin)
            if parsed.scheme not in ("http", "https") or not parsed.netloc:
                raise SmsError(400, "平台地址必须是有效的 HTTP 或 HTTPS 地址")
            cfg["origin"] = origin
        if payload.get("clearApiKey") is True:
            cfg["apiKey"] = ""
        elif isinstance(payload.get("apiKey"), str) and payload["apiKey"].strip():
            cfg["apiKey"] = payload["apiKey"].strip()
        if cfg.get("enabled") and not cfg.get("apiKey"):
            raise SmsError(400, "启用平台前请填写 API Key" +
                           (" / Token" if provider == "eomsg" else ""))
        save_sms_config(config)
        self._gateway_write_json(200, {"ok": True, "platform": {
            "id": provider, "configured": bool(cfg.get("apiKey")),
            "enabled": cfg.get("enabled", False),
        }})

    def handle_sms_request(self, method):
        path = self.path.split("?")[0][len("/api/sms/"):]
        query = dict(parse_qsl(urlparse(self.path).query, keep_blank_values=True))
        try:
            if path == "platform":
                if method == "POST":
                    self.handle_sms_platform_post(self._sms_read_json())
                else:
                    self.handle_sms_platform_get()
                return
            provider = query.get("provider", "smsnex")
            if provider not in ("smsnex", "eomsg"):
                raise SmsError(400, "未知接码平台")
            cfg = load_sms_config()[provider]
            if not cfg.get("enabled") or not cfg.get("apiKey"):
                raise SmsError(400, "请先配置并启用" + ("EOMSG" if provider == "eomsg" else "号码盾"))
            self._gateway_write_json(200, self._sms_dispatch(provider, cfg, method, path, query))
        except SmsError as error:
            self._gateway_write_json(error.status, {"error": error.message})

    def _sms_dispatch(self, provider, cfg, method, path, query):
        invalid = SmsError(400, "请检查项目、通道和号码参数")
        if path == "account":
            if provider == "eomsg":
                return {"balance": eomsg_upstream(cfg, "leftAmount"), "username": "EOMSG"}
            return smsnex_upstream(cfg, "GET", "/me")
        if path == "projects":
            if provider == "eomsg":
                name = query.get("name", "智谱AI").strip()
                return {"list": [{"id": name, "name": name, "platform": "eomsg"}], "total": 1}
            market = query.get("platform", "domestic")
            if market not in ("domestic", "international"):
                raise invalid
            params = [("platform", market), ("page", query.get("page", "1")), ("size", "50")]
            name = query.get("name", "").strip()
            if name:
                params.append(("name", name))
            return smsnex_upstream(cfg, "GET", "/projects", params)
        if path == "channels":
            if provider == "eomsg":
                return {"list": [{"uid": "default", "name": "固定收费", "price": None,
                                  "availableNumber": 1}]}
            project_id = query.get("projectId", "").strip()
            market = query.get("platform", "domestic")
            if not project_id or market not in ("domestic", "international"):
                raise invalid
            params = [("platform", market)]
            country = query.get("country", "").strip()
            if country:
                params.append(("country", country))
            value = smsnex_upstream(cfg, "GET",
                                    "/projects/%s/channels" % quote(project_id, safe=""), params)
            if isinstance(value.get("list"), list):
                value["list"].sort(key=lambda c: c["price"]
                                   if isinstance(c.get("price"), (int, float))
                                   and not isinstance(c.get("price"), bool) else float("inf"))
            return value
        if path == "rent" and method == "POST":
            body = self._sms_read_json()
            if not isinstance(body, dict):
                raise invalid
            if provider == "eomsg":
                keyword = str(body.get("projectId", "")).strip()
                if not keyword:
                    raise invalid
                params = [("keyWord", keyword),
                          ("cardType", str(body.get("cardType") or "全部"))]
                phone = str(body.get("phone", "")).strip()
                province = str(body.get("province", "")).strip()
                if phone:
                    params.append(("phone", phone))
                if province:
                    params.append(("province", province))
                number = eomsg_upstream(cfg, "getPhone", params)
                return {"id": number, "phone": number, "projectName": keyword,
                        "price": None, "status": "pending"}
            project_id = str(body.get("projectId", "")).strip()
            channel = str(body.get("channelUid", "")).strip()
            market = str(body.get("platform") or "domestic")
            if not project_id or not channel or market not in ("domestic", "international"):
                raise invalid
            up_body = {"project_id": project_id, "channel_uid": channel, "platform": market}
            country = str(body.get("country", "")).strip()
            phone = str(body.get("phone", "")).strip()
            if market == "international" and not country:
                raise invalid
            if country:
                up_body["country"] = country
            if phone:
                up_body["phone"] = phone
            return smsnex_upstream(cfg, "POST", "/phone/rent", body=up_body)
        action, _, ident = path.partition("/")
        ident = ident.strip()
        if action == "release":
            if method != "POST" or not ident:
                raise invalid
            if provider == "smsnex":
                if not ident.isdigit() or int(ident) == 0:
                    raise invalid
                return smsnex_upstream(cfg, "POST", "/phone/%s/release" % ident, body={})
            return {"ok": True, "result": eomsg_upstream(cfg, "release", [("phone", ident)])}
        if action not in ("code", "poll") or method != "GET" or not ident:
            raise invalid
        deadline = time.time() + min(cfg.get("maxWaitMs", 300000), 300000) / 1000.0
        interval = max(cfg.get("pollIntervalMs", 3000), 2100) / 1000.0
        if provider == "smsnex":
            if not ident.isdigit() or int(ident) == 0:
                raise invalid
            while True:
                value = smsnex_upstream(cfg, "GET", "/phone/%s/code" % ident)
                if action == "code" or value.get("status") != "pending":
                    return value
                if time.time() >= deadline:
                    return {"status": "timeout", "code": None, "sms": None}
                time.sleep(interval)
        keyword = query.get("keyWord", "").strip()
        previous = query.get("previousSms", "")
        if not keyword:
            raise SmsError(400, "EOMSG 查码必须提供短信关键词")
        while True:
            message = eomsg_upstream(cfg, "getMsg", [("phone", ident), ("keyWord", keyword)])
            if "尚未收到" not in message and (not previous or message != previous):
                return {"status": "success", "code": sms_verification_code(message), "sms": message}
            if action == "code":
                return {"status": "pending", "code": None, "sms": None}
            if time.time() >= deadline:
                return {"status": "timeout", "code": None, "sms": None}
            time.sleep(interval)

    def send_json(self, payload):
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        self.send_response(200)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)


_active_dashboard_server = {"httpd": None}


class DashboardServer(ThreadingHTTPServer):
    """Route unhandled handler-thread errors into the persistent log."""

    def handle_error(self, request, client_address):
        error = sys.exc_info()[1]
        if isinstance(error, (BrokenPipeError, ConnectionResetError)):
            log.info("client disconnected %s", client_address[0])
        else:
            log.error("request handler error from %s: %s", client_address[0], error, exc_info=True)


def bind_dashboard_server(port, host="0.0.0.0"):
    """Bind the dashboard listener without serving; raises OSError when the port is taken."""
    return DashboardServer((host, port), DashboardHandler)


def activate_dashboard_server(httpd, retire=None):
    Thread(target=httpd.serve_forever, name="dashboard-http", daemon=True).start()
    _active_dashboard_server["httpd"] = httpd
    if retire is not None:
        Thread(target=_retire_dashboard_server, args=(retire,), daemon=True).start()


def _retire_dashboard_server(httpd):
    httpd.shutdown()
    httpd.server_close()


if __name__ == "__main__":
    os.chdir(os.environ.get("APP_DIR", "/srv"))
    configured_port = normalize_gateway_port(load_gateway_config().get("port"))
    httpd = bind_dashboard_server(configured_port or int(os.environ.get("PORT", "80")))
    quota_stop = Event()
    Thread(target=gateway_quota_worker, args=(DashboardHandler.requests_path, quota_stop), daemon=True).start()
    activate_dashboard_server(httpd)
    log.info("dashboard listening on %s:%s (pid %d, log file %s)",
             httpd.server_address[0], httpd.server_address[1], os.getpid(), LOG_PATH)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        pass
    finally:
        log.info("dashboard shutting down")
        quota_stop.set()
        current = _active_dashboard_server["httpd"]
        if current is not None:
            current.shutdown()
            current.server_close()
