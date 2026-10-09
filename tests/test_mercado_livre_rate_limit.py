"""Rate limit com relógio/esperas simulados; sem rede, .env ou cofre reais."""

import io
import threading
from contextlib import redirect_stderr, redirect_stdout
from datetime import datetime, timedelta, timezone
from email.utils import format_datetime
from unittest.mock import Mock, call, patch

import main
from config import Config, ConfigError, load_config
from database import Database
from engine import OfferEngine
from services.mercado_livre import API_PRODUCTS, API_SEARCH, CatalogSource as MercadoLivreSource, retry_after_seconds
from telegram_service import TelegramService
from test_mercado_livre_catalog import batch, catalog, response
from test_mvp import IsolatedTest, NOW, test_directory


class RateLimitTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.token = self.enterContext(patch("services.mercado_livre.get_valid_access_token", return_value="fake-rate-access"))
        self.http = self.enterContext(patch("services.mercado_livre.requests.Session.get", autospec=True))
        self.enterContext(patch("services.mercado_livre.time.time", return_value=1000))
        self.enterContext(patch("services.mercado_livre.time.monotonic", return_value=1000))
        self.jitter = self.enterContext(patch("services.mercado_livre.random.uniform", return_value=0))
        self.source = MercadoLivreSource()
        self.enterContext(patch.object(self.source, "_pace"))
        self.wait = self.enterContext(patch.object(self.source, "_wait"))

    def test_retry_after_seconds_waits_then_succeeds_without_forced_refresh(self):
        limited = response(status=429, headers={"Retry-After": "120"})
        limited.close = Mock(wraps=limited.close)
        self.http.side_effect = [limited, response({"results": []})]
        self.wait.side_effect = lambda seconds: limited.close.assert_called_once()
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            result = self.source.search("mouse gamer", 5)
        self.assertFalse(result.stop)
        self.assertEqual(result.errors, [])
        self.assertEqual(self.http.call_count, 2)
        self.wait.assert_called_once_with(120)
        self.assertEqual(self.token.call_args_list, [call(), call()])
        text = "\n".join(logs.output)
        for part in ("retornou 429", "Retry-After: 120 segundos", "Tentativa: 1/3", "/products/search", "Nova tentativa após backoff"):
            self.assertIn(part, text)

    def test_http_date_retry_after_is_respected(self):
        http_date = format_datetime(datetime.fromtimestamp(1090, timezone.utc), usegmt=True)
        self.http.side_effect = [response(status=429, headers={"Retry-After": http_date}), response({"results": []})]
        self.assertFalse(self.source.search("mouse", 5).stop)
        self.wait.assert_called_once_with(90)

    def test_retry_after_parser_rejects_invalid_values_and_handles_past_date(self):
        self.assertEqual(retry_after_seconds("0", 1000), 0)
        self.assertEqual(retry_after_seconds(" 42 ", 1000), 42)
        past = format_datetime(datetime.fromtimestamp(900, timezone.utc), usegmt=True)
        self.assertEqual(retry_after_seconds(past, 1000), 0)
        for invalid in (None, "", "-10", "1.5", "nan", "inf", "Bearer fake-private", "1\nAuthorization: fake-private"):
            self.assertIsNone(retry_after_seconds(invalid, 1000))

    def test_missing_retry_after_uses_exponential_backoff_with_jitter(self):
        self.jitter.side_effect = [0.5, 1.5]
        self.http.side_effect = [response(status=429), response(status=429), response({"results": []})]
        result = self.source.search("mouse", 5)
        self.assertFalse(result.stop)
        self.assertEqual(self.wait.call_args_list, [call(5.5), call(11.5)])
        self.assertEqual(self.jitter.call_args_list, [call(0, 1.25), call(0, 2.5)])
        self.assertEqual(self.http.call_count, 3)

    def test_server_delay_is_minimum_even_with_jitter(self):
        self.jitter.return_value = 0.75
        self.http.side_effect = [response(status=429, headers={"Retry-After": "30"}), response({"results": []})]
        self.assertFalse(self.source.search("mouse", 5).stop)
        self.wait.assert_called_once_with(30.75)

    def test_each_retry_uses_latest_retry_after(self):
        self.http.side_effect = [response(status=429, headers={"Retry-After": "1"}),
                                 response(status=429, headers={"Retry-After": "120"}), response({"results": []})]
        self.assertFalse(self.source.search("mouse", 5).stop)
        self.assertEqual(self.wait.call_args_list, [call(5), call(120)])

    def test_three_failures_stop_without_fourth_request_and_keep_cooldown(self):
        self.http.return_value = response(status=429)
        result = self.source.search("mouse", 5)
        self.assertTrue(result.stop)
        self.assertEqual(result.errors[0].status, 429)
        self.assertEqual(result.errors[0].retry_after, 60)
        self.assertIn("3 tentativas", str(result.errors[0]))
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.wait.call_args_list, [call(5), call(10)])
        self.source.begin_cycle()
        self.assertTrue(self.source.search("ssd", 5).stop)
        self.assertEqual(self.http.call_count, 3)

    def test_success_clears_cooldown_for_next_operation(self):
        self.http.side_effect = [response(status=429), response({"results": []}), response({"results": []})]
        self.assertFalse(self.source.search("mouse", 5).stop)
        self.assertFalse(self.source.search("ssd", 5).stop)
        self.assertEqual(self.http.call_count, 3)

    def test_401_and_429_share_three_attempt_budget(self):
        self.http.side_effect = [response(status=429), response(status=401), response(status=429)]
        result = self.source.search("mouse", 5)
        self.assertTrue(result.stop)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.token.call_args_list, [call(), call(), call(force_refresh=True)])
        self.wait.assert_called_once_with(5)

    def test_last_attempt_401_does_not_consume_refresh_without_budget_for_get(self):
        self.http.side_effect = [response(status=429), response(status=429), response(status=401)]
        result = self.source.search("mouse", 5)
        self.assertEqual(result.errors[0].status, 401)
        self.assertEqual(self.http.call_count, 3)
        self.assertEqual(self.token.call_args_list, [call(), call(), call()])

    def test_successful_detail_is_not_repeated_after_retry_or_in_next_term(self):
        listing = {"results": [{"id": "MLB100"}, {"id": "MLB100"}]}
        self.http.side_effect = [response(listing), response(status=429), response(catalog()), response(listing)]
        self.assertEqual(len(self.source.search("mouse", 5).products), 1)
        self.assertEqual(len(self.source.search("headset", 5).products), 1)
        urls = [c.args[1] for c in self.http.call_args_list]
        self.assertEqual(urls, [API_SEARCH, API_PRODUCTS + "/MLB100", API_PRODUCTS + "/MLB100", API_SEARCH])

    def test_unavailable_details_are_also_cached(self):
        listing = {"results": [{"id": "MLB100"}, {"id": "MLB200"}]}
        self.http.side_effect = [response(listing), response(catalog(buy_box_winner=None)),
                                 response(status=404), response(listing)]
        self.assertEqual(self.source.search("mouse", 5).products, [])
        self.assertEqual(self.source.search("ssd", 5).products, [])
        self.assertEqual(self.http.call_count, 4)

    def test_only_safe_numeric_rate_headers_and_endpoint_are_logged(self):
        self.http.side_effect = [response({"message": "fake-body-secret"}, 429, {
            "Retry-After": "fake-header-secret", "X-RateLimit-Limit": "100",
            "X-RateLimit-Remaining": "0", "RateLimit-Reset": "1100",
            "X-RateLimit-Reset": "fake-header-secret", "X-Unlisted": "fake-header-secret",
            "Authorization": "Bearer fake-rate-access", "Set-Cookie": "fake-cookie-secret",
        }), response({"results": []})]
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            result = self.source.search("mouse", 5)
        text = "\n".join(logs.output) + str(result.errors)
        for forbidden in ("fake-", "Authorization", "Cookie", "https://", "X-Unlisted"):
            self.assertNotIn(forbidden, text)
        for allowed in ("X-RateLimit-Limit=100", "X-RateLimit-Remaining=0", "RateLimit-Reset=1100"):
            self.assertIn(allowed, text)

    def test_cancellation_during_backoff_stops_without_retry_and_preserves_delay(self):
        source = MercadoLivreSource()
        event = Mock(spec=threading.Event)
        event.is_set.return_value = False
        event.wait.return_value = True
        source.begin_cycle(event)
        self.http.return_value = response(status=429, headers={"Retry-After": "120"})
        result = source.search("mouse", 5)
        self.assertTrue(result.stop)
        self.assertIn("interrompida", str(result.errors[0]))
        self.assertEqual(result.errors[0].retry_after, 120)
        self.http.assert_called_once()
        event.wait.assert_called_once_with(60)

    def test_ctrl_c_during_backoff_is_safe_and_does_not_retry(self):
        source = MercadoLivreSource()
        self.http.return_value = response(status=429, headers={"Retry-After": "120"})
        with patch("services.mercado_livre.time.sleep", side_effect=KeyboardInterrupt):
            result = source.search("mouse", 5)
        self.assertTrue(result.stop)
        self.assertIn("interrompida", str(result.errors[0]))
        self.assertEqual(result.errors[0].retry_after, 120)
        self.http.assert_called_once()

    def test_full_delay_follows_successful_response_not_request_start(self):
        source = MercadoLivreSource(request_interval_seconds=2.5)
        # Primeira chamada inicia em 1000, resposta só chega em 1020.
        with patch("services.mercado_livre.time.monotonic", side_effect=[1000, 1020, 1020, 1023]), \
             patch("services.mercado_livre.time.sleep") as sleep:
            self.http.return_value = response({"results": []})
            source.search("mouse", 5)
            sleep.assert_not_called()
            source.search("ssd", 5)
            sleep.assert_called_once_with(2.5)

    def test_exhaustion_during_detail_preserves_partial_data_and_stops_all_terms(self):
        listing = {"results": [{"id": "MLB100"}, {"id": "MLB200"}, {"id": "MLB300"}]}
        self.http.side_effect = [response(listing), response(catalog()), response(status=429),
                                 response(status=429), response(status=429)]
        with test_directory() as directory:
            db = Database(directory / "test.db")
            config = Config(database_path=db.path, request_interval_seconds=0)
            telegram = Mock(spec=TelegramService)
            output = Mock()
            engine = OfferEngine(config, db, self.source, telegram, clock=lambda: NOW, reporter=output)
            engine.run_search()
            self.assertEqual(engine.last_report.status, "partial")
            self.assertEqual(engine.last_report.terms_searched, 1)
            self.assertEqual(len(engine.last_report.offers), 1)
            self.assertEqual(self.http.call_count, 5)
            self.assertEqual(datetime.fromisoformat(db.get_state("meli_retry_not_before")), NOW + timedelta(seconds=60))
            with db.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
            telegram.publish_offer.assert_not_called()
            telegram.reply.assert_not_called()
            telegram.get_updates.assert_not_called()


