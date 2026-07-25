import json
import tempfile
import unittest
from pathlib import Path

from server import (
    build_http_request,
    execute_volcengine_openapi,
    infer_source,
    is_success_status,
    load_credentials,
    load_requests,
    load_results,
    load_order,
    load_snapshot,
    mask_secret,
    parse_curl,
    save_credentials,
    save_order,
    save_requests,
    save_results,
    save_snapshot,
    should_skip_source,
    volcengine_sign,
    VOLC_ACTIONS,
)


class SnapshotPersistenceTest(unittest.TestCase):
    def test_round_trips_snapshot_as_json(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "snapshot.json"
            snapshot = {"codex": {"remaining": 2}, "updatedAt": "2026-07-14T00:00:00Z"}
            save_snapshot(path, snapshot)
            self.assertEqual(load_snapshot(path), snapshot)
            self.assertEqual(json.loads(path.read_text()), snapshot)

    def test_round_trips_raw_query_results_cache(self):
        with tempfile.TemporaryDirectory() as temp_dir:
            path = Path(temp_dir) / "results.json"
            results = {
                "codexUsage": {
                    "ok": True,
                    "status": 0,
                    "body": '{"rate_limit":{}}',
                    "updatedAt": "2026-07-14T00:00:00Z",
                }
            }
            save_results(path, results)
            self.assertEqual(load_results(path), results)
            self.assertEqual(json.loads(path.read_text()), results)


    def test_load_requests_migrates_old_format(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.json"
            save_snapshot(path, {"codexUsage": {"curl": "curl test", "updatedAt": "2026-01-01"}, "minimax": {"curl": "curl mm"}})
            loaded = load_requests(path)
            # Old format should be migrated to account IDs
            self.assertNotIn("codexUsage", loaded)
            self.assertNotIn("minimax", loaded)
            self.assertEqual(len(loaded), 2)
            for acc_id, acc in loaded.items():
                self.assertTrue(acc_id.startswith("acc_"))
                self.assertIn("source", acc)
                self.assertIn("label", acc)
                self.assertIn("curl", acc)
            # Second load should not re-migrate
            loaded2 = load_requests(path)
            self.assertEqual(loaded, loaded2)

class CurlImportTest(unittest.TestCase):
    def test_only_http_200_is_a_successful_provider_result(self):
        self.assertTrue(is_success_status(200))
        self.assertFalse(is_success_status(0))
        self.assertFalse(is_success_status(201))
        self.assertFalse(is_success_status(500))

    def test_newapi_codex_skips_official_codex_requests(self):
        accounts = {"acc1": {"source": "codexNewApi", "curl": "..."}, "acc2": {"source": "codexNewApiCredits", "curl": "..."}, "acc3": {"source": "codexUsage", "curl": "..."}, "acc4": {"source": "codexCredits", "curl": "..."}}
        self.assertTrue(should_skip_source("codexUsage", accounts))
        self.assertTrue(should_skip_source("codexCredits", accounts))
        self.assertTrue(should_skip_source("codexCredits", {"a1": {"source": "codexNewApiCredits", "curl": "..."}, "a2": {"source": "codexCredits", "curl": "..."}}))
        self.assertFalse(should_skip_source("codexNewApi", accounts))
        self.assertFalse(should_skip_source("codexNewApiCredits", accounts))
        self.assertFalse(should_skip_source("minimax", accounts))

    def test_infers_source_from_endpoint(self):
        import server
        import unittest.mock
        sample_host = "newapi.example.lan"
        with unittest.mock.patch.object(server, "NEWAPI_HOSTS", {sample_host}):
            self.assertEqual(infer_source(f"curl 'http://{sample_host}:3000/api/channel/26/codex/usage/reset-credits'"), "codexNewApiCredits")
            self.assertEqual(infer_source(f"curl 'http://{sample_host}:3000/api/channel/42/codex/usage'"), "codexNewApi")
            self.assertEqual(infer_source(f"curl 'http://{sample_host}:3000/api/channel/999999/codex/usage'"), "codexNewApi")
            self.assertEqual(infer_source(f"curl 'http://{sample_host}:3000/api/channel/1/codex/usage/reset-credits'"), "codexNewApiCredits")
            # Non-matching paths on a NewAPI host are rejected
            with self.assertRaises(ValueError):
                infer_source(f"curl 'http://{sample_host}:3000/api/channel/26/other'")
            with self.assertRaises(ValueError):
                infer_source(f"curl 'http://{sample_host}:3000/api/users/me'")
        self.assertEqual(infer_source("curl 'https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats'"), "kimi")
        self.assertEqual(infer_source("curl 'https://longcat.chat/api/pay/quota/metering/token-packs/summary'"), "longcat")
        self.assertEqual(infer_source("curl 'https://cs-data.qianwenai.com/data/api.json?product=sfm_bailian&action=BroadScopeAspnGateway&api=zeldaHttp.apikeyMgr.%2Ftokenplan%2Fpersonal%2Fapi%2Fv2%2Fusage'"), "qianwen")
        self.assertEqual(infer_source("curl https://chatgpt.com/backend-api/wham/rate-limit-reset-credits"), "codexCredits")
        self.assertEqual(infer_source("curl https://chatgpt.com/backend-api/wham/usage"), "codexUsage")
        self.assertEqual(infer_source("curl https://www.minimaxi.com/backend/account/token_plan/remains_percent"), "minimax")
        self.assertEqual(infer_source("curl https://console.volcengine.com/api/top/ark/cn-beijing/2024-01-01/GetCodingPlanUsage?"), "volcCoding")
        self.assertEqual(infer_source("curl https://console.volcengine.com/api/top/ark/cn-beijing/2024-01-01/GetAgentPlanUsageDetails?"), "volcAgent")
        self.assertEqual(infer_source("curl https://console.volcengine.com/api/top/ark/cn-beijing/2024-01-01/GetAgentPlanAFPUsage?"), "volcAgent")

    def test_parses_allowed_curl_without_shell(self):
        command = "curl 'https://www.minimaxi.com/backend/account/token_plan/remains_percent' -H 'accept: application/json' --data-raw '{}'"
        self.assertEqual(parse_curl(command)[0], "https://www.minimaxi.com/backend/account/token_plan/remains_percent")

    def test_rejects_unapproved_host_and_shell_operator(self):
        with self.assertRaises(ValueError):
            parse_curl("curl 'https://example.com/data'")
        with self.assertRaises(ValueError):
            parse_curl("curl 'https://www.minimaxi.com/data' && rm -rf /")

    def test_converts_headers_cookies_and_json_body(self):
        command = "curl 'https://www.minimaxi.com/data' -H 'x-group-id: 123' -b 'session=abc' --data-raw '{}'"
        request, proxy = build_http_request(parse_curl(command)[1])
        self.assertEqual(request.get_method(), "POST")
        self.assertEqual(request.get_header("X-group-id"), "123")
        self.assertEqual(request.get_header("Cookie"), "session=abc")
        self.assertIsNone(proxy)

    def test_accepts_combined_silent_flags_and_http_proxy(self):
        command = "curl -sS https://chatgpt.com/backend-api/wham/rate-limit-reset-credits --proxy http://proxy.example:1081"
        _, args = parse_curl(command)
        request, proxy = build_http_request(args)
        self.assertEqual(request.full_url, "https://chatgpt.com/backend-api/wham/rate-limit-reset-credits")
        self.assertEqual(proxy, "http://proxy.example:1081")

    def test_accepts_line_continuations_and_semicolons_inside_cookie(self):
        command = """curl 'https://www.minimaxi.com/backend/account/token_plan/remains_percent' \\
  -H 'accept: application/json, text/plain, */*' \\
  -b 'session=abc; _token=secret; minimax_group_id_v2=123' \\
  -H 'x-group-id: 123'"""
        _, args = parse_curl(command)
        request, _ = build_http_request(args)
        self.assertEqual(request.get_header("Cookie"), "session=abc; _token=secret; minimax_group_id_v2=123")

    def test_accepts_header_without_colon_or_value(self):
        command = "curl 'https://www.kimi.com/apiv2/kimi.gateway.membership.v2.MembershipService/GetSubscriptionStats' -H 'x-msh-session-id;' -H 'accept: */*'"
        request, _ = build_http_request(parse_curl(command)[1])
        self.assertEqual(request.get_header("X-msh-session-id"), "")
        self.assertEqual(request.get_header("Accept"), "*/*")

    def test_accepts_private_http_newapi_curl_and_insecure_flag(self):
        import server
        import unittest.mock
        sample_host = "newapi.example.lan"
        command = f"curl 'http://{sample_host}:3000/api/channel/7/codex/usage' --insecure -H 'New-Api-User: 1'"
        with unittest.mock.patch.object(server, "NEWAPI_HOSTS", {sample_host}):
            url, args = parse_curl(command)
            self.assertEqual(url, f"http://{sample_host}:3000/api/channel/7/codex/usage")
            request, _ = build_http_request(args)
            self.assertEqual(request.get_header("New-api-user"), "1")
        # Without the host whitelisted the URL is rejected.
        with self.assertRaises(ValueError):
            parse_curl(command)



class VolcengineOpenApiTest(unittest.TestCase):
    def test_signing_produces_valid_authorization_header(self):
        headers = volcengine_sign(
            "AKTEST", "SKTEST", "POST", "/",
            {"Action": "GetAgentPlanAFPUsage", "Version": "2024-01-01"},
            b"{}", region="cn-beijing", service="ark",
        )
        self.assertIn("Authorization", headers)
        self.assertTrue(headers["Authorization"].startswith("HMAC-SHA256 Credential=AKTEST/"))
        self.assertIn("SignedHeaders=content-type;host;x-content-sha256;x-date", headers["Authorization"])
        self.assertIn("X-Date", headers)
        self.assertIn("X-Content-Sha256", headers)
        self.assertEqual(headers["Host"], "open.volcengineapi.com")
        self.assertEqual(headers["Content-Type"], "application/json")

    def test_volc_actions_map_sources(self):
        self.assertEqual(VOLC_ACTIONS["volcAgent"], "GetAgentPlanAFPUsage")
        self.assertEqual(VOLC_ACTIONS["volcCoding"], "GetCodingPlanUsage")

    def test_mask_secret_hides_middle(self):
        self.assertEqual(mask_secret("abcd1234efgh"), "abcd****efgh")
        self.assertEqual(mask_secret("short"), "****")
        self.assertEqual(mask_secret(""), "")

    def test_credentials_round_trip(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "creds.json"
            save_credentials(path, {"volc": {"accessKeyId": "AK", "secretAccessKey": "SK"}})
            loaded = load_credentials(path)
            self.assertEqual(loaded["volc"]["accessKeyId"], "AK")
            self.assertEqual(load_credentials(Path(tmp) / "missing.json"), {})

    def test_execute_openapi_requires_credentials(self):
        with self.assertRaises(ValueError):
            execute_volcengine_openapi({"accessKeyId": "", "secretAccessKey": ""}, "GetAgentPlanAFPUsage")


    def test_post_requests_preserves_curl_when_updating_creds_only(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "requests.json"
            save_requests(path, {"acc_1": {"source": "volcCoding", "label": "acct1", "curl": "curl test1", "updatedAt": "2026-01-01"}})
            # Update with ak/sk but no curl - should preserve existing curl
            loaded = load_requests(path)
            loaded["acc_1"] = {"source": "volcCoding", "label": "acct1", "curl": "curl test1", "ak": "AKTEST", "sk": "SKTEST", "updatedAt": "2026-01-02"}
            save_requests(path, loaded)
            result = load_requests(path)
            self.assertEqual(result["acc_1"]["curl"], "curl test1")
            self.assertEqual(result["acc_1"]["ak"], "AKTEST")
            self.assertEqual(result["acc_1"]["sk"], "SKTEST")


    def test_edit_label_on_curl_less_volc_account(self):
        """Saving a label on an existing volc account with empty curl should succeed and preserve ak/sk."""
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            save_requests(req_path, {"acc_existing": {"source": "volcCoding", "label": "old", "curl": "", "ak": "AK_OLD", "sk": "SK_OLD_LONG_VALUE", "updatedAt": "2026-01-01"}})
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({"id": "acc_existing", "label": "new-label", "curl": "", "updatedAt": "2026-07-20T14:09:09.529Z"}).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            handler.send_json = lambda payload: captured.update({"payload": payload})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(captured["payload"]["source"], "volcCoding")
            result = load_requests(req_path)["acc_existing"]
            self.assertEqual(result["label"], "new-label")
            self.assertEqual(result["curl"], "")
            self.assertEqual(result["ak"], "AK_OLD")
            self.assertEqual(result["sk"], "SK_OLD_LONG_VALUE")

    def test_update_ak_sk_on_curl_less_volc_account(self):
        """Updating AK/SK on an existing volc account with empty curl should succeed with new creds."""
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            save_requests(req_path, {"acc_existing": {"source": "volcAgent", "label": "agent", "curl": "", "ak": "AK_OLD", "sk": "SK_OLD_LONG_VALUE", "updatedAt": "2026-01-01"}})
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({"id": "acc_existing", "label": "agent", "ak": "AK_NEW", "sk": "SK_NEW_LONG_VALUE", "curl": "", "updatedAt": "2026-07-20T14:09:09.529Z"}).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            handler.send_json = lambda payload: captured.update({"payload": payload})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(captured["payload"]["source"], "volcAgent")
            result = load_requests(req_path)["acc_existing"]
            self.assertEqual(result["ak"], "AK_NEW")
            self.assertEqual(result["sk"], "SK_NEW_LONG_VALUE")


    def test_create_google_ai_account_without_curl(self):
        """POST /api/requests with source=googleAi + refreshToken + no curl should succeed."""
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({
                "source": "googleAi",
                "label": "my-google",
                "refreshToken": "1//09_long_refresh_token_value",
                "proxy": "http://proxy.example:1091",
            }).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            handler.send_json = lambda payload: captured.update({"payload": payload})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(captured["payload"]["source"], "googleAi")
            result = load_requests(req_path)
            saved = next(iter(result.values()))
            self.assertEqual(saved["source"], "googleAi")
            self.assertEqual(saved["refreshToken"], "1//09_long_refresh_token_value")
            self.assertEqual(saved["proxy"], "http://proxy.example:1091")
            self.assertEqual(saved["curl"], "")

    def test_create_google_ai_account_without_refresh_token_rejected(self):
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({"source": "googleAi", "label": "no-token"}).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            errors = {}
            handler.send_error = lambda code, msg=None: errors.update({"code": code, "msg": msg})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(errors["code"], 400)
            self.assertIn("refreshToken is required", errors["msg"])

    def test_edit_label_on_google_ai_account_preserves_token(self):
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            save_requests(req_path, {"acc_g": {"source": "googleAi", "label": "old", "curl": "", "refreshToken": "RT_OLD", "proxy": "http://p:1", "updatedAt": "2026-01-01"}})
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({"id": "acc_g", "label": "new", "curl": "", "updatedAt": "2026-07-22T00:00:00Z"}).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            handler.send_json = lambda payload: captured.update({"payload": payload})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(captured["payload"]["source"], "googleAi")
            result = load_requests(req_path)["acc_g"]
            self.assertEqual(result["label"], "new")
            self.assertEqual(result["refreshToken"], "RT_OLD")
            self.assertEqual(result["proxy"], "http://p:1")


if __name__ == "__main__":
    unittest.main()


class AccountOrderTest(unittest.TestCase):
    def test_load_order_returns_empty_when_missing(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            self.assertEqual(load_order(path), [])

    def test_save_and_load_order_round_trips(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_order(path, ["acc_1", "acc_2"])
            self.assertEqual(load_order(path), ["acc_1", "acc_2"])

    def test_load_order_coerces_ids_to_strings(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_snapshot(path, {"order": [123, "acc_2", None, 4.5]})
            self.assertEqual(load_order(path), ["123", "acc_2"])

    def test_load_order_returns_empty_when_malformed(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "order.json"
            save_snapshot(path, {"order": "not-a-list"})
            self.assertEqual(load_order(path), [])


class VolcAkSkAccountCreationTest(unittest.TestCase):
    def test_create_volg_coding_account_without_curl(self):
        """POST /api/requests with explicit source + ak + sk + no curl should succeed for volcCoding."""
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({
                "source": "volcCoding",
                "label": "my-coding-account",
                "ak": "AK_TEST",
                "sk": "SK_TEST_VALUE_LONG_ENOUGH",
            }).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            def _capture(payload):
                captured["payload"] = payload
            handler.send_json = lambda payload: _capture(payload)
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertIsNotNone(captured["payload"])
            self.assertEqual(captured["payload"]["source"], "volcCoding")
            requests = load_requests(req_path)
            self.assertEqual(len(requests), 1)
            saved_id = next(iter(requests))
            saved = requests[saved_id]
            self.assertEqual(saved["source"], "volcCoding")
            self.assertEqual(saved["ak"], "AK_TEST")
            self.assertEqual(saved["sk"], "SK_TEST_VALUE_LONG_ENOUGH")
            self.assertEqual(saved["label"], "my-coding-account")
            self.assertEqual(saved["curl"], "")

    def test_create_volc_agent_account_without_curl(self):
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({
                "source": "volcAgent",
                "label": "agent-1",
                "ak": "AK_AGENT",
                "sk": "SK_AGENT_VALUE_LONG_ENOUGH",
            }).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            captured = {}
            def _capture(payload):
                captured["payload"] = payload
            handler.send_json = lambda payload: _capture(payload)
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertIsNotNone(captured["payload"])
            self.assertEqual(captured["payload"]["source"], "volcAgent")

    def test_create_non_volc_account_without_curl_rejected(self):
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({
                "source": "minimax",
                "label": "no-curl",
                "ak": "AK_X",
                "sk": "SK_X",
            }).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            errors = {}
            handler.send_error = lambda code, msg=None: errors.update({"code": code, "msg": msg})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(errors["code"], 400)
            self.assertIn("curl is required", errors["msg"])

    def test_create_volc_account_without_ak_sk_rejected(self):
        from server import DashboardHandler
        from io import BytesIO
        import json as _json

        with tempfile.TemporaryDirectory() as tmp:
            req_path = Path(tmp) / "requests.json"
            DashboardHandler.requests_path = req_path
            DashboardHandler.snapshot_path = Path(tmp) / "snapshot.json"
            DashboardHandler.results_path = Path(tmp) / "results.json"
            DashboardHandler.credentials_path = Path(tmp) / "credentials.json"
            DashboardHandler.order_path = Path(tmp) / "order.json"

            body = _json.dumps({
                "source": "volcCoding",
                "label": "missing-ak-sk",
            }).encode()
            handler = DashboardHandler.__new__(DashboardHandler)
            handler.path = "/api/requests"
            handler.headers = {"Content-Length": str(len(body))}
            handler.rfile = BytesIO(body)
            errors = {}
            handler.send_error = lambda code, msg=None: errors.update({"code": code, "msg": msg})
            handler.wfile = BytesIO()

            DashboardHandler.do_POST(handler)

            self.assertEqual(errors["code"], 400)
            self.assertIn("ak and sk are required", errors["msg"])
