"""Catálogo com HTTP simulado, SQLite temporário e credenciais fictícias."""

import io
import json
import sqlite3
import threading
from contextlib import closing, redirect_stdout
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, call, patch

import requests

import main
from admin_bot import AdminBot
from config import DEFAULT_TERMS, Config, load_config
from database import SCHEMA, Database
from engine import OfferEngine
from models import EvaluatedOffer, SearchReport, timestamp
from services.mercado_livre import API_PRODUCTS, API_SEARCH, CatalogSource as MercadoLivreSource, SearchBatch, normalize_product
from services.offer_filter import score_offer
from services.offer_formatter import format_dry_run_report
from telegram_service import TelegramService
from test_mvp import IsolatedTest, NOW, product, test_directory, update


def catalog(product_id="MLB100", item_id="MLB900", **changes):
    detail = {
        "id": product_id, "site_id": "MLB", "status": "active", "name": "Mouse gamer de teste",
        "domain_id": "MLB-COMPUTER_MICE", "permalink": f"https://www.mercadolivre.com.br/p/{product_id}?tracking=example",
        "pictures": [{"url": "https://http2.mlstatic.com/example.jpg"}],
        "attributes": [{"id": "BRAND", "value_name": "Logitech"}],
        "buy_box_winner": {
            "item_id": item_id, "product_id": product_id, "site_id": "MLB", "price": 60,
            "original_price": 100, "currency_id": "BRL", "condition": "new", "sold_quantity": 1000,
            "available_quantity": 5, "shipping": {"free_shipping": True},
            "seller": {"reputation_level_id": "GREEN"},
        },
    }
    detail.update(changes)
    return detail


def response(payload=None, status=200, headers=None):
    result = requests.Response()
    result.status_code = status
    result._content = json.dumps(payload if payload is not None else {}).encode()
    result._content_consumed = True
    result.headers.update(headers or {})
    return result


def batch(*products):
    return SearchBatch(products=list(products), found_ids={p.product_id or p.external_id for p in products})


