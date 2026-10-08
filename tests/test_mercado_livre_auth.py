"""OAuth com credenciais sinteticas, HTTP simulado e leitura de .env bloqueada."""

import copy
import http.client
import io
import json
import logging
import os
import secrets
import threading
import unittest
from contextlib import contextmanager, redirect_stderr, redirect_stdout
from dataclasses import replace
from pathlib import Path
from unittest.mock import Mock, patch
from urllib.parse import parse_qs, urlencode, urlsplit

import requests

from safety import SafeError
from services import mercado_livre_auth as oauth
from services.meli_oauth_store import LocalOAuthStore, WINDOWS_HEADER
from test_mvp import IsolatedTest, test_directory


class MemoryStore:
    def __init__(self):
        self.saved = {"version": 1}
        self.mutex = threading.Lock()

    @contextmanager
    def locked(self):
        if not self.mutex.acquire(blocking=False):
            raise SafeError("OAuth ocupado.")
        try:
            yield
        except OSError:
            raise SafeError("OAuth: falha de armazenamento; detalhes omitidos.") from None
        finally:
            self.mutex.release()

    def load(self):
        return copy.deepcopy(self.saved)

    def save(self, saved):
        self.saved = copy.deepcopy(saved)


class OAuthBase(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.enterContext(patch("services.mercado_livre_auth.load_dotenv",
                                side_effect=AssertionError(".env proibido nos testes")))
        self.config = oauth.OAuthConfig("12345", "fake-client-secret", "https://radar.test/oauth/callback")
        self.clock = Mock(return_value=1000.0)
        self.store = MemoryStore()
        self.auth = oauth.MercadoLivreAuth(self.config, self.store, clock=self.clock)
        self.http = self.enterContext(patch("services.mercado_livre_auth.requests.Session.post"))
        self.http.return_value = self.response()

    def response(self, *, status=200, **changes):
        payload = dict(access_token="fake-access-one", refresh_token="fake-refresh-one",
                       token_type="bearer", expires_in=21600)
        payload.update(changes)
        return Mock(status_code=status, json=Mock(return_value=payload))

    def callback(self, **changes):
        params = {"code": "fake-authorization-code", "state": self.store.saved["pending"]["state"]}
        params.update(changes)
        return self.config.redirect_uri + "?" + urlencode(params)

    def grant(self):
        self.auth.authorize()
        self.auth.exchange(self.callback())
        self.http.reset_mock()


class PKCEAndCallbackTests(OAuthBase):
    def test_pkce_rfc7636_public_vector(self):
        # Vetor publico do apendice B da RFC; nao e credencial de uma conta.
        verifier = "dBjftJeZ4CVP-mB92K27uhbUJU1p1r_wW1gFWFOEjXk"
        self.assertEqual(oauth.create_code_challenge(verifier), "E9Melhoa2OwvFrEMTJguCHaoeK1t8URWbuGJSstw-cM")

    def test_pkce_generator_and_state_use_secrets(self):
        with patch.object(oauth.secrets, "token_urlsafe", wraps=secrets.token_urlsafe) as random:
            url = self.auth.authorize()
        self.assertEqual([c.args[0] for c in random.call_args_list], [64, 32])
        pending = self.store.saved["pending"]
        self.assertRegex(pending["code_verifier"], r"^[A-Za-z0-9._~-]{43,128}$")
        self.assertGreaterEqual(len(pending["state"]), 43)
        query = parse_qs(urlsplit(url).query)
        self.assertEqual(query["code_challenge_method"], ["S256"])
        self.assertEqual(query["response_type"], ["code"])
        self.assertEqual(query["code_challenge"], [oauth.create_code_challenge(pending["code_verifier"])])
        self.assertEqual(url.split("?")[0], oauth.AUTHORIZATION_URL)
        self.assertNotIn("client_secret", query)
        self.assertNotIn(pending["code_verifier"], url)
        self.assertNotIn(self.config.client_secret, url)
        self.http.assert_not_called()

    def test_verifier_invalid_length_or_alphabet(self):
        for verifier in ("a" * 42, "a" * 129, "a" * 42 + "+", "a" * 42 + "é"):
            with self.assertRaises(SafeError):
                oauth.create_code_challenge(verifier)

    def test_state_constant_time_and_rejects_missing_or_wrong(self):
        with patch.object(oauth.secrets, "compare_digest", wraps=secrets.compare_digest) as compare:
            oauth.validate_state("synthetic-state", "synthetic-state")
            compare.assert_called_once()
        for received in ("", "wrong-state", "nao-ascii-é"):
            with self.assertRaises(SafeError):
                oauth.validate_state(received, "synthetic-state")

    def test_callback_decodes_code_without_logging_it(self):
        callback = oauth.parse_callback(self.config.redirect_uri + "?code=fake%2Bcode&state=fake-state", self.config.redirect_uri)
        self.assertEqual(callback.code, "fake+code")
        self.assertEqual(callback.state, "fake-state")
        self.assertNotIn("fake+code", repr(callback))

    def test_callback_rejects_wrong_endpoint_credentials_fragments_and_duplicates(self):
        urls = [
            "http://radar.test/oauth/callback?code=x&state=y",
            "https://other.test/oauth/callback?code=x&state=y",
            "https://radar.test/other?code=x&state=y",
            "https://private@radar.test/oauth/callback?code=x&state=y",
            self.config.redirect_uri + "?code=x&state=y#fragment",
            self.config.redirect_uri + "?code=x&code=z&state=y",
            self.config.redirect_uri + "?code=x&state=y&state=z",
            self.config.redirect_uri + "?code=x&error=denied&state=y",
            self.config.redirect_uri + "?code=x",
            self.config.redirect_uri + "?code=&state=y",
            self.config.redirect_uri + "?code=%GG&state=y",
            self.config.redirect_uri + "?code=x&state=",
        ]
        for url in urls:
            with self.assertRaises(SafeError):
                oauth.parse_callback(url, self.config.redirect_uri)
        self.http.assert_not_called()

    def test_wrong_state_does_not_consume_legitimate_pending_flow(self):
        self.auth.authorize()
        pending = copy.deepcopy(self.store.saved["pending"])
        with self.assertRaises(SafeError):
            self.auth.exchange(self.callback(state="wrong"))
        self.assertEqual(self.store.saved["pending"], pending)
        self.http.assert_not_called()

    def test_expired_pending_flow_is_deleted(self):
        self.auth.authorize()
        callback = self.callback()
        self.clock.return_value += oauth.PENDING_TTL_SECONDS
        with self.assertRaises(SafeError):
            self.auth.exchange(callback)
        self.assertNotIn("pending", self.store.saved)
        self.http.assert_not_called()

    def test_changed_application_or_redirect_rejects_exchange(self):
        self.auth.authorize()
        for config in (replace(self.config, client_id="98765"), replace(self.config, redirect_uri="https://other.test/oauth/callback")):
            auth = oauth.MercadoLivreAuth(config, self.store, clock=self.clock)
            callback = self.callback().replace(self.config.redirect_uri, config.redirect_uri)
            with self.assertRaises(SafeError):
                auth.exchange(callback)
        self.http.assert_not_called()

    def test_provider_denial_checks_state_and_does_not_echo_description(self):
        self.auth.authorize()
        url = self.config.redirect_uri + "?" + urlencode({"error": "access_denied", "error_description": "fake-private-text",
                                                           "state": self.store.saved["pending"]["state"]})
        with self.assertRaises(SafeError) as caught:
            self.auth.exchange(url)
        self.assertNotIn("fake-private-text", str(caught.exception))
        self.assertNotIn("pending", self.store.saved)
        self.http.assert_not_called()


class ExchangeAndRefreshTests(OAuthBase):
    def test_exchange_parameters_only_in_body_and_state_cannot_replay(self):
        self.auth.authorize()
        verifier = self.store.saved["pending"]["code_verifier"]
        callback = self.callback()
        self.auth.exchange(callback)
        args, kwargs = self.http.call_args
        self.assertEqual(args, (oauth.TOKEN_URL,))
        self.assertNotIn("params", kwargs)
        self.assertEqual(kwargs["data"]["code_verifier"], verifier)
        self.assertEqual(kwargs["data"]["client_secret"], self.config.client_secret)
        self.assertEqual(kwargs["data"]["grant_type"], "authorization_code")
        self.assertEqual(kwargs["data"]["redirect_uri"], self.config.redirect_uri)
        self.assertEqual(kwargs["timeout"], (5, 20))
        self.assertFalse(kwargs["allow_redirects"])
        self.assertNotIn("pending", self.store.saved)
        with self.assertRaises(SafeError):
            self.auth.exchange(callback)
        self.assertEqual(self.http.call_count, 1)

    def test_valid_access_token_returns_without_http(self):
        self.grant()
        self.assertEqual(self.auth.get_valid_access_token(), "fake-access-one")
        self.http.assert_not_called()

    def test_refresh_rotates_both_tokens_and_next_refresh_uses_new_pair(self):
        self.grant()
        self.clock.return_value = self.store.saved["tokens"]["refresh_after"]
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        self.assertEqual(self.auth.get_valid_access_token(), "fake-access-two")
        self.assertEqual(self.http.call_args.kwargs["data"]["refresh_token"], "fake-refresh-one")
        self.assertEqual(self.http.call_args.kwargs["data"]["grant_type"], "refresh_token")
        self.clock.return_value = self.store.saved["tokens"]["expires_at"]
        self.http.return_value = self.response(access_token="fake-access-three", refresh_token="fake-refresh-three")
        self.assertEqual(self.auth.get_valid_access_token(), "fake-access-three")
        self.assertEqual(self.http.call_args.kwargs["data"]["refresh_token"], "fake-refresh-two")
        self.assertNotIn("fake-refresh-one", str(self.store.saved))

    def test_explicit_refresh_can_handle_early_revocation(self):
        self.grant()
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        self.assertEqual(self.auth.get_valid_access_token(force_refresh=True), "fake-access-two")
        self.http.assert_called_once()

    def test_timeout_consumes_refresh_and_cannot_retry_after_restart(self):
        self.grant()
        self.http.side_effect = requests.Timeout("fake-client-secret fake-refresh-one")
        with self.assertRaises(SafeError) as caught:
            self.auth.get_valid_access_token(force_refresh=True)
        self.assertNotIn("fake-refresh-one", str(caught.exception))
        restarted = oauth.MercadoLivreAuth(self.config, self.store, clock=self.clock)
        with self.assertRaises(SafeError):
            restarted.get_valid_access_token()
        self.assertNotIn("tokens", self.store.saved)
        self.assertEqual(self.http.call_count, 1)

    def test_exchange_connection_error_cannot_reuse_code(self):
        self.auth.authorize()
        callback = self.callback()
        self.http.side_effect = requests.ConnectionError("fake-private-transport-detail")
        with self.assertRaises(SafeError) as caught:
            self.auth.exchange(callback)
        self.assertNotIn("fake-private", str(caught.exception))
        with self.assertRaises(SafeError):
            self.auth.exchange(callback)
        self.assertEqual(self.http.call_count, 1)

    def test_http_error_statuses_are_safe_without_echoing_response(self):
        for status in (301, 400, 401, 403, 429, 500, 503):
            self.auth.authorize()
            self.http.return_value = self.response(status=status, error_description="fake-private-error")
            with self.assertRaises(SafeError) as caught:
                self.auth.exchange(self.callback())
            self.assertEqual(caught.exception.status, status)
            self.assertNotIn("fake-private", str(caught.exception))
            self.http.return_value.json.assert_not_called()

    def test_invalid_success_payloads_do_not_restore_consumed_refresh(self):
        invalid = [None, [], {}, {"refresh_token": "fake-refresh-two"}]
        for payload in invalid:
            self.http.return_value = self.response()
            self.grant()
            self.http.return_value.json.return_value = payload
            with self.assertRaises(SafeError):
                self.auth.get_valid_access_token(force_refresh=True)
            self.assertNotIn("tokens", self.store.saved)

    def test_malformed_json_error_is_sanitized(self):
        self.auth.authorize()
        self.http.return_value.json.side_effect = ValueError("fake-secret-json-body")
        with self.assertRaises(SafeError) as caught:
            self.auth.exchange(self.callback())
        self.assertNotIn("fake-secret", str(caught.exception))

    def test_unchanged_refresh_or_invalid_expiration_rejected(self):
        for changes in ({}, {"refresh_token": None}, {"refresh_token": "fake-refresh-two", "expires_in": 0},
                        {"refresh_token": "fake-refresh-two", "expires_in": True},
                        {"refresh_token": "fake-refresh-two", "token_type": "other"}):
            self.http.return_value = self.response()
            self.grant()
            self.http.return_value = self.response(**changes)
            with self.assertRaises(SafeError):
                self.auth.get_valid_access_token(force_refresh=True)
            self.assertTrue(self.store.saved["reauthorize_required"])

    def test_failure_to_persist_consumption_prevents_http(self):
        self.grant()
        with patch.object(self.store, "save", side_effect=OSError("fake-private-disk-error")):
            with self.assertRaises(SafeError):
                self.auth.get_valid_access_token(force_refresh=True)
        self.http.assert_not_called()

    def test_token_expired_during_response_is_never_returned(self):
        self.grant()
        self.http.return_value = self.response(refresh_token="fake-refresh-two", expires_in=1)
        self.clock.side_effect = [1000.0, 1002.0]
        with self.assertRaises(SafeError):
            self.auth.get_valid_access_token(force_refresh=True)
        self.assertTrue(self.store.saved["reauthorize_required"])

    def test_failure_to_save_rotated_pair_never_reuses_old_refresh(self):
        self.grant()
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        save = self.store.save

        def fail_on_tokens(saved):
            if "tokens" in saved:
                raise OSError("fake-private-disk-error")
            save(saved)

        with patch.object(self.store, "save", side_effect=fail_on_tokens):
            with self.assertRaises(SafeError):
                self.auth.get_valid_access_token(force_refresh=True)
        with self.assertRaises(SafeError):
            self.auth.get_valid_access_token()
        self.assertEqual(self.http.call_count, 1)

    def test_lock_prevents_simultaneous_refresh(self):
        self.grant()
        with self.store.locked():
            with self.assertRaises(SafeError):
                self.auth.get_valid_access_token(force_refresh=True)
        self.http.assert_not_called()

    def test_tokens_bound_to_client_and_missing_tokens_need_authorization(self):
        with self.assertRaises(SafeError):
            self.auth.get_valid_access_token()
        self.grant()
        other = oauth.MercadoLivreAuth(replace(self.config, client_id="98765"), self.store, clock=self.clock)
        with self.assertRaises(SafeError):
            other.get_valid_access_token()
        self.http.assert_not_called()

    def test_success_logs_never_include_credentials_codes_or_verifier(self):
        self.auth.authorize()
        pending = copy.deepcopy(self.store.saved["pending"])
        with self.assertLogs(oauth.logger, level=logging.INFO) as captured:
            self.auth.exchange(self.callback())
            self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
            self.auth.get_valid_access_token(force_refresh=True)
        logs = "\n".join(captured.output)
        for secret in (self.config.client_secret, "fake-authorization-code", pending["state"], pending["code_verifier"],
                       "fake-access-one", "fake-refresh-one", "fake-access-two", "fake-refresh-two"):
            self.assertNotIn(secret, logs)


class OAuthCLIAndConfigTests(OAuthBase):
    def test_config_validates_without_env_and_repr_hides_secrets(self):
        config = oauth.load_oauth_config({"MELI_CLIENT_ID": self.config.client_id,
                                          "MELI_CLIENT_SECRET": self.config.client_secret,
                                          "MELI_REDIRECT_URI": self.config.redirect_uri})
        self.assertEqual(config, self.config)
        self.assertNotIn(self.config.client_secret, repr(config))
        for values in ({}, {"MELI_CLIENT_ID": "seu_client_id"},
                       {"MELI_CLIENT_ID": "12345", "MELI_CLIENT_SECRET": "seu_client_secret"}):
            with self.assertRaises(SafeError):
                oauth.load_oauth_config(values)

    def test_config_rejects_redirect_query_and_userinfo(self):
        for redirect in ("https://radar.test/callback?private=yes", "https://private@radar.test/callback",
                         "http://radar.test/callback", "https://seu-dominio.example/oauth/callback"):
            with self.assertRaises(SafeError):
                oauth.load_oauth_config({"MELI_CLIENT_ID": self.config.client_id,
                                         "MELI_CLIENT_SECRET": self.config.client_secret,
                                         "MELI_REDIRECT_URI": redirect})

    def cli(self, arguments, *, auth=None):
        stdout, stderr = io.StringIO(), io.StringIO()
        with patch.object(oauth, "load_oauth_config", return_value=self.config), \
                patch.object(oauth, "MercadoLivreAuth", return_value=auth or self.auth), \
                patch.object(oauth, "configure_logging"), redirect_stdout(stdout), redirect_stderr(stderr):
            code = oauth.main(arguments)
        return code, stdout.getvalue(), stderr.getvalue()

    def test_authorize_cli_outputs_only_authorization_url(self):
        code, stdout, stderr = self.cli(["authorize"])
        self.assertEqual(code, 0)
        self.assertEqual(len(stdout.splitlines()), 1)
        self.assertTrue(stdout.startswith(oauth.AUTHORIZATION_URL + "?"))
        self.assertEqual(stderr, "")
        self.assertNotIn(self.config.client_secret, stdout)
        self.http.assert_not_called()

    def test_exchange_cli_callback_argument_and_hidden_input(self):
        for hidden in (False, True):
            self.auth.authorize()
            callback = self.callback()
            args = ["exchange"] if hidden else ["exchange", "--callback-url", callback]
            with patch.object(oauth.getpass, "getpass", return_value=callback) as prompt:
                code, stdout, stderr = self.cli(args)
            self.assertEqual(code, 0)
            self.assertNotIn(callback, stdout + stderr)
            self.assertNotIn("fake-access-one", stdout + stderr)
            self.assertEqual(prompt.call_count, int(hidden))

    def test_cli_errors_do_not_echo_arguments_exception_or_provider_body(self):
        code, stdout, stderr = self.cli(["exchange", "--unknown", "fake-sensitive-callback"])
        self.assertEqual(code, 1)
        self.assertNotIn("fake-sensitive", stdout + stderr)
        broken = Mock()
        broken.authorize.side_effect = RuntimeError("fake-private-exception")
        code, stdout, stderr = self.cli(["authorize"], auth=broken)
        self.assertEqual(code, 1)
        self.assertNotIn("fake-private", stdout + stderr)

    def test_hidden_input_never_falls_back_to_echo(self):
        with patch.object(oauth.getpass, "getpass", side_effect=oauth.getpass.GetPassWarning("fake-secret")):
            code, stdout, stderr = self.cli(["exchange"])
        self.assertEqual(code, 1)
        self.assertNotIn("fake-secret", stdout + stderr)

    def test_cli_refresh_does_not_print_access_token(self):
        self.grant()
        code, stdout, stderr = self.cli(["refresh"])
        self.assertEqual(code, 0)
        self.assertNotIn("fake-access-one", stdout + stderr)
        self.http.assert_not_called()

    def test_reusable_function_delegates_without_printing(self):
        auth = Mock()
        auth.get_valid_access_token.return_value = "fake-access-one"
        with patch.object(oauth, "load_oauth_config", return_value=self.config), \
                patch.object(oauth, "MercadoLivreAuth", return_value=auth), redirect_stdout(io.StringIO()) as stdout:
            token = oauth.get_valid_access_token()
        self.assertEqual(token, "fake-access-one")
        self.assertEqual(stdout.getvalue(), "")
        auth.get_valid_access_token.assert_called_once_with(force_refresh=False)

    def test_verify_outputs_only_status_and_authentication(self):
        self.grant()
        response = Mock(status_code=200)
        response.json.return_value = {"id": 42, "nickname": "fake-private-account", "email": "private@example.test"}
        with patch.object(oauth, "get_valid_access_token", wraps=oauth.get_valid_access_token) as get_token, \
                patch("requests.Session.get", return_value=response) as get:
            code, stdout, stderr = self.cli(["verify"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"http_status": 200, "authenticated": True})
        self.assertEqual(stderr, "")
        get_token.assert_called_once_with()
        self.assertEqual(get.call_args.args, (oauth.USERS_ME_URL,))
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fake-access-one")
        self.assertEqual(get.call_args.kwargs["timeout"], (5, 20))
        self.assertFalse(get.call_args.kwargs["allow_redirects"])
        self.assertNotIn("params", get.call_args.kwargs)
        response.close.assert_called_once()
        self.http.assert_not_called()

    def test_verify_automatically_refreshes_expired_token_and_saves_both(self):
        self.grant()
        self.clock.return_value = self.store.saved["tokens"]["expires_at"]
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        response = Mock(status_code=200, json=Mock(return_value={"id": 42}))
        with patch("requests.Session.get", return_value=response) as get:
            code, stdout, stderr = self.cli(["verify"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"http_status": 200, "authenticated": True})
        self.assertEqual(stderr, "")
        self.assertEqual(get.call_args.kwargs["headers"]["Authorization"], "Bearer fake-access-two")
        self.assertEqual(self.store.saved["tokens"]["access_token"], "fake-access-two")
        self.assertEqual(self.store.saved["tokens"]["refresh_token"], "fake-refresh-two")
        self.http.assert_called_once()

    def test_verify_401_refreshes_once_and_retries_users_me(self):
        self.grant()
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        denied = Mock(status_code=401)
        accepted = Mock(status_code=200, json=Mock(return_value={"id": 42}))
        with patch("requests.Session.get", side_effect=[denied, accepted]) as get:
            code, stdout, stderr = self.cli(["verify"])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"http_status": 200, "authenticated": True})
        self.assertEqual(stderr, "")
        self.assertEqual(get.call_count, 2)
        self.http.assert_called_once()
        self.assertEqual(self.http.call_args.kwargs["data"]["refresh_token"], "fake-refresh-one")
        self.assertEqual(self.store.saved["tokens"]["refresh_token"], "fake-refresh-two")

    def test_verify_persistent_401_has_no_refresh_loop(self):
        self.grant()
        self.http.return_value = self.response(access_token="fake-access-two", refresh_token="fake-refresh-two")
        with patch("requests.Session.get", return_value=Mock(status_code=401)) as get:
            code, stdout, stderr = self.cli(["verify"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), {"http_status": 401, "authenticated": False})
        self.assertEqual(stderr, "")
        self.assertEqual(get.call_count, 2)
        self.http.assert_called_once()

    def test_verify_other_http_errors_do_not_refresh_or_dump_body(self):
        self.grant()
        for status in (302, 403, 429, 500):
            response = Mock(status_code=status, json=Mock(return_value={"private": "fake-private-response"}))
            with patch("requests.Session.get", return_value=response) as get:
                code, stdout, stderr = self.cli(["verify"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout), {"http_status": status, "authenticated": False})
            self.assertEqual(stderr, "")
            response.json.assert_not_called()
            get.assert_called_once()
        self.http.assert_not_called()

    def test_verify_network_and_tls_errors_have_no_traceback_or_insecure_retry(self):
        self.grant()
        for error in (requests.Timeout, requests.exceptions.ProxyError, requests.exceptions.SSLError):
            with patch("requests.Session.get", side_effect=error("fake-private-header-or-token")) as get:
                code, stdout, stderr = self.cli(["verify"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout), {"http_status": None, "authenticated": False})
            self.assertEqual(stderr, "")
            get.assert_called_once()
            self.assertNotIn("verify", get.call_args.kwargs)
        self.http.assert_not_called()

    def test_verify_failed_refresh_preserves_users_me_status_without_leaking_error(self):
        self.grant()
        self.http.side_effect = requests.Timeout("fake-private-token-response")
        with patch("requests.Session.get", return_value=Mock(status_code=401)) as get:
            code, stdout, stderr = self.cli(["verify"])
        self.assertEqual(code, 1)
        self.assertEqual(json.loads(stdout), {"http_status": 401, "authenticated": False})
        self.assertEqual(stderr, "")
        self.assertTrue(self.store.saved["reauthorize_required"])
        get.assert_called_once()
        self.http.assert_called_once()

    def test_verify_bad_200_payload_never_claims_authentication(self):
        self.grant()
        for payload in (None, [], {}, {"id": True}, {"id": "42"}, {"id": 0}):
            response = Mock(status_code=200, json=Mock(return_value=payload))
            with patch("requests.Session.get", return_value=response):
                code, stdout, stderr = self.cli(["verify"])
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout), {"http_status": 200, "authenticated": False})
            self.assertEqual(stderr, "")

    def test_verify_disables_header_debug_and_logs_then_restores_settings(self):
        self.grant()

        def get(*args, **kwargs):
            self.assertEqual(http.client.HTTPConnection.debuglevel, 0)
            self.assertEqual(http.client.HTTPSConnection.debuglevel, 0)
            logging.getLogger("urllib3").critical("fake-private-header-or-token")
            return Mock(status_code=200, json=Mock(return_value={"id": 42}))

        old_logging = logging.root.manager.disable
        with patch.object(http.client.HTTPConnection, "debuglevel", 1), \
                patch.object(http.client.HTTPSConnection, "debuglevel", 2), \
                patch("requests.Session.get", side_effect=get), self.assertNoLogs("urllib3", level=logging.DEBUG):
            code, stdout, stderr = self.cli(["verify"])
            self.assertEqual(http.client.HTTPConnection.debuglevel, 1)
            self.assertEqual(http.client.HTTPSConnection.debuglevel, 2)
        self.assertEqual(logging.root.manager.disable, old_logging)
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(stdout), {"http_status": 200, "authenticated": True})
        self.assertEqual(stderr, "")

    def test_verify_unexpected_or_config_error_does_not_expose_secrets(self):
        broken = Mock()
        for error in (SafeError("fake-private-config-error"), RuntimeError("fake-private-error")):
            broken.get_valid_access_token.side_effect = error
            code, stdout, stderr = self.cli(["verify"], auth=broken)
            self.assertEqual(code, 1)
            self.assertEqual(json.loads(stdout), {"http_status": None, "authenticated": False})
            self.assertEqual(stderr, "")
        self.http.assert_not_called()


class OAuthStoreTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(test_directory())
        self.store = LocalOAuthStore(directory / "oauth")

    def test_secure_store_round_trip_and_permissions_or_encryption(self):
        saved = {"version": 1, "tokens": {"access_token": "fake-private-access", "refresh_token": "fake-private-refresh"}}
        with self.store.locked():
            self.store.save(saved)
            self.assertEqual(self.store.load(), saved)
        raw = self.store.path.read_bytes()
        if os.name == "nt":
            self.assertTrue(raw.startswith(WINDOWS_HEADER))
            self.assertNotIn(b"fake-private-access", raw)
            self.assertNotIn(b"fake-private-refresh", raw)
        else:
            self.assertEqual(self.store.path.stat().st_mode & 0o777, 0o600)
            self.assertEqual(self.store.directory.stat().st_mode & 0o777, 0o700)

    def test_store_atomic_failure_preserves_previous_file_and_cleans_temporary(self):
        with self.store.locked():
            self.store.save({"version": 1, "marker": "before"})
        before = self.store.path.read_bytes()
        with patch("services.meli_oauth_store.os.replace", side_effect=OSError("fake-private-error")):
            with self.assertRaises(SafeError), self.store.locked():
                self.store.save({"version": 1, "marker": "after"})
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertFalse(list(self.store.directory.glob("*.tmp")))

    def test_store_rejects_corrupt_data(self):
        with self.store.locked():
            self.store.save({"version": 1})
            self.store.path.write_bytes(b"corrupt-nonsecret-test-data")
            with self.assertRaises(SafeError):
                self.store.load()

    def test_store_excludes_other_instance(self):
        other = LocalOAuthStore(self.store.directory)
        with self.store.locked():
            with self.assertRaises(SafeError), other.locked():
                self.fail("Dois processos nao podem compartilhar a renovacao")

    def test_windows_protection_failure_never_writes_plaintext(self):
        with patch("services.meli_oauth_store._dpapi", side_effect=SafeError("protecao indisponivel")):
            if os.name == "nt":
                with self.assertRaises(SafeError), self.store.locked():
                    self.store.save({"version": 1, "secret": "fake-secret"})
                self.assertFalse(self.store.path.exists())


if __name__ == "__main__":
    unittest.main()
