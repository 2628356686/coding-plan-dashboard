import json
import tempfile
import unittest
from pathlib import Path

from server import (
    build_http_request,
    infer_source,
    is_success_status,
    load_requests,
    load_results,
    load_order,
    load_snapshot,
    mask_secret,
    parse_curl,
    save_order,
    save_requests,
    save_results,
    save_snapshot,
    should_skip_source,
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
    def test_volc_actions_map_sources(self):
        self.assertEqual(VOLC_ACTIONS["volcAgent"], "GetAgentPlanAFPUsage")
        self.assertEqual(VOLC_ACTIONS["volcCoding"], "GetCodingPlanUsage")

    def test_mask_secret_hides_middle(self):
        self.assertEqual(mask_secret("abcd1234efgh"), "abcd****efgh")
        self.assertEqual(mask_secret("short"), "****")
        self.assertEqual(mask_secret(""), "")

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