class CatalogNormalizationTests(IsolatedTest):
    def test_winner_fields_are_normalized_without_private_seller_data(self):
        raw = catalog(sold_quantity=99999, reviews={"rating_average": 5}, seller_address={"private": "ignored"})
        p = normalize_product(raw, "mouse gamer")
        self.assertEqual((p.product_id, p.item_id, p.external_id), ("MLB100", "MLB900", "catalog:MLB100"))
        self.assertEqual((p.price, p.original_price, p.discount_percentage), (6000, 10000, 40))
        self.assertEqual((p.sold_quantity, p.available_quantity), (1000, 5))
        self.assertEqual((p.domain_id, p.currency_id, p.brand), ("MLB-COMPUTER_MICE", "BRL", "Logitech"))
        self.assertTrue(p.free_shipping)
        self.assertEqual(p.seller_reputation, "GREEN")
        self.assertEqual(p.url, "https://www.mercadolivre.com.br/p/MLB100")
        self.assertEqual(p.source, "mercado_livre")
        self.assertIsNone(p.rating)
        self.assertNotIn("private", repr(p))

    def test_absent_buy_box_never_becomes_an_offer(self):
        for winner in (None, {}, [], False):
            self.assertIsNone(normalize_product(catalog(buy_box_winner=winner), "mouse"))

    def test_missing_fields_stay_none_and_do_not_use_aggregate_data(self):
        raw = catalog(attributes=None, pictures=None, sold_quantity=8000, original_price=200,
                      buy_box_winner_price_range={"min": {"price": 1}, "max": {"price": 500}})
        raw["buy_box_winner"] = {"item_id": "MLB900", "price": 60, "currency_id": "BRL", "base_price": 200}
        p = normalize_product(raw, "mouse")
        for name in ("original_price", "rating", "sold_quantity", "free_shipping", "seller_reputation",
                     "available_quantity", "brand", "image_url", "discount_percentage"):
            self.assertIsNone(getattr(p, name), name)
        self.assertEqual(score_offer(p, []).score, 0)
        self.assertEqual(score_offer(p, []).criteria, {})

    def test_discount_requires_real_higher_original_price_and_rounds(self):
        for original, expected in ((None, None), (0, None), (100, None), (179.90, None), (250, 28.04)):
            raw = catalog()
            raw["buy_box_winner"].update(price=179.90, original_price=original)
            self.assertEqual(normalize_product(raw, "mouse").discount_percentage, expected)

    def test_free_shipping_and_reputation_are_not_inferred(self):
        for value, expected in ((True, True), (False, False), (None, None), ("true", None), (1, None)):
            raw = catalog()
            raw["buy_box_winner"].update(shipping={"free_shipping": value}, seller={"tags": ["platinum"]})
            p = normalize_product(raw, "mouse")
            self.assertIs(p.free_shipping, expected)
            self.assertIsNone(p.seller_reputation)

    def test_invalid_offers_are_rejected(self):
        for changes in ({"price": None}, {"price": 0}, {"price": "NaN"}, {"price": True},
                        {"currency_id": "USD"}, {"available_quantity": 0}, {"available_quantity": -1},
                        {"condition": "used"}, {"status": "paused"}, {"item_id": "MLA900"},
                        {"product_id": "MLB999"}, {"site_id": "MLA"}):
            with self.subTest(fields=list(changes)):
                raw = catalog()
                raw["buy_box_winner"].update(changes)
                self.assertIsNone(normalize_product(raw, "mouse"))
        for changes in ({"status": "inactive"}, {"site_id": "MLA"}, {"id": "../../other"},
                        {"name": None}, {"name": "Sofá", "domain_id": "MLB-SOFAS"}):
            self.assertIsNone(normalize_product(catalog(**changes), "mouse"))

    def test_domain_accepts_a_tech_name_without_keyword_and_winner_link_wins(self):
        raw = catalog(name="Logitech G305")
        raw["buy_box_winner"]["permalink"] = "https://produto.mercadolivre.com.br/MLB-900-mouse?affiliate=example#tracking"
        p = normalize_product(raw, "mouse")
        self.assertEqual(p.category, "mouse")
        self.assertEqual(p.url, "https://produto.mercadolivre.com.br/MLB-900-mouse")
        for link in ("https://evil.example/MLB900", "https://user:password@www.mercadolivre.com.br/p/MLB100", "https://meli.la/example"):
            self.assertIsNone(normalize_product(catalog(permalink=link), "mouse"))

    def test_bad_optional_values_are_omitted(self):
        raw = catalog()
        raw["buy_box_winner"].update(sold_quantity="100+", available_quantity="5", shipping=[], seller=[])
        p = normalize_product(raw, "mouse")
        self.assertIsNone(p.sold_quantity)
        self.assertIsNone(p.available_quantity)
        self.assertIsNone(p.free_shipping)
        self.assertIsNone(p.seller_reputation)


class CatalogSourceTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.token = self.enterContext(patch("services.mercado_livre.get_valid_access_token", return_value="fake-catalog-access"))
        self.http = self.enterContext(patch("services.mercado_livre.requests.Session.get", autospec=True))
        self.source = MercadoLivreSource()
        self.pace = self.enterContext(patch.object(self.source, "_pace"))
        self.wait = self.enterContext(patch.object(self.source, "_wait"))
        self.enterContext(patch("services.mercado_livre.random.uniform", return_value=0))

    def test_search_and_detail_use_official_api_safe_session_and_oauth(self):
        self.token.assert_not_called()
        self.http.side_effect = [response({"results": [{"id": "MLB100"}]}), response(catalog())]
        result = self.source.search("mouse gamer", 10)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(result.found_ids, {"MLB100"})
        self.assertEqual(self.token.call_count, 2)
        calls = self.http.call_args_list
        self.assertEqual([c.args[1] for c in calls], [API_SEARCH, API_PRODUCTS + "/MLB100"])
        self.assertEqual(calls[0].kwargs["params"], {"status": "active", "site_id": "MLB", "q": "mouse gamer", "limit": 10})
        for c in calls:
            self.assertFalse(c.args[0].trust_env)
            self.assertTrue(c.args[0].verify)
            self.assertEqual(c.kwargs["timeout"], (5, 20))
            self.assertFalse(c.kwargs["allow_redirects"])
            self.assertEqual(c.kwargs["headers"]["Authorization"], "Bearer fake-catalog-access")
            self.assertNotIn("fake-catalog-access", c.args[1])

    def test_limit_is_ten_no_pagination_and_invalid_ids_are_never_requested(self):
        self.http.side_effect = [response({"results": [{"id": f"MLB{i}"} for i in range(100, 115)]})] + [
            response(catalog(f"MLB{i}", f"MLB{i + 1000}")) for i in range(100, 110)]
        result = self.source.search("mouse", 50)
        self.assertEqual(len(result.products), 10)
        self.assertEqual(self.http.call_count, 11)
        self.assertEqual(self.http.call_args_list[0].kwargs["params"]["limit"], 10)

    def test_catalog_duplicates_share_one_detail_per_cycle(self):
        listing = {"results": [{"id": "MLB100"}, {"id": "MLB100"}]}
        self.http.side_effect = [response(listing), response(catalog()), response(listing), response(listing)]
        for term in DEFAULT_TERMS:
            self.assertEqual(len(self.source.search(term, 10).products), 1)
        self.assertEqual(self.http.call_count, 4)
        self.source.begin_cycle()
        self.http.side_effect = [response(listing), response(catalog())]
        self.assertEqual(len(self.source.search("mouse", 10).products), 1)
        self.assertEqual(self.http.call_count, 6)

    def test_null_buy_box_is_counted_as_found_only(self):
        self.http.side_effect = [response({"results": [{"id": "MLB100"}]}), response(catalog(buy_box_winner=None))]
        result = self.source.search("mouse", 10)
        self.assertEqual(result.found_ids, {"MLB100"})
        self.assertEqual(result.products, [])
        self.assertEqual(result.errors, [])

    def test_401_forces_one_refresh_then_uses_replacement(self):
        self.token.side_effect = ["fake-old-access", "fake-new-access"]
        self.http.side_effect = [response(status=401), response({"results": []})]
        result = self.source.search("mouse", 10)
        self.assertFalse(result.stop)
        self.assertEqual(self.token.call_args_list, [call(), call(force_refresh=True)])
        self.assertEqual(self.http.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer fake-new-access")

    def test_persistent_401_has_no_refresh_loop(self):
        self.http.side_effect = [response(status=401), response(status=401)]
        result = self.source.search("mouse", 10)
        self.assertTrue(result.stop)
        self.assertEqual(result.errors[0].status, 401)
        self.assertEqual(self.http.call_count, 2)
        self.assertEqual(self.token.call_args_list, [call(), call(force_refresh=True)])

    def test_second_401_in_another_endpoint_does_not_refresh_again_in_cycle(self):
        self.http.side_effect = [response(status=401), response({"results": [{"id": "MLB100"}]}), response(status=401)]
        result = self.source.search("mouse", 10)
        self.assertTrue(result.stop)
        self.assertEqual(result.errors[0].status, 401)
        self.assertEqual(self.token.call_args_list, [call(), call(force_refresh=True), call()])
        self.assertEqual(self.http.call_count, 3)

    def test_429_stops_and_blocks_an_immediate_new_search(self):
        self.http.return_value = response({"message": "fake-private-response"}, 429, {"Retry-After": "120"})
        with patch("services.mercado_livre.time.time", return_value=1000):
            first = self.source.search("mouse", 10)
            second = self.source.search("ssd", 10)
        self.assertEqual(first.errors[0].retry_after, 120)
        self.assertEqual(second.errors[0].retry_after, 120)
        self.assertTrue(second.stop)
        self.assertEqual(self.http.call_count, 3)
        self.assertNotIn("fake-private-response", str(first.errors))

    def test_429_invalid_header_uses_conservative_backoff(self):
        self.http.return_value = response(status=429, headers={"Retry-After": "invalid-private-value"})
        result = self.source.search("mouse", 10)
        self.assertEqual(result.errors[0].retry_after, 60)
        self.assertNotIn("invalid-private-value", str(result.errors))

    def test_403_404_and_5xx_do_not_retry_or_dump_response(self):
        for status in (403, 404, 500, 502, 503):
            self.http.reset_mock()
            self.http.return_value = response({"message": "fake-access fake-refresh fake-secret"}, status)
            result = self.source.search("mouse", 10)
            self.assertEqual(result.errors[0].status, status)
            self.http.assert_called_once()
            self.assertNotIn("fake-", str(result.errors))

    def test_detail_404_continues_to_next_product(self):
        self.http.side_effect = [response({"results": [{"id": "MLB100"}, {"id": "MLB200"}]}),
                                 response(status=404), response(catalog("MLB200", "MLB920"))]
        result = self.source.search("mouse", 10)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(len(result.errors), 1)
        self.assertFalse(result.stop)

    def test_partial_success_survives_a_later_detail_failure(self):
        self.http.side_effect = [response({"results": [{"id": "MLB100"}, {"id": "MLB200"}, {"id": "MLB300"}]}),
                                 response(catalog()), response(status=503)]
        result = self.source.search("mouse", 10)
        self.assertEqual(len(result.products), 1)
        self.assertEqual(len(result.found_ids), 3)
        self.assertTrue(result.stop)
        self.assertEqual(self.http.call_count, 3)

    def test_invalid_json_and_mismatched_detail_fail_safely(self):
        invalid = response()
        invalid._content = b"fake-private-value not json"
        self.http.return_value = invalid
        result = self.source.search("mouse", 10)
        self.assertEqual(len(result.errors), 1)
        self.assertNotIn("fake-private-value", str(result.errors))
        self.http.side_effect = [response({"results": [{"id": "MLB100"}]}), response(catalog("MLB200"))]
        result = self.source.search("mouse", 10)
        self.assertEqual(result.products, [])
        self.assertEqual(len(result.errors), 1)

    def test_network_errors_do_not_expose_tokens_urls_or_headers(self):
        for error in (requests.Timeout, requests.ConnectionError, requests.exceptions.SSLError):
            self.http.side_effect = error("Authorization: Bearer fake-access; refresh_token=fake-refresh; client_secret=fake-secret")
            result = self.source.search("mouse", 10)
            self.assertTrue(result.stop)
            self.assertNotIn("fake-", str(result.errors))
            self.assertNotIn("Authorization", str(result.errors))

    def test_echoed_access_token_is_removed_from_product_before_storage(self):
        raw = catalog(name="Mouse fake-catalog-access", attributes=[{"id": "BRAND", "value_name": "fake-catalog-access"}])
        self.http.side_effect = [response({"results": [{"id": "MLB100"}]}), response(raw)]
        p = self.source.search("mouse", 10).products[0]
        self.assertNotIn("fake-catalog-access", repr(p))

    def test_pacing_is_between_requests_and_stop_interrupts_wait(self):
        source = MercadoLivreSource(request_interval_seconds=2)
        with patch("services.mercado_livre.time.monotonic", return_value=1000), \
             patch("services.mercado_livre.time.sleep") as sleep:
            self.http.return_value = response({"results": []})
            source.search("mouse", 1)
            sleep.assert_not_called()
            source.search("ssd", 1)
            sleep.assert_called_once_with(2)
        event = threading.Event()
        event.set()
        source.begin_cycle(event)
        with self.assertRaisesRegex(Exception, "interrompida"):
            source._pace()


class CatalogScoreTests(IsolatedTest):
    def test_discount_points_follow_bands_without_normalization(self):
        for percent, points in ((0, 0), (9, 0), (10, 12), (20, 22), (30, 30), (40, 35), (80, 35)):
            p = product(price=10000 - percent * 100, original_price=10000,
                        brand=None, sold_quantity=None, free_shipping=None, seller_reputation=None)
            self.assertEqual(score_offer(p, []).score, points)

    def test_moderate_sales_shipping_and_green_reputation(self):
        p = product(original_price=None, brand=None, sold_quantity=0, free_shipping=False, seller_reputation="RED")
        self.assertEqual(score_offer(p, []).score, 0)
        p = replace(p, sold_quantity=50, free_shipping=True, seller_reputation="GREEN")
        self.assertEqual(score_offer(p, []).score, 24)
        self.assertFalse(score_offer(p, []).approved(20))

    def test_history_requires_three_previous_observations(self):
        p = product(price=6000, original_price=None)
        for history in ([], [10000], [10000, 11000]):
            self.assertNotIn("history", score_offer(p, history).criteria)
        self.assertEqual(score_offer(p, [10000, 11000, 12000]).criteria["history"], 25)
        self.assertEqual(score_offer(p, [5000, 11000, 12000]).criteria["history"], 0)

    def test_brand_is_configurable_and_alone_is_not_a_good_offer(self):
        p = product(original_price=None, free_shipping=None, sold_quantity=None, seller_reputation=None)
        self.assertEqual(score_offer(p, [], known_brands=()).score, 0)
        result = score_offer(p, [], known_brands=("LOGITECH",))
        self.assertEqual(result.score, 5)
        self.assertFalse(result.approved(0))

    def test_maximum_score_is_100_and_rating_is_not_used(self):
        p = product(rating=5, relevance_score=10)
        result = score_offer(p, [20000, 20000, 20000])
        self.assertEqual(result.score, 100)
        self.assertNotIn("rating", result.criteria)
        self.assertEqual(score_offer(p, []).score, 80)


class CatalogIntegrationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.directory = self.enterContext(test_directory())
        self.db = Database(self.directory / "test.db")
        self.config = Config(database_path=self.db.path, telegram_admin_id=42, request_interval_seconds=0)
        self.telegram = Mock(spec=TelegramService)
        self.output = Mock()

    def engine(self, source, **kwargs):
        return OfferEngine(self.config, self.db, source, self.telegram, clock=lambda: NOW, reporter=self.output, **kwargs)

    def test_products_scores_history_and_duplicate_ids_are_saved_once_per_cycle(self):
        p = normalize_product(catalog(), "mouse")
        source = Mock(search=Mock(return_value=batch(p, p)))
        engine = self.engine(source)
        engine.run_search()
        self.assertEqual(engine.last_report.terms_searched, 3)
        self.assertEqual(len(engine.last_report.offers), 1)
        self.assertEqual(len(engine.last_report.found_ids), 1)
        self.assertEqual(engine.last_report.simulated, 1)
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM products").fetchone()
            self.assertEqual((row["product_id"], row["item_id"], row["free_shipping"]), ("MLB100", "MLB900", 1))
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_evaluations").fetchone()[0], 1)
        # Troca de vencedor preserva identidade e histórico do mesmo catálogo.
        source.search.return_value = batch(replace(p, item_id="MLB901", price=5800, free_shipping=None))
        engine.clock = lambda: NOW + timedelta(hours=1)
        engine.run_search()
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)
            row = connection.execute("SELECT item_id, free_shipping FROM products").fetchone()
            self.assertEqual(tuple(row), ("MLB901", None))
        self.telegram.publish_offer.assert_not_called()
        self.telegram.reply.assert_not_called()

    def test_same_item_cannot_be_simulated_for_two_catalog_ids(self):
        p = normalize_product(catalog(), "mouse")
        duplicate = replace(p, external_id="catalog:MLB200", product_id="MLB200")
        engine = self.engine(Mock(search=Mock(return_value=batch(p, duplicate))))
        engine.run_search()
        self.assertEqual(len(engine.last_report.offers), 1)

    def test_high_threshold_still_reports_all_scores_and_at_most_ten_details(self):
        products = [replace(normalize_product(catalog(f"MLB{i}", f"MLB{i + 1000}"), "mouse"),
                            title=f"Mouse de teste {i}", original_price=None) for i in range(100, 115)]
        engine = self.engine(Mock(search=Mock(return_value=batch(*products))))
        engine.run_search()
        text = self.output.call_args.args[0]
        self.assertIn("Ofertas acima do threshold: 0", text)
        self.assertEqual(text.count("threshold: não passa"), 15)
        self.assertEqual(text.count("Radar Score:"), 15)
        self.assertNotIn("11.", text.split("TOP OFERTAS:")[1])
        self.assertNotIn("Preço anterior:", text)
        self.assertNotIn("Desconto:", text)
        self.assertNotIn("SERIA PUBLICADA", text)
        self.assertIn("Vendas: 1000", text)
        self.assertIn("Frete grátis: Sim", text)
        self.assertEqual(self.db.stats(NOW, self.config.post_timezone)["analyzed"], 15)
        self.telegram.publish_offer.assert_not_called()

    def test_report_omits_unavailable_optional_fields_and_shows_false_shipping(self):
        p = normalize_product(catalog(), "mouse")
        p = replace(p, original_price=None, sold_quantity=None, free_shipping=None)
        report = SearchReport(offers=[EvaluatedOffer(p, score_offer(p, []), "não seria publicada")])
        text = format_dry_run_report(report, 70)
        for label in ("Preço anterior:", "Desconto:", "Vendas:", "Frete grátis:", "Avaliação:"):
            self.assertNotIn(label, text)
        report.offers[0].product = replace(p, free_shipping=False)
        self.assertIn("Frete grátis: Não", format_dry_run_report(report, 70))

    def test_catalog_http_failure_logs_and_database_have_no_secrets(self):
        source = MercadoLivreSource()
        with patch("services.mercado_livre.get_valid_access_token", return_value="fake-catalog-access"), \
             patch("services.mercado_livre.requests.Session.get", return_value=response({"message": "fake-catalog-access fake-refresh fake-secret"}, 503)), \
             self.assertLogs("engine", level="INFO") as logs:
            engine = self.engine(source)
            engine.run_search()
        content = "\n".join(logs.output) + self.output.call_args.args[0] + str(self.db.recent_errors())
        for secret in ("fake-catalog-access", "fake-refresh", "fake-secret", "Authorization"):
            self.assertNotIn(secret, content)
        self.assertIn("503", content)
        self.assertEqual(engine.last_report.status, "failed")

    def test_backoff_survives_a_new_engine_and_database_instance(self):
        from safety import SafeError
        limited = Mock(search=Mock(return_value=SearchBatch(errors=[SafeError("Limite de consultas.", status=429, retry_after=120)], stop=True)))
        self.engine(limited).run_search()
        replacement = Mock()
        restarted = OfferEngine(self.config, Database(self.db.path), replacement, self.telegram,
                                clock=lambda: NOW + timedelta(seconds=30), reporter=self.output)
        restarted.run_search()
        replacement.search.assert_not_called()
        self.assertEqual(restarted.last_report.terms_searched, 0)
        self.assertIn("backoff", self.output.call_args.args[0])

    def test_search_now_forces_three_terms_and_no_telegram_even_if_config_is_true(self):
        config = replace(self.config, auto_publish=True, search_terms=("other",), search_limit_per_term=50)
        source = Mock(search=Mock(return_value=batch(normalize_product(catalog(), "mouse"))))
        output = io.StringIO()
        with patch("main.load_config", return_value=config) as load, \
             patch("main.configure_logging"), patch("main.MercadoLivreSource", return_value=source), \
             patch("main.TelegramService", return_value=self.telegram), patch("main.poll_admin") as poll, \
             patch("sys.argv", ["main.py", "--dry-run", "--search-now"]), redirect_stdout(output):
            self.assertEqual(main.main(), 0)
        load.assert_called_once_with(force_dry_run=True)
        self.assertEqual(source.search.call_args_list, [call(term, 10) for term in DEFAULT_TERMS])
        self.assertIn("RADAR TECH — DRY RUN", output.getvalue())
        self.assertFalse(self.db.get_state("auto_publish"))
        poll.assert_not_called()
        self.telegram.publish_offer.assert_not_called()
        self.telegram.reply.assert_not_called()
        self.telegram.get_updates.assert_not_called()

    def test_buscar_uses_same_engine_in_simulation(self):
        source = Mock(search=Mock(return_value=batch(normalize_product(catalog(), "mouse"))))
        engine = self.engine(source)
        admin = AdminBot(self.config, self.db, engine, self.telegram)
        admin.handle_update(update("/buscar"))
        self.assertTrue(engine._requested.is_set())
        engine.run_search()
        self.assertEqual(source.search.call_count, 3)
        self.assertEqual(engine.last_report.simulated, 1)
        self.telegram.publish_offer.assert_not_called()

    def test_complete_http_to_sqlite_and_cli_report_pipeline_with_mocked_network(self):
        pages = [response({"results": [{"id": "MLB100"}, {"id": "MLB200"}]}),
                 response(catalog()), response(catalog("MLB200", "MLB920", buy_box_winner=None)),
                 response({"results": [{"id": "MLB100"}]}),
                 response({"results": [{"id": "MLB300"}]}), response(catalog("MLB300", "MLB930"))]
        output = io.StringIO()
        with patch("services.mercado_livre.get_valid_access_token", return_value="fake-catalog-access"), \
             patch("services.mercado_livre.requests.Session.get", side_effect=pages) as http, \
             patch("services.mercado_livre.CatalogSource._pace"), \
             patch("main.MercadoLivreSource", return_value=MercadoLivreSource()), \
             patch("main.TelegramService", return_value=self.telegram), redirect_stdout(output):
            self.assertEqual(main.run(self.config, search_now=True), 0)
        stats = self.db.stats(NOW, self.config.post_timezone)
        self.assertEqual(stats["last_run"]["terms_searched"], 3)
        self.assertEqual(stats["last_run"]["found"], 3)
        self.assertEqual(stats["last_run"]["analyzed"], 2)
        self.assertEqual(stats["last_run"]["approved"], 2)
        self.assertEqual(stats["last_run"]["simulated"], 1)
        self.assertEqual(stats["publications"], 0)
        self.assertEqual(http.call_count, 6)
        self.assertIn("Itens válidos: 2", output.getvalue())
        self.assertNotIn("fake-catalog-access", output.getvalue())
        self.telegram.publish_offer.assert_not_called()
        self.telegram.reply.assert_not_called()
        self.telegram.get_updates.assert_not_called()

    def test_schema_v1_migration_keeps_existing_history_and_is_repeatable(self):
        path = self.directory / "legacy.db"
        with closing(sqlite3.connect(path)) as connection, connection:
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version=1")
            connection.execute("INSERT INTO products(id,external_id,title,price,url,source,first_seen,last_seen) VALUES (1,?,?,?,?,?,?,?)",
                               ("MLB100", "Mouse antigo", 10000, "https://www.mercadolivre.com.br/p/MLB100", "mercado_livre", timestamp(NOW), timestamp(NOW)))
            connection.execute("INSERT INTO price_history(product_id,price,checked_at) VALUES (1,?,?)", (10000, timestamp(NOW)))
        migrated = Database(path)
        p = normalize_product(catalog(), "mouse")
        migrated.observe(p, NOW, 30)
        restarted = Database(path)
        with restarted.connect() as connection:
            self.assertEqual(connection.execute("PRAGMA user_version").fetchone()[0], 3)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)
            self.assertEqual(connection.execute("SELECT price FROM products WHERE id=1").fetchone()[0], 10000)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)

    def test_defaults_are_small_and_old_environment_limits_are_capped(self):
        cfg = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", "SEARCH_LIMIT_PER_TERM": "50", "SEARCH_TERMS_PER_CYCLE": "10"})
        self.assertEqual(cfg.search_terms, ("mouse gamer", "headset gamer", "ssd nvme"))
        self.assertEqual((cfg.search_terms_per_cycle, cfg.search_limit_per_term), (3, 10))
        self.assertFalse(cfg.auto_publish)
