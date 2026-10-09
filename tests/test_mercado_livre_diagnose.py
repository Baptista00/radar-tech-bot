"""Diagnóstico por mocks: não lê .env/cofre real e nunca acessa a rede."""

import copy
import http.client
import io
import json
import logging
from contextlib import redirect_stderr, redirect_stdout
from unittest.mock import patch
from urllib.parse import quote, urlsplit

import requests

from safety import SafeError
from services import mercado_livre_auth as oauth
from services import mercado_livre_diagnose as diag
from test_mercado_livre_auth import MemoryStore
from test_mercado_livre_catalog import response
from test_mvp import IsolatedTest


USER_ID = 123456789
OWN_ID = "MLB10001"
TOKEN = "fake-diagnostic-access"
REFRESH = "fake-diagnostic-refresh"
SECRET = "fake-diagnostic-client-secret"
THIRD = "/items/" + diag.ITEM_ID
OWN_SEARCH = f"/users/{USER_ID}/items/search"
GRANTS = "/applications/12345/grants"
USER_APPS = f"/users/{USER_ID}/applications"
POLICY = {"status": 403, "blocked_by": "PolicyAgent", "path": THIRD,
          "code": "PA_UNAUTHORIZED_RESULT_FROM_POLICIES", "message": "At least one policy returned UNAUTHORIZED."}
DENIED = {"error": "access_denied", "code": "FORBIDDEN", "message": "access to the requested resource is forbidden"}


class DiagnoseTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.config = oauth.OAuthConfig("12345", SECRET, "https://radar.test/callback")
        self.store = MemoryStore()
        self.store.saved["tokens"] = {"access_token": TOKEN, "refresh_token": REFRESH}
        self.load = self.enterContext(patch.object(oauth, "load_oauth_config", return_value=self.config))
        self.token = self.enterContext(patch.object(oauth, "get_valid_access_token", return_value=TOKEN))
        self.enterContext(patch.object(oauth, "LocalOAuthStore", return_value=self.store))
        self.sleep = self.enterContext(patch.object(diag.time, "sleep"))
        self.enterContext(patch.dict(oauth.os.environ, {"AUTO_PUBLISH": "true",
                               "TELEGRAM_BOT_TOKEN": "fake-diagnostic-telegram", "TELEGRAM_ADMIN_ID": "765432123"}))
        self.grant = {"user_id": USER_ID, "app_id": 12345, "scopes": ["read", "offline_access"],
                      "date_created": "2026-10-09T10:00:00.000-03:00", "status": "active"}
        self.routes = {
            ("/users/me", True): ({"id": USER_ID, "site_id": "MLB", "status": {"site_status": "active"},
                                   "email": "private@example.test", "first_name": "Private Name", "address": {"street": "private address"}}, 200),
            ("/applications/12345", True): ({"id": 12345, "active": True, "sandbox_mode": False,
                                             "site_id": "MLB", "certification_status": "not_certified",
                                             "client_secret": SECRET, "scopes": ["read"]}, 200),
            (GRANTS, True): ({"paging": {"total": 1, "offset": 0, "limit": 50}, "grants": [self.grant]}, 200),
            (USER_APPS, True): ([], 200),
            (THIRD, True): (copy.deepcopy(POLICY), 403),
            (THIRD, False): (copy.deepcopy(DENIED), 403),
            (OWN_SEARCH, True): ({"results": [], "paging": {"total": 0}}, 200),
        }
        self.http = self.enterContext(patch("requests.Session.get", autospec=True, side_effect=self.respond))

    def respond(self, session, url, **kwargs):
        self.assertEqual(urlsplit(url).scheme, "https")
        self.assertEqual(urlsplit(url).netloc, "api.mercadolibre.com")
        key = (urlsplit(url).path, "Authorization" in kwargs["headers"])
        self.assertIn(key, self.routes)
        payload, status = self.routes[key]
        return response(copy.deepcopy(payload), status=status)

    def run_report(self):
        return diag.diagnose(self.config, TOKEN, protected=(REFRESH, "fake-diagnostic-telegram", "765432123"))

    def cli(self):
        stdout, stderr = io.StringIO(), io.StringIO()
        with redirect_stdout(stdout), redirect_stderr(stderr):
            code = oauth.main(["diagnose"])
        return code, stdout.getvalue(), stderr.getvalue()

    def test_users_me_200_retains_only_presence_site_and_official_status(self):
        report = self.run_report()
        self.assertEqual(report["users_me"], {"http_status": 200, "user_id_present": True,
                                            "site_id": "MLB", "account_status": {"site_status": "active"}})
        text = diag.format_report(report)
        for private in (str(USER_ID), "private@example.test", "Private Name", "private address", SECRET):
            self.assertNotIn(private, text)

    def test_active_application_shows_actual_site_certification_and_sandbox(self):
        app = self.run_report()["application"]
        self.assertIs(app["active"], True)
        self.assertIs(app["sandbox_mode"], False)
        self.assertEqual(app["certification_status"], "not_certified")
        self.assertEqual(app["site_id"], "MLB")
        self.assertNotIn("client_secret", app)

    def test_inactive_application_is_classified_with_explicit_evidence(self):
        self.routes[("/applications/12345", True)][0]["active"] = False
        self.assertTrue(self.run_report()["classification"]["category"].startswith("B)"))

    def test_blocked_application_is_not_confused_with_missing_field(self):
        self.routes[("/applications/12345", True)][0]["blocked"] = True
        self.assertTrue(self.run_report()["classification"]["category"].startswith("B)"))

    def test_application_403_does_not_claim_app_inactive(self):
        self.routes[("/applications/12345", True)] = (DENIED, 403)
        report = self.run_report()
        self.assertIsNone(report["application"]["active"])
        self.assertNotIn("OAuth válido", report["classification"]["next_step"])

    def test_application_id_mismatch_ignores_other_app_metadata(self):
        self.routes[("/applications/12345", True)][0].update(id=98765, active=False)
        report = self.run_report()
        self.assertIsNone(report["application"]["active"])
        self.assertNotIn("B)", report["classification"]["category"])

    def test_grant_found_is_matched_to_both_authenticated_user_and_application(self):
        report = self.run_report()
        grant = report["grant"]
        self.assertIs(grant["found"], True)
        self.assertIs(grant["user_matches"], True)
        self.assertEqual(grant["scopes"], ["read", "offline_access"])
        self.assertEqual(grant["date_created"], self.grant["date_created"])
        text = diag.format_report(report)
        for line in ("Grant encontrado: Sim", "Usuário do grant corresponde ao /users/me: Sim",
                     "read: Sim", "offline_access: Sim", "write: Não"):
            self.assertIn(line, text)

    def test_grant_without_read_is_classified_as_scopes(self):
        self.grant["scopes"] = ["offline_access"]
        report = self.run_report()
        self.assertIs(report["grant"]["scope_presence"]["read"], False)
        self.assertTrue(report["classification"]["category"].startswith("A)"))

    def test_read_scope_does_not_require_write_for_item_reading(self):
        self.grant["scopes"] = ["read"]
        report = self.run_report()
        self.assertEqual(report["grant"]["scope_presence"], {"read": True, "offline_access": False, "write": False})
        self.assertTrue(report["classification"]["category"].startswith("F)"))

    def test_user_id_divergence_never_borrows_another_users_scopes(self):
        self.grant["user_id"] = USER_ID + 1
        self.routes[(USER_APPS, True)] = ([self.grant], 200)
        grant = self.run_report()["grant"]
        self.assertIsNone(grant["found"])
        self.assertIs(grant["user_matches"], False)
        self.assertIsNone(grant["scope_presence"]["read"])
        self.assertNotIn(str(USER_ID + 1), json.dumps(grant))

    def test_grants_unavailable_uses_documented_user_applications_resource(self):
        self.routes[(GRANTS, True)] = (DENIED, 403)
        self.routes[(USER_APPS, True)] = ([self.grant], 200)
        grant = self.run_report()["grant"]
        self.assertTrue(grant["found"])
        self.assertEqual(len(grant["queries"]), 2)
        self.assertEqual(grant["queries"][0]["http_status"], 403)

    def test_partial_grants_page_does_not_prove_absence(self):
        other = {**self.grant, "user_id": USER_ID + 1}
        self.routes[(GRANTS, True)] = ({"paging": {"total": 100, "limit": 1}, "grants": [other]}, 200)
        self.routes[(USER_APPS, True)] = ([self.grant], 200)
        self.assertTrue(self.run_report()["grant"]["found"])

    def test_grant_query_errors_remain_unknown_instead_of_missing_read(self):
        self.routes[(GRANTS, True)] = (DENIED, 403)
        self.routes[(USER_APPS, True)] = ({}, 404)
        report = self.run_report()
        self.assertIsNone(report["grant"]["found"])
        self.assertIsNone(report["grant"]["scope_presence"]["read"])
        self.assertIn("Administrar permissões", report["grant"]["note"])
        self.assertFalse(report["classification"]["category"].startswith("A)"))

    def test_grant_missing_from_complete_user_list_is_reported_as_absent(self):
        self.routes[(GRANTS, True)] = ({"grants": []}, 200)
        report = self.run_report()
        self.assertIs(report["grant"]["found"], False)
        self.assertTrue(report["classification"]["category"].startswith("A)"))

    def test_missing_or_malformed_scopes_are_unknown(self):
        for scopes in (None, "read", ["read", {"secret": SECRET}], [TOKEN]):
            with self.subTest(scope_type=type(scopes).__name__):
                self.grant["scopes"] = scopes
                report = self.run_report()
                self.assertIsNone(report["grant"]["scope_presence"]["read"])
                self.assertNotIn(TOKEN, diag.format_report(report))

    def test_item_200_is_confirmed_without_printing_body(self):
        self.routes[(THIRD, True)] = ({"id": diag.ITEM_ID, "title": "private title", "seller_address": "private address"}, 200)
        code, text, stderr = self.cli()
        self.assertEqual(code, 0)
        self.assertIn("success: Sim", text)
        self.assertNotIn("private title", text)
        self.assertNotIn("private address", text)
        self.assertEqual(stderr, "")

    def test_item_206_is_accepted_and_safe_missing_content_header_preserved(self):
        original = self.respond
        def respond(session, url, **kwargs):
            if urlsplit(url).path == THIRD and "Authorization" in kwargs["headers"]:
                return response({"id": diag.ITEM_ID}, status=206, headers={"X-Content-Missing": "seller_address,geolocation"})
            return original(session, url, **kwargs)
        self.http.side_effect = respond
        report = self.run_report()
        self.assertTrue(report["item"]["success"])
        self.assertEqual(report["item"]["headers"]["x-content-missing"], "seller_address,geolocation")

    def test_generic_403_preserves_exact_error_and_stays_inconclusive(self):
        self.routes[(THIRD, True)] = (DENIED, 403)
        report = self.run_report()
        self.assertEqual(report["item"]["message"], DENIED["message"])
        self.assertEqual(report["item"]["error"], "access_denied")
        self.assertTrue(report["classification"]["category"].startswith("G)"))

    def test_policyagent_403_with_valid_oauth_app_and_grant_recommends_support(self):
        report = self.run_report()
        self.assertEqual(report["item"]["message"], POLICY["message"])
        self.assertEqual(report["item"]["blocked_by"], "PolicyAgent")
        self.assertTrue(report["classification"]["category"].startswith("F)"))
        self.assertIn("suporte oficial", report["classification"]["next_step"])
        self.assertIn("request_id", report["classification"]["next_step"])

    def test_invalid_scopes_explicit_error_is_classified(self):
        self.routes[(THIRD, True)] = ({"error": "Invalid scopes", "code": "FORBIDDEN"}, 403)
        self.assertTrue(self.run_report()["classification"]["category"].startswith("A)"))

    def test_explicit_ip_user_and_ownership_errors_are_distinguished(self):
        for error, category in (("ip_not_allowed", "D)"), ("user_validation_required", "C)"),
                                ("caller is not the owner", "E)")):
            with self.subTest(category=category):
                self.routes[(THIRD, True)] = ({"error": error}, 403)
                self.assertTrue(self.run_report()["classification"]["category"].startswith(category))

    def test_official_inactive_user_status_is_reported(self):
        self.routes[("/users/me", True)][0]["status"]["site_status"] = "inactive"
        self.assertTrue(self.run_report()["classification"]["category"].startswith("C)"))

    def test_unconfirmed_email_and_generic_required_action_do_not_explain_item_403(self):
        self.routes[("/users/me", True)][0]["status"].update(confirmed_email=False, required_action="verify_email")
        self.routes[(THIRD, True)] = (DENIED, 403)
        self.assertTrue(self.run_report()["classification"]["category"].startswith("G)"))

    def test_classification_never_uses_request_id_or_negated_message_as_cause(self):
        self.routes[(THIRD, True)] = ({**DENIED, "request_id": "ip_blocked", "message": "No invalid scopes were found."}, 403)
        self.assertTrue(self.run_report()["classification"]["category"].startswith("G)"))

    def test_no_own_item_has_exact_message(self):
        report = self.run_report()
        self.assertFalse(report["own_item"]["available"])
        self.assertIn("Não há item próprio disponível para comparação.", diag.format_report(report))

    def test_own_item_200_is_compared_but_does_not_prove_general_third_party_ban(self):
        self.routes[(THIRD, True)] = (DENIED, 403)
        self.routes[(OWN_SEARCH, True)] = ({"results": [OWN_ID]}, 200)
        self.routes[("/items/" + OWN_ID, True)] = ({"id": OWN_ID, "seller_id": USER_ID}, 200)
        report = self.run_report()
        self.assertTrue(report["own_item"]["owner_confirmed"])
        self.assertIn("Item próprio: HTTP 200", diag.format_report(report))
        self.assertIn("Item terceiro: HTTP 403", diag.format_report(report))
        self.assertTrue(report["classification"]["category"].startswith("G)"))

    def test_failed_own_search_never_claims_no_own_items_exist(self):
        self.routes[(OWN_SEARCH, True)] = (DENIED, 403)
        report = self.run_report()
        self.assertIsNone(report["own_item"]["available"])
        self.assertNotIn("Não há item próprio", diag.format_report(report))

    def test_same_own_item_as_target_never_repeats_authenticated_item_get(self):
        self.routes[(OWN_SEARCH, True)] = ({"results": [diag.ITEM_ID]}, 200)
        self.run_report()
        calls = [c for c in self.http.call_args_list if c.args[1] == diag.API + THIRD and "Authorization" in c.kwargs["headers"]]
        self.assertEqual(len(calls), 1)

    def test_missing_optional_response_fields_are_not_invented(self):
        self.routes[("/users/me", True)] = ({"id": USER_ID}, 200)
        self.routes[("/applications/12345", True)] = ({"id": 12345}, 200)
        self.routes[(THIRD, True)] = ({}, 403)
        report = self.run_report()
        self.assertNotIn("account_status", report["users_me"])
        self.assertIsNone(report["application"]["active"])
        self.assertEqual(report["item"], {"http_status": 403})
        self.assertTrue(report["classification"]["category"].startswith("G)"))

    def test_without_token_uses_one_clean_session_and_no_auth_cookies_or_netrc(self):
        self.run_report()
        self.assertEqual(self.http.call_count, 6)
        sessions = []
        anonymous_calls = []
        for invocation in self.http.call_args_list:
            session, url = invocation.args
            sessions.append(session)
            self.assertFalse(session.trust_env)
            self.assertTrue(session.verify)
            self.assertFalse(invocation.kwargs["allow_redirects"])
            self.assertEqual(invocation.kwargs["timeout"], (5, 20))
            self.assertFalse(session.cookies)
            self.assertIsNone(session.auth)
            self.assertNotIn(TOKEN, url)
            self.assertNotIn("Cookie", invocation.kwargs["headers"])
            if "Authorization" not in invocation.kwargs["headers"]:
                anonymous_calls.append(invocation)
            else:
                self.assertEqual(invocation.kwargs["headers"]["Authorization"], "Bearer " + TOKEN)
        self.assertEqual(len({id(session) for session in sessions}), 6)
        self.assertEqual(len(anonymous_calls), 1)
        self.assertEqual(anonymous_calls[0].args[1], diag.API + THIRD)

    def test_without_token_success_is_observation_only(self):
        self.routes[(THIRD, False)] = ({"id": diag.ITEM_ID}, 200)
        report = self.run_report()
        self.assertEqual(report["without_token"], {"http_status": 200})
        self.assertIn("não é solução de produção", report["public_comparison"])

    def test_429_stops_further_requests_without_retry(self):
        self.routes[("/users/me", True)] = ({}, 429)
        report = self.run_report()
        self.http.assert_called_once()
        self.assertIsNone(report["item"]["http_status"])
        self.assertIn("429", report["item"]["note"])

    def test_network_tls_and_invalid_json_do_not_echo_raw_data(self):
        for failure in (requests.exceptions.SSLError(TOKEN), requests.Timeout(SECRET), ValueError(REFRESH)):
            with self.subTest(kind=type(failure).__name__):
                if isinstance(failure, requests.RequestException):
                    self.http.side_effect = failure
                else:
                    bad = response()
                    bad._content = b"private-body-not-json"
                    self.http.side_effect = None
                    self.http.return_value = bad
                code, text, stderr = self.cli()
                self.assertEqual(code, 1)
                self.assertIn("G) INCONCLUSIVO", text)
                for secret in (TOKEN, SECRET, REFRESH, "private-body-not-json"):
                    self.assertNotIn(secret, text + stderr)

    def test_raw_403_safe_fields_headers_and_nested_cause_survive_redaction(self):
        original = self.respond
        def respond(session, url, **kwargs):
            if urlsplit(url).path == THIRD and "Authorization" in kwargs["headers"]:
                return response({**POLICY, "request_id": "trace-123", "cause": [{"code": "policy_denied", "message": "Safe explanation",
                    "access_token": TOKEN, "refresh_token": REFRESH, "seller_address": "private address"}]}, status=403,
                    headers={"X-Request-ID": "trace-456", "Retry-After": "10", "X-Content-Missing": "location",
                             "Authorization": "Bearer " + TOKEN, "Set-Cookie": "private-cookie"})
            return original(session, url, **kwargs)
        self.http.side_effect = respond
        report = self.run_report()
        self.assertEqual(report["item"]["cause"], [{"code": "policy_denied", "message": "Safe explanation"}])
        self.assertEqual(report["item"]["request_id"], "trace-123")
        self.assertEqual(report["item"]["headers"], {"x-request-id": "trace-456", "retry-after": "10", "x-content-missing": "location"})
        text = diag.format_report(report)
        for forbidden in ("Authorization", "Set-Cookie", "private-cookie", TOKEN, REFRESH, "private address"):
            self.assertNotIn(forbidden, text)

    def test_credentials_are_redacted_even_when_echoed_in_allowed_error_fields(self):
        private = (TOKEN, REFRESH, SECRET, "fake-diagnostic-telegram", "765432123")
        self.routes[(THIRD, True)] = ({**POLICY, "message": " | ".join(private),
            "error": "refresh_token=unexpected-credential", "code": "Bearer unexpected-access",
            "cause": [{"message": "Authorization: Basic bad-secret", "code": 765432123}],
            "path": THIRD + "?client_secret=" + quote(SECRET)}, 403)
        code, text, stderr = self.cli()
        self.assertEqual(code, 1)
        for forbidden in (*private, "unexpected-credential", "unexpected-access", "bad-secret"):
            self.assertNotIn(forbidden, text + stderr)

    def test_cli_suppresses_library_logs_header_debug_and_restores_settings(self):
        original = self.respond
        def respond(session, url, **kwargs):
            self.assertEqual(http.client.HTTPConnection.debuglevel, 0)
            self.assertEqual(http.client.HTTPSConnection.debuglevel, 0)
            logging.getLogger("urllib3").critical("Authorization %s", TOKEN)
            return original(session, url, **kwargs)
        self.http.side_effect = respond
        old_disable = logging.root.manager.disable
        with patch.object(http.client.HTTPConnection, "debuglevel", 1), \
             patch.object(http.client.HTTPSConnection, "debuglevel", 1), self.assertNoLogs("urllib3", level=logging.DEBUG):
            code, text, stderr = self.cli()
            self.assertEqual(http.client.HTTPConnection.debuglevel, 1)
            self.assertEqual(http.client.HTTPSConnection.debuglevel, 1)
        self.assertEqual(logging.root.manager.disable, old_disable)
        self.assertEqual(code, 1)
        self.assertNotIn(TOKEN, text + stderr)
        self.token.assert_called_once_with()

    def test_unexpected_or_configuration_failure_has_no_exception_details(self):
        for failure in (SafeError(TOKEN), RuntimeError(SECRET)):
            with self.subTest(kind=type(failure).__name__):
                self.load.side_effect = failure
                code, text, stderr = self.cli()
                self.assertEqual(code, 1)
                self.assertIn("G) INCONCLUSIVO", text)
                self.assertNotIn(TOKEN, text + stderr)
                self.assertNotIn(SECRET, text + stderr)
        self.http.assert_not_called()

    def test_diagnose_never_instantiates_pipeline_database_or_telegram(self):
        with patch("main.run", side_effect=AssertionError("pipeline proibido")), \
             patch("database.Database", side_effect=AssertionError("banco proibido")), \
             patch("telegram_service.requests.post", side_effect=AssertionError("Telegram proibido")) as post:
            code, text, stderr = self.cli()
        self.assertEqual(code, 1)
        self.assertIn("AUTO_PUBLISH=false", text)
        self.assertIn("Telegram: 0 mensagens", text)
        self.assertEqual(stderr, "")
        post.assert_not_called()
