import importlib.util
import json
import os
import pathlib
import threading
import unittest
import urllib.error
import urllib.request
from unittest import mock

ROOT = pathlib.Path(__file__).resolve().parents[1]
SPEC = importlib.util.spec_from_file_location("merqato_hub", ROOT / "hub" / "hub.py")
hub = importlib.util.module_from_spec(SPEC)
SPEC.loader.exec_module(hub)


class ProbeSummaryTests(unittest.TestCase):
    def test_failure_names_are_complete(self):
        probes = [
            {"host": "command.merqato.digital", "code": 502, "ok": False, "ms": 10, "slow": False},
            {"host": "sanvic.palawancollective.com", "code": 0, "ok": False, "ms": 10, "slow": False},
            {"host": "susi.merqato.digital", "code": 0, "ok": False, "ms": 10, "slow": False},
            {"host": "www.palawancollective.com", "code": 526, "ok": False, "ms": 10, "slow": False},
            {"host": "www.sanvic.palawancollective.com", "code": 0, "ok": False, "ms": 10, "slow": False},
            {"host": "www.status.merqato.digital", "code": 0, "ok": False, "ms": 10, "slow": False},
            {"host": "home.merqato.digital", "code": 401, "ok": True, "ms": 10, "slow": False},
        ]

        summary = hub.summarize_probes(probes)

        self.assertEqual(summary["failing"], 6)
        self.assertEqual(len(summary["failing_hosts"]), 6)
        self.assertIn("www.sanvic.palawancollective.com", summary["failing_names"])
        self.assertIn("www.status.merqato.digital", summary["failing_names"])
        self.assertGreater(len(summary["failing_names"]), 120)

    def test_empty_failure_list(self):
        summary = hub.summarize_probes([])
        self.assertEqual(summary["failing_names"], "none")
        self.assertEqual(summary["failing_hosts"], [])


class ProjectNormalizationTests(unittest.TestCase):
    def test_inventory_rows_are_normalized_for_the_ui(self):
        rows = hub.normalize_projects([
            {
                "project": "example",
                "deploymentURL": "https://example.vercel.app",
                "repo": "owner/example",
                "repoURL": "https://github.com/owner/example",
                "vercelTeam": "team",
                "state": "error",
                "updatedAt": 123,
                "protected": True,
            }
        ])

        self.assertEqual(rows[0]["name"], "example")
        self.assertEqual(rows[0]["state"], "ERROR")
        self.assertEqual(rows[0]["provider"], "Vercel")
        self.assertEqual(rows[0]["url"], "https://example.vercel.app")
        self.assertTrue(rows[0]["protected"])

    def test_canonical_domain_is_preferred_over_deployment_url(self):
        rows = hub.normalize_projects([{
            "project": "example",
            "domains": ["example.com", "example.vercel.app"],
            "deploymentURL": "https://deployment.vercel.app",
        }])

        self.assertEqual(rows[0]["url"], "https://example.com")

    def test_unsafe_http_urls_are_rejected(self):
        for value in (
            "javascript:alert(1)",
            "https://user:password@example.com",
            "https://example.com/\njavascript:alert(1)",
        ):
            self.assertEqual(hub.safe_http_url(value), "")

    def test_malformed_rows_are_skipped_without_losing_valid_rows(self):
        rows = hub.normalize_projects([
            None,
            "not-a-row",
            {"project": "unsafe", "deploymentURL": "javascript:alert(1)", "repoURL": "data:text/html,x"},
            {"project": "valid", "deploymentURL": "https://valid.example.com"},
        ])

        self.assertEqual([row["name"] for row in rows], ["valid"])
        self.assertEqual(rows[0]["url"], "https://valid.example.com")
        self.assertEqual(rows[0]["repoUrl"], "")


class GitHubIntegrationTests(unittest.TestCase):
    def test_public_account_reads_use_authenticated_quota_when_available(self):
        with mock.patch.object(hub, "GITHUB_TOKEN", "test-token"), \
                mock.patch.object(hub, "GITHUB_ACCOUNTS", ["example"]), \
                mock.patch.object(hub, "http_json", return_value={"public_repos": 3}) as mocked:
            data = hub.github_data()

        self.assertEqual(data["public_repos_total"], 3)
        public_call = mocked.call_args_list[0]
        self.assertEqual(public_call.kwargs["headers"]["Authorization"], "Bearer test-token")


