"""Transporte sem proxy herdado, validado sem conexoes ou credenciais reais."""

import os
from unittest.mock import Mock, patch

import requests

from services.mercado_livre_http import API_TIMEOUT, create_meli_session
from test_mvp import IsolatedTest


class MercadoLivreTransportTests(IsolatedTest):
    def test_session_policy_is_local_and_keeps_certificate_validation(self):
        with create_meli_session() as meli, requests.Session() as other:
            self.assertFalse(meli.trust_env)
            self.assertIs(meli.verify, True)
            self.assertEqual(meli.proxies, {})
            self.assertTrue(other.trust_env)
            self.assertIs(other.verify, True)

    def test_get_and_post_ignore_poisoned_proxy_environment_without_real_network(self):
        fake_environment = {
            "HTTP_PROXY": "http://proxy.example.test:9999",
            "HTTPS_PROXY": "http://proxy.example.test:9999",
            "ALL_PROXY": "http://proxy.example.test:9999",
            "NO_PROXY": "unrelated.example.test",
            "http_proxy": "http://proxy.example.test:9999",
            "https_proxy": "http://proxy.example.test:9999",
            "all_proxy": "http://proxy.example.test:9999",
            "no_proxy": "unrelated.example.test",
            "REQUESTS_CA_BUNDLE": "unused-test-bundle.pem",
            "CURL_CA_BUNDLE": "unused-test-bundle.pem",
        }
        # Exercita o preparo real da Session; intercepta send antes de qualquer I/O.
        with patch("requests.Session.send", return_value=Mock(status_code=200)) as send, \
                patch("requests.sessions.get_netrc_auth", side_effect=AssertionError("netrc proibido no teste")), \
                patch.dict(os.environ, fake_environment, clear=True):
            self.network.stop()
            try:
                with create_meli_session() as session:
                    session.get("https://api.mercadolibre.com/users/me", timeout=API_TIMEOUT, allow_redirects=False)
                    session.post("https://api.mercadolibre.com/oauth/token", data={"grant_type": "fake-test-grant"},
                                 timeout=API_TIMEOUT, allow_redirects=False)
            finally:
                self.network.start()
        self.assertEqual(send.call_count, 2)
        for call in send.call_args_list:
            self.assertEqual(call.kwargs["proxies"], {})
            self.assertIs(call.kwargs["verify"], True)
            self.assertEqual(call.kwargs["timeout"], (5, 20))
            self.assertFalse(call.kwargs["allow_redirects"])