class SmallSearchTests(IsolatedTest):
    def test_delay_config_defaults_and_fractional_override(self):
        self.assertEqual(load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder"}).meli_request_delay_seconds, 1.0)
        cfg = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", "MELI_REQUEST_DELAY_SECONDS": "2.5"})
        self.assertEqual(cfg.meli_request_delay_seconds, 2.5)
        self.assertFalse(cfg.auto_publish)

    def test_delay_config_rejects_unsafe_values_without_echoing_them(self):
        for invalid in ("fake-private-value", "0", "-1", "0.5", "nan", "inf", "61"):
            with self.assertRaises(ConfigError) as caught:
                load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", "MELI_REQUEST_DELAY_SECONDS": invalid})
            self.assertNotIn("fake-private-value", str(caught.exception))

    def test_cli_limit_forces_one_mouse_term_and_no_telegram(self):
        with test_directory() as directory:
            config = Config(database_path=directory / "test.db", auto_publish=True,
                            search_terms=("other",), search_limit_per_term=50,
                            meli_request_delay_seconds=2.5)
            source = Mock(search=Mock(return_value=batch()))
            telegram = Mock(spec=TelegramService)
            output = io.StringIO()
            with patch("main.load_config", return_value=config) as load, patch("main.configure_logging"), \
                 patch("main.MercadoLivreSource", return_value=source) as factory, \
                 patch("main.TelegramService", return_value=telegram), patch("main.poll_admin") as poll, \
                 patch("sys.argv", ["main.py", "--dry-run", "--search-now", "--search-limit", "5"]), redirect_stdout(output):
                self.assertEqual(main.main(), 0)
            load.assert_called_once_with(force_dry_run=True)
            factory.assert_called_once_with(request_interval_seconds=2.5, item_ids=(), negative_keywords=config.negative_keywords)
            source.search.assert_called_once_with("mouse gamer", 5)
            self.assertIn("Termos pesquisados: 1", output.getvalue())
            self.assertFalse(Database(config.database_path).get_state("auto_publish"))
            poll.assert_not_called()
            self.assertEqual(telegram.method_calls, [])

    def test_limit_validation_stops_before_loading_env_or_starting_app(self):
        for argv in (["--search-limit", "5"], ["--search-now", "--search-limit", "0"],
                     ["--search-now", "--search-limit", "11"], ["--search-now", "--search-limit", "fake-private-value"]):
            output = io.StringIO()
            with patch("sys.argv", ["main.py", *argv]), patch("main.load_config") as load, \
                 patch("main.run") as run, redirect_stderr(output), self.assertRaises(SystemExit) as caught:
                main.main()
            self.assertEqual(caught.exception.code, 2)
            load.assert_not_called()
            run.assert_not_called()
            self.assertNotIn("fake-private-value", output.getvalue())

    def test_small_search_limits_actual_mocked_http_and_saves_only_five_observations(self):
        with test_directory() as directory:
            config = Config(database_path=directory / "test.db", auto_publish=True)
            listing = {"results": [{"id": f"MLB{i}"} for i in range(100, 110)]}
            pages = [response(listing)] + [response(catalog(f"MLB{i}", f"MLB{i + 1000}")) for i in range(100, 105)]
            telegram = Mock(spec=TelegramService)
            output = io.StringIO()
            with patch("services.mercado_livre.get_valid_access_token", return_value="fake-rate-access"), \
                 patch("services.mercado_livre.requests.Session.get", side_effect=pages) as http, \
                 patch("services.mercado_livre.CatalogSource._pace"), \
                 patch("main.MercadoLivreSource", return_value=MercadoLivreSource()), \
                 patch("main.TelegramService", return_value=telegram), redirect_stdout(output):
                self.assertEqual(main.run(config, search_now=True, search_limit=5), 0)
            self.assertEqual(http.call_count, 6)
            self.assertEqual(http.call_args_list[0].kwargs["params"], {"status": "active", "site_id": "MLB", "q": "mouse gamer", "limit": 5})
            with Database(config.database_path).connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 5)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
            self.assertEqual(telegram.method_calls, [])

    def test_blocked_cycle_does_not_query_or_extend_persisted_cooldown(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            deadline = (NOW + timedelta(seconds=60)).isoformat()
            db.set_state("meli_retry_not_before", deadline)
            source, telegram = Mock(), Mock(spec=TelegramService)
            output = Mock()
            engine = OfferEngine(Config(database_path=db.path), db, source, telegram,
                                 clock=lambda: NOW + timedelta(seconds=20), reporter=output)
            engine.run_search()
            self.assertIn("40 segundos restantes", output.call_args.args[0])
            source.search.assert_not_called()
            source.begin_cycle.assert_not_called()
            self.assertEqual(db.get_state("meli_retry_not_before"), deadline)

    def test_engine_refuses_a_second_cycle_while_search_is_in_progress(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            source = Mock()
            config = Config(database_path=db.path, search_terms=("mouse gamer",))
            engine = OfferEngine(config, db, source, Mock(spec=TelegramService), clock=lambda: NOW, reporter=Mock())

            def search(term, limit):
                self.assertFalse(engine.run_search())
                self.assertFalse(engine.request_search())
                return batch()

            source.search.side_effect = search
            self.assertTrue(engine.run_search())
            source.search.assert_called_once_with("mouse gamer", 10)