class VercelIntegrationTests(unittest.TestCase):
    def test_ready_error_and_canceled_states_are_distinct(self):
        def fake_json(url, **_kwargs):
            if "/v2/teams" in url:
                return {"teams": [{"slug": "example-team"}]}
            if "/v9/projects" in url:
                return {"projects": [{"name": "example-project"}]}
            return {"deployments": [
                {"name": "ready", "state": "READY", "target": "production", "createdAt": 1},
                {"name": "failed", "state": "ERROR", "target": "production", "createdAt": 2},
                {"name": "canceled", "state": "CANCELED", "target": "preview", "createdAt": 3},
            ]}

        with mock.patch.object(hub, "VERCEL_TOKEN", "test-token"), \
                mock.patch.object(hub, "http_json", side_effect=fake_json):
            data = hub.vercel_data()

        self.assertEqual(data["ready"], 1)
        self.assertEqual(data["errors"], 1)
        self.assertEqual(data["canceled"], 1)
        self.assertEqual(data["error_details"], [{
            "name": "failed",
            "state": "ERROR",
            "target": "production",
            "created": 2,
        }])
        summary = hub.vercel_summary(data)
        self.assertEqual(summary["vc_errors"], 1)
        self.assertEqual(summary["vc_canceled"], 1)


class AlertEndpointTests(unittest.TestCase):
    def test_http_endpoint_requires_the_exact_key_header(self):
        server = hub.ThreadingHTTPServer(("127.0.0.1", 0), hub.Handler)
        thread = threading.Thread(target=server.serve_forever, daemon=True)
        thread.start()
        url = "http://127.0.0.1:%d/alert" % server.server_port
        payload = json.dumps({"monitor": {"name": "test"}}).encode()

        def post(headers):
            request = urllib.request.Request(
                url,
                data=payload,
                headers=headers,
                method="POST",
            )
            try:
                with urllib.request.urlopen(request, timeout=3) as response:
                    return response.status, json.load(response)
            except urllib.error.HTTPError as exc:
                body = exc.read()
                exc.close()
                return exc.code, json.loads(body)

        try:
            with mock.patch.object(hub, "ALERT_KEY", "right-key"), \
                    mock.patch.object(hub, "send_alert", return_value=(True, "ok")) as sender:
                missing_status, missing_body = post({"Content-Type": "application/json"})
                wrong_status, wrong_body = post({
                    "Content-Type": "application/json",
                    "X-Hub-Key": "wrong-key",
                })
                correct_status, correct_body = post({
                    "Content-Type": "application/json",
                    "X-Hub-Key": "right-key",
                })
        finally:
            server.shutdown()
            server.server_close()
            thread.join(timeout=2)

        self.assertEqual(missing_status, 403)
        self.assertEqual(missing_body["error"], "forbidden")
        self.assertEqual(wrong_status, 403)
        self.assertEqual(wrong_body["error"], "forbidden")
        self.assertEqual(correct_status, 200)
        self.assertTrue(correct_body["ok"])
        sender.assert_called_once()


class SecurityAndEnvironmentTests(unittest.TestCase):
    def test_alert_webhook_fails_closed_without_a_key(self):
        self.assertFalse(hub.alert_key_matches("anything", ""))
        self.assertFalse(hub.alert_key_matches("", ""))
        self.assertFalse(hub.alert_key_matches("wrong", "right"))
        self.assertFalse(hub.alert_key_matches("é", "right"))
        self.assertTrue(hub.alert_key_matches("right", "right"))

    def test_integer_environment_values_use_safe_defaults(self):
        with mock.patch.dict(os.environ, {"TEST_HUB_INT": ""}, clear=False):
            self.assertEqual(hub.env_int("TEST_HUB_INT", 60), 60)
        with mock.patch.dict(os.environ, {"TEST_HUB_INT": "not-an-int"}, clear=False):
            self.assertEqual(hub.env_int("TEST_HUB_INT", 60), 60)
        with mock.patch.dict(os.environ, {"TEST_HUB_INT": "12"}, clear=False):
            self.assertEqual(hub.env_int("TEST_HUB_INT", 60), 12)


if __name__ == "__main__":
    unittest.main()

class CleanTextTests(unittest.TestCase):
    def test_html_and_oversized_values_are_rejected(self):
        page = '<!DOCTYPE html><html><head><title>Sign in</title></head></html>'
        self.assertEqual(hub.clean_text(page, 40), "")
        self.assertEqual(hub.clean_text("x" * 200, 40), "")
        self.assertEqual(hub.clean_text(None), "")
        self.assertEqual(hub.clean_text({"a": 1}), "")

    def test_plain_version_is_kept(self):
        self.assertEqual(hub.clean_text(" 4.7.6\n", 40), "4.7.6")
