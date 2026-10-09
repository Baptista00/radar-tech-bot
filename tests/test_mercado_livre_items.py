"""Anúncios e contratos oficiais simulados; sem internet, .env ou cofre real."""

import io
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, call, patch

import requests

import main
from admin_bot import AdminBot
from config import Config, ConfigError, load_config, parse_item_ids
from database import Database
from engine import OfferEngine
from models import EvaluatedOffer, SearchReport
from safety import SafeError
from services.mercado_livre import API_ITEMS, API_PRODUCTS, MercadoLivreSource, SearchBatch
from services.mercado_livre_items import item_public_url, normalize_listing, safe_fields
from services.offer_filter import score_offer
from services.offer_formatter import format_dry_run_report, format_search_summary
from services.offer_relevance import assess_relevance
from telegram_service import TelegramService
from test_mercado_livre_catalog import catalog, response
from test_mvp import IsolatedTest, NOW, test_directory, update


def listing(identity="MLB100", **changes):
    item = {
        "id": identity, "site_id": "MLB", "status": "active",
        "title": "Mouse gamer Logitech", "condition": "new", "buying_mode": "buy_it_now",
        "currency_id": "BRL", "domain_id": "MLB-COMPUTER_MICE", "category_id": "MLB1714",
        "permalink": f"https://produto.mercadolivre.com.br/MLB-{identity[3:]}-mouse",
        "shipping": {"free_shipping": True},
        "attributes": [{"id": "BRAND", "value_name": "Logitech"}],
        "pictures": [{"secure_url": "https://http2.mlstatic.com/test-only.jpg"}],
    }
    item.update(changes)
    return item


def sale_price(**changes):
    payload = {"amount": 60, "regular_amount": 100, "currency_id": "BRL"}
    payload.update(changes)
    return payload


def bulk(*items, status=200):
    return [{"id": item["id"], "status_code": status, "body": item} for item in items]


def offer(**changes):
    return normalize_listing(listing(**changes), sale_price(), "mouse gamer")[0]


class ItemNormalizationTests(IsolatedTest):
    def test_active_item_without_catalog_is_a_real_candidate(self):
        p, reason = normalize_listing(listing(), sale_price(), "mouse gamer")
        self.assertEqual((p.external_id, p.item_id), ("item:MLB100", "MLB100"))
        self.assertIsNone(p.product_id)
        self.assertIsNone(p.catalog_product_id)
        self.assertEqual((p.price, p.original_price, p.discount_percentage), (6000, 10000, 40))
        self.assertEqual((p.currency_id, p.price_source, p.source), ("BRL", "sale_price", "mercado_livre"))
        self.assertEqual((p.domain_id, p.category_id), ("MLB-COMPUTER_MICE", "MLB1714"))
        self.assertIn("ELEGÍVEL", reason)

    def test_invalid_basic_fields_reject_with_an_explicit_reason(self):
        cases = (
            ({"id": "MLA100"}, "item_id"), ({"site_id": "MLA"}, "site_id"),
            ({"status": "paused"}, "status"), ({"status": None}, "status"),
            ({"buying_mode": "auction"}, "buying_mode"), ({"title": None}, "título"),
            ({"condition": "used"}, "condição"), ({"condition": None}, "condição"),
            ({"available_quantity": 0}, "estoque"), ({"available_quantity": -1}, "estoque"),
            ({"tags": ["non_buyable_as_standalone"]}, "isoladamente"),
            ({"tags": ["test_item"]}, "teste"), ({"channels": ["mshops"]}, "marketplace"),
            ({"permalink": None}, "permalink"),
        )
        for changes, expected in cases:
            with self.subTest(fields=tuple(changes)):
                p, reason = normalize_listing(listing(**changes), sale_price(), "mouse gamer")
                self.assertIsNone(p)
                self.assertIn(expected, reason)

    def test_item_condition_attribute_replaces_deprecated_condition(self):
        item = listing(condition=None, attributes=[{"id": "ITEM_CONDITION", "value_id": "2230284"}])
        self.assertIsNotNone(normalize_listing(item, sale_price(), "mouse gamer")[0])
        item["attributes"][0]["value_id"] = "2230581"
        self.assertIsNone(normalize_listing(item, sale_price(), "mouse gamer")[0])

    def test_null_buybox_in_catalog_does_not_remove_item(self):
        item = listing(catalog_product_id="MLB900", pictures=None, attributes=None)
        p, _ = normalize_listing(item, sale_price(), "mouse gamer",
                                 enrichment=catalog("MLB900", buy_box_winner=None))
        self.assertEqual((p.item_id, p.catalog_product_id), ("MLB100", "MLB900"))
        self.assertEqual(p.brand, "Logitech")
        self.assertTrue(p.image_url)

    def test_catalog_winner_never_replaces_item_price_id_or_link(self):
        item = listing(catalog_product_id="MLB900", pictures=None, attributes=None)
        detail = catalog("MLB900", "MLB777", name="Outro nome de catálogo")
        detail["buy_box_winner"].update(price=1, original_price=10000, sold_quantity=9999)
        p, _ = normalize_listing(item, sale_price(), "mouse gamer", enrichment=detail)
        self.assertEqual((p.price, p.original_price, p.item_id), (6000, 10000, "MLB100"))
        self.assertEqual((p.url, p.title), (item["permalink"], item["title"]))
        self.assertIsNone(p.sold_quantity)

    def test_wrong_catalog_id_cannot_enrich(self):
        item = listing(catalog_product_id="MLB900", attributes=None, pictures=None)
        p, _ = normalize_listing(item, sale_price(), "mouse gamer", enrichment=catalog("MLB999"))
        self.assertIsNone(p.brand)
        self.assertIsNone(p.image_url)

    def test_deprecated_item_prices_never_fill_missing_sale_price(self):
        item = listing(price=1, original_price=200, base_price=300)
        for price in (None, {}, {"amount": None, "currency_id": "BRL"}):
            p, reason = normalize_listing(item, price, "mouse gamer")
            self.assertIsNone(p)
            self.assertIn("sale_price.amount", reason)

    def test_invalid_current_prices_and_currency_are_rejected(self):
        for amount in (None, 0, -1, True, "NaN", "Infinity", {}, 100_000_000):
            with self.subTest(amount=amount):
                self.assertIsNone(normalize_listing(listing(), sale_price(amount=amount), "mouse")[0])
        for changes in ({"currency_id": "USD"}, {"currency_id": None}):
            self.assertIsNone(normalize_listing(listing(), sale_price(**changes), "mouse")[0])

    def test_original_is_only_valid_official_regular_amount(self):
        for regular, expected in ((None, None), (0, None), (True, None), ("NaN", None),
                                  (60, None), (59, None), (100, 10000)):
            p, _ = normalize_listing(listing(original_price=999, base_price=999),
                                     sale_price(regular_amount=regular), "mouse gamer")
            self.assertEqual(p.original_price, expected)
            if expected is None:
                self.assertIsNone(p.discount_percentage)
                self.assertNotIn("discount", score_offer(p, []).criteria)

    def test_price_and_discount_rounding(self):
        p, _ = normalize_listing(listing(), sale_price(amount=179.9, regular_amount=249.9), "mouse gamer")
        self.assertEqual(p.price, 17990)
        self.assertEqual(p.discount_percentage, 28.01)

    def test_third_party_sales_inventory_reputation_and_stars_stay_unknown(self):
        item = listing(sold_quantity=5000, available_quantity=50,
                       seller={"reputation_level_id": "GREEN"}, reviews={"rating_average": 5})
        p, _ = normalize_listing(item, sale_price(), "mouse gamer")
        for field in ("sold_quantity", "available_quantity", "seller_reputation", "rating"):
            self.assertIsNone(getattr(p, field))
        self.assertNotIn("sold", score_offer(p, []).criteria)
        self.assertNotIn("reputation", score_offer(p, []).criteria)

    def test_shipping_is_only_a_real_boolean(self):
        for value, expected in ((True, True), (False, False), (None, None), (1, None), ("true", None)):
            p = offer(shipping={"free_shipping": value})
            self.assertIs(p.free_shipping, expected)
            self.assertEqual(score_offer(p, []).criteria.get("free_shipping", 0), 10 if expected is True else 0)
        self.assertIsNone(offer(shipping=None).free_shipping)

    def test_direct_permalink_preserves_identity_and_strips_tracking(self):
        base = listing()["permalink"]
        self.assertEqual(item_public_url(base + "?affiliate=test#private", "MLB100"), base)
        for value in (base.replace("MLB-100", "MLB-999"), "https://meli.la/affiliate",
                      "https://evil.example/MLB100", "https://user:password@mercadolivre.com.br/MLB100"):
            self.assertIsNone(item_public_url(value, "MLB100"))

    def test_catalog_permalink_requires_explicit_item_selection(self):
        link = "https://www.mercadolivre.com.br/p/MLB900"
        self.assertIsNone(item_public_url(link, "MLB100"))
        self.assertIsNone(item_public_url(link + "?pdp_filters=item_id:MLB999", "MLB100"))
        self.assertEqual(item_public_url(link + "?pdp_filters=item_id:MLB100&affiliate=x", "MLB100"),
                         link + "?pdp_filters=item_id%3AMLB100")

    def test_private_or_unknown_fields_never_enter_diagnostics(self):
        fields = safe_fields({"id": "MLB100", "price": "fake-private-value",
                              "fake-private-key": 1, "Authorization": "Bearer fake-token",
                              "seller_address": {"email": "private@example.test"}})
        self.assertEqual(fields, ("id:str", "price:str"))
        self.assertNotIn("private", repr(fields))

    def test_token_echo_is_redacted_and_sensitive_permalink_rejected(self):
        secret = "fake-test-access-token"
        p, _ = normalize_listing(listing(title="Mouse gamer " + secret),
                                 sale_price(), "mouse gamer", secrets=(secret,))
        self.assertNotIn(secret, repr(p))
        p, reason = normalize_listing(listing(permalink=listing()["permalink"] + "-" + secret),
                                      sale_price(), "mouse gamer", secrets=(secret,))
        self.assertIsNone(p)
        self.assertNotIn(secret, reason)


class ItemRelevanceAndScoreTests(IsolatedTest):
    def test_mousepad_parts_stickers_and_unrelated_items_are_not_mice(self):
        titles = ("Mousepad gamer", "Cabo para mouse gamer", "Adesivo mouse gamer",
                  "Peça de reposição mouse", "Receptor para mouse gamer", "Sofá para sala")
        for title in titles:
            with self.subTest(title=title):
                self.assertEqual(assess_relevance(title, "mouse gamer").points, 0)

    def test_title_domain_and_term_are_used_without_external_ai(self):
        self.assertEqual(assess_relevance("Mouse gamer Logitech", "mouse gamer").points, 10)
        self.assertEqual(assess_relevance("Logitech G305", "mouse gamer", domain="MLB-COMPUTER_MICE").points, 5)
        self.assertEqual(assess_relevance("Mouse gamer", "mouse gamer", domain="MLB-MOUSE_PADS").points, 0)
        self.assertEqual(assess_relevance("SSD NVMe Kingston", "ssd nvme").points, 10)

    def test_plural_accessories_are_also_rejected(self):
        for title in ("Adesivos mouse gamer", "Cabos para mouse gamer", "Peças para mouse gamer",
                      "Kit de botões mouse gamer", "Receptores para mouse gamer", "Skate para mouse gamer"):
            with self.subTest(title=title):
                self.assertEqual(assess_relevance(title, "mouse gamer").points, 0)

    def test_negative_keywords_are_configurable_accent_insensitive_and_whole_word(self):
        self.assertEqual(assess_relevance("Mouse gamer réplica", "mouse gamer",
                                         negative_keywords=("replica",)).points, 0)
        self.assertGreater(assess_relevance("Mouse gamer Logitech", "mouse gamer",
                                           negative_keywords=("log",)).points, 0)
        self.assertEqual(assess_relevance("Mouse gamer", "mouse gamer", negative_keywords=()).points, 10)

    def test_partial_score_does_not_invent_quality_or_rescale_to_100(self):
        p = offer()
        result = score_offer(p, [])
        self.assertEqual(result.criteria, {"discount": 35, "free_shipping": 10, "brand": 5, "relevance": 10})
        self.assertEqual(result.score, 60)
        self.assertFalse(result.approved(70))

    def test_no_discount_and_brand_alone_do_not_approve(self):
        p, _ = normalize_listing(listing(), sale_price(regular_amount=None), "mouse gamer")
        result = score_offer(p, [])
        self.assertEqual(result.score, 25)
        self.assertFalse(result.approved(0))
        self.assertEqual(score_offer(replace(p, free_shipping=None, relevance_score=None), []).score, 5)

    def test_discount_bands_use_real_prices(self):
        for current, expected in ((60, 35), (70, 30), (80, 22), (90, 12), (95, 0)):
            p, _ = normalize_listing(listing(), sale_price(amount=current), "mouse gamer")
            self.assertEqual(score_offer(p, []).criteria["discount"], expected)

    def test_history_needs_three_previous_observations_and_caps_at_25(self):
        p = offer()
        for prices in ([], [10000], [10000, 10000]):
            self.assertNotIn("history", score_offer(p, prices).criteria)
        result = score_offer(p, [10000, 11000, 12000])
        self.assertEqual((result.criteria["history"], result.score), (25, 85))
        self.assertTrue(result.approved(70))

    def test_penalties_and_score_bounds(self):
        p = offer(title="Mouse gamer !!!")
        result = score_offer(p, [100000] * 3, recently_published=True)
        self.assertEqual(result.criteria["suspicious_title"], -10)
        self.assertEqual(result.criteria["price_inconsistency"], -25)
        self.assertEqual(result.criteria["recent_publication"], -10)
        self.assertGreaterEqual(result.score, 0)
        full = replace(offer(), sold_quantity=10000, seller_reputation="GREEN", relevance_score=100)
        self.assertEqual(score_offer(full, [10000] * 3).score, 100)


class ItemProviderTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.source = MercadoLivreSource(item_ids=("MLB100",))
        self.get = self.enterContext(patch("services.mercado_livre.requests.Session.get", autospec=True))
        self.token = self.enterContext(patch("services.mercado_livre.get_valid_access_token",
                                            return_value="fake-item-access-token"))
        self.pace = self.enterContext(patch.object(self.source, "_pace"))
        self.wait = self.enterContext(patch.object(self.source, "_wait"))
        self.enterContext(patch("services.mercado_livre.random.uniform", return_value=0))

    def test_official_bulk_and_sale_price_with_secure_session(self):
        self.get.side_effect = [response(bulk(listing())), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual(len(batch.products), 1)
        self.assertFalse(batch.errors)
        self.assertEqual([c.args[1] for c in self.get.call_args_list],
                         [API_ITEMS + "/bulk", API_ITEMS + "/MLB100/sale_price"])
        params = self.get.call_args_list[0].kwargs["params"]
        self.assertEqual(params["ids"], "MLB100")
        self.assertTrue(all(field.startswith("body.") for field in params["attributes"].split(",")))
        self.assertNotIn("body.seller_address", params["attributes"])
        self.assertEqual(self.get.call_args.kwargs["params"], {"context": "channel_marketplace"})
        for invocation in self.get.call_args_list:
            session = invocation.args[0]
            self.assertFalse(session.trust_env)
            self.assertTrue(session.verify)
            self.assertEqual(invocation.kwargs["timeout"], (5, 20))
            self.assertFalse(invocation.kwargs["allow_redirects"])
        self.assertEqual(self.token.call_args_list, [call(), call()])

    def test_no_ids_reports_unavailable_discovery_without_http(self):
        batch = MercadoLivreSource().search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertIn("Descoberta global", str(batch.errors[0]))
        self.get.assert_not_called()
        self.token.assert_not_called()

    def test_empty_bulk_is_not_misreported_as_no_offers_success(self):
        self.get.return_value = response([])
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.errors)
        self.assertTrue(batch.stop)
        self.assertIn("ausente", batch.diagnostics[0].reason)
        self.get.assert_called_once()

    def test_inactive_item_does_not_fetch_price_or_catalog(self):
        self.get.return_value = response(bulk(listing(status="paused", catalog_product_id="MLB900")))
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.products)
        self.assertIn("status", batch.diagnostics[0].reason)
        self.get.assert_called_once()

    def test_catalog_is_optional_and_null_winner_is_accepted(self):
        self.get.side_effect = [response(bulk(listing(catalog_product_id="MLB900", attributes=None))),
                                response(sale_price()), response(catalog("MLB900", buy_box_winner=None))]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual(len(batch.products), 1)
        self.assertEqual(batch.products[0].brand, "Logitech")
        self.assertEqual(self.get.call_args.args[1], API_PRODUCTS + "/MLB900")
        self.assertFalse(batch.errors)

    def test_complete_item_does_not_fetch_catalog_even_when_id_exists(self):
        self.get.side_effect = [response(bulk(listing(catalog_product_id="MLB900"))), response(sale_price())]
        self.assertEqual(len(self.source.search("mouse gamer", 5).products), 1)
        self.assertEqual(self.get.call_count, 2)

    def test_catalog_403_or_404_does_not_drop_item(self):
        for status in (403, 404):
            with self.subTest(status=status):
                self.source.begin_cycle()
                self.get.side_effect = [response(bulk(listing(catalog_product_id="MLB900", attributes=None))),
                                        response(sale_price()), response(status=status)]
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(len(batch.products), 1)
                self.assertFalse(batch.stop)
                self.assertTrue(any(d.http_status == status and "preservado" in d.reason for d in batch.diagnostics))

    def test_two_items_same_catalog_share_enrichment_but_keep_separate_prices(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [
            response(bulk(listing(catalog_product_id="MLB900", attributes=None),
                          listing("MLB101", catalog_product_id="MLB900", attributes=None))),
            response(sale_price()), response(catalog("MLB900", buy_box_winner=None)),
            response(sale_price(amount=70)),
        ]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.price for p in batch.products], [6000, 7000])
        self.assertEqual(sum(c.args[1] == API_PRODUCTS + "/MLB900" for c in self.get.call_args_list), 1)

    def test_caches_details_and_prices_across_terms_but_resets_next_cycle(self):
        self.get.side_effect = [response(bulk(listing())), response(sale_price()),
                                response(bulk(listing())), response(sale_price(amount=55))]
        self.source.search("mouse gamer", 5)
        second = self.source.search("headset gamer", 5)
        self.assertEqual(second.products[0].relevance_score, 0)
        self.assertEqual(self.get.call_count, 2)
        self.source.begin_cycle()
        self.assertEqual(self.source.search("mouse gamer", 5).products[0].price, 5500)
        self.assertEqual(self.get.call_count, 4)

    def test_ids_are_deduplicated_and_small_limit_is_honored(self):
        source = MercadoLivreSource(item_ids=("MLB100", "MLB100", "MLB101"))
        self.get.side_effect = [response(bulk(listing())), response(sale_price())]
        with patch.object(source, "_pace"):
            batch = source.search("mouse gamer", 1)
        self.assertEqual(batch.found_ids, {"MLB100"})
        self.assertEqual(self.get.call_args_list[0].kwargs["params"]["ids"], "MLB100")
        self.assertEqual(self.get.call_count, 2)

    def test_bulk_item_404_keeps_other_items_and_diagnostic(self):
        self.source.item_ids = ("MLB100", "MLB101")
        payload = [{"id": "MLB100", "status_code": 404, "body": {}}] + bulk(listing("MLB101"))
        self.get.side_effect = [response(payload), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        self.assertEqual(batch.errors[0].status, 404)
        self.assertTrue(any(d.item_id == "MLB100" and d.http_status == 404 for d in batch.diagnostics))

    def test_sale_price_404_does_not_use_old_item_price(self):
        self.get.side_effect = [response(bulk(listing(price=1, original_price=2))), response(status=404)]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.products)
        self.assertEqual(batch.errors[0].status, 404)
        self.assertIn("price ausente", batch.diagnostics[-1].reason)

    def test_malformed_bulk_contract_has_safe_error(self):
        payloads = ({},
                    [{"id": "MLB100", "status_code": [], "body": listing()}],
                    bulk(listing()) + bulk(listing()))
        for payload in payloads:
            with self.subTest(kind=type(payload).__name__):
                self.source.begin_cycle()
                self.get.side_effect = [response(payload), response(listing("MLB999"))]
                batch = self.source.search("mouse gamer", 5)
                self.assertFalse(batch.products)
                self.assertTrue(batch.errors)

    def test_bulk_detail_mismatch_is_rejected(self):
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200, "body": listing("MLB999")}]),
                                response(listing("MLB999"))]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.products)
        self.assertIn("não corresponde", batch.diagnostics[0].reason)
        self.assertEqual(self.get.call_count, 2)
        self.assertFalse(batch.stop)

    def test_http_206_public_partial_item_is_accepted(self):
        self.get.side_effect = [response(bulk(listing(), status=206), status=206), response(sale_price())]
        self.assertEqual(len(self.source.search("mouse gamer", 5).products), 1)

    def test_http_401_refreshes_once_and_uses_new_token(self):
        self.token.side_effect = ["fake-old-token", "fake-new-token", "fake-new-token"]
        self.get.side_effect = [response(status=401), response(bulk(listing())), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.errors)
        self.assertEqual(self.token.call_args_list, [call(), call(force_refresh=True), call()])
        self.assertEqual(self.get.call_args_list[1].kwargs["headers"]["Authorization"], "Bearer fake-new-token")

    def test_repeated_401_stops_without_infinite_refresh(self):
        self.get.side_effect = [response(status=401), response(status=401)]
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertEqual(batch.errors[0].status, 401)
        self.assertEqual(self.get.call_count, 2)
        self.assertEqual(self.token.call_args_list, [call(), call(force_refresh=True)])

    def test_http_403_404_and_5xx_stop_safely(self):
        for status in (403, 404, 500, 503):
            with self.subTest(status=status):
                self.source.begin_cycle()
                self.get.reset_mock()
                self.get.return_value = response({"message": "fake-private-error-body"}, status=status)
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(batch.errors[0].status, status)
                self.assertTrue(batch.stop)
                self.assertNotIn("private", repr(batch))
                self.assertEqual(self.get.call_count, 3 if status >= 500 else 1)

    def test_429_retry_after_is_respected_on_item_endpoint(self):
        self.get.side_effect = [response(status=429, headers={"Retry-After": "12"}),
                                response(bulk(listing())), response(sale_price())]
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.errors)
        self.wait.assert_called_once_with(12)
        self.assertIn("Endpoint: /items/bulk", " ".join(logs.output))
        self.assertIn("Tentativa: 1/3", " ".join(logs.output))

    def test_429_without_header_uses_bounded_backoff(self):
        self.get.side_effect = [response(status=429), response(status=429), response(status=429)]
        with self.assertLogs("services.mercado_livre", level="WARNING"):
            batch = self.source.search("mouse gamer", 5)
        self.assertEqual(self.wait.call_args_list, [call(5), call(10)])
        self.assertEqual(self.get.call_count, 3)
        self.assertTrue(batch.stop)
        self.assertEqual(batch.errors[0].status, 429)
        self.assertGreaterEqual(batch.errors[0].retry_after, 60)

    def test_429_jitter_is_added_without_shortening_retry_after(self):
        self.get.side_effect = [response(status=429, headers={"Retry-After": "12"}),
                                response(bulk(listing())), response(sale_price())]
        with patch("services.mercado_livre.random.uniform", return_value=1.25), \
             self.assertLogs("services.mercado_livre", level="WARNING"):
            self.source.search("mouse gamer", 5)
        self.wait.assert_called_once_with(13.25)

    def test_per_item_429_envelope_stops_without_aggressive_retries(self):
        self.get.return_value = response([{"id": "MLB100", "status_code": 429, "body": {}}])
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertEqual(batch.errors[0].status, 429)
        self.assertGreaterEqual(batch.errors[0].retry_after, 60)
        self.get.assert_called_once()

    def test_timeout_does_not_echo_request_or_exception(self):
        self.get.side_effect = requests.Timeout("Authorization: Bearer fake-private-token")
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertIn("timeout", str(batch.errors[0]))
        self.assertNotIn("private", repr(batch))
        self.assertNotIn("Authorization", repr(batch))

    def test_partial_success_survives_later_failure(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response(bulk(listing(), listing("MLB101"))),
                                response(sale_price()), *[response(status=503) for _ in range(3)]]
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertEqual([p.item_id for p in batch.products], ["MLB100"])
        self.assertEqual(batch.diagnostics[-1].item_id, "MLB101")

    def test_enrichment_429_preserves_already_observed_item(self):
        self.get.side_effect = [response(bulk(listing(catalog_product_id="MLB900", attributes=None))),
                                response(sale_price()), *[response(status=429) for _ in range(3)]]
        with self.assertLogs("services.mercado_livre", level="WARNING"):
            batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertEqual(len(batch.products), 1)
        self.assertEqual(batch.products[0].price, 6000)

    def test_delay_configuration_applies_after_success(self):
        source = MercadoLivreSource(item_ids=("MLB100",), request_interval_seconds=2.5)
        self.get.side_effect = [response(bulk(listing())), response(sale_price())]
        with patch.object(source, "_wait") as wait, \
             patch("services.mercado_livre.time.monotonic", return_value=100):
            source.search("mouse gamer", 5)
        self.assertEqual(wait.call_args_list, [call(0), call(2.5)])

    def test_logs_and_diagnostics_do_not_contain_headers_or_tokens(self):
        secret = "fake-item-access-token"
        self.get.side_effect = [
            response({"message": secret}, status=429,
                     headers={"Retry-After": secret, "X-RateLimit-Remaining": secret,
                              "Authorization": "Bearer " + secret, "Set-Cookie": "private-cookie"}),
            response(bulk(listing(title="Mouse gamer " + secret, seller_address={"email": "private-email"}))),
            response(sale_price()),
        ]
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            batch = self.source.search("mouse gamer", 5)
        report = SearchReport(
            offers=[EvaluatedOffer(p, score_offer(p, []), "análise") for p in batch.products],
            diagnostics=batch.diagnostics)
        text = "\n".join(logs.output) + repr(batch) + format_dry_run_report(report, 70)
        for private in (secret, "Authorization", "private-cookie", "private-email", "seller_address"):
            self.assertNotIn(private, text)


class ItemPipelineTests(IsolatedTest):
    def test_sqlite_history_is_per_item_and_catalog_can_have_many_sellers(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            a = offer(catalog_product_id="MLB900")
            b = normalize_listing(listing("MLB101", catalog_product_id="MLB900"),
                                  sale_price(amount=65), "mouse gamer")[0]
            first, _ = db.observe(a, NOW, 30)
            second, _ = db.observe(b, NOW, 30)
            self.assertNotEqual(first, second)
            _, previous = db.observe(replace(a, price=5500), NOW + timedelta(hours=1), 30)
            self.assertEqual(previous, [6000])
            with db.connect() as con:
                rows = list(con.execute("SELECT item_id,catalog_product_id,product_id,price_source,category_id FROM products ORDER BY item_id"))
                self.assertEqual([tuple(r) for r in rows],
                                 [("MLB100", "MLB900", None, "sale_price", "MLB1714"),
                                  ("MLB101", "MLB900", None, "sale_price", "MLB1714")])
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 3)

    def test_same_item_same_instant_does_not_duplicate_history(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            p = offer()
            self.assertEqual(db.observe(p, NOW, 30)[0], db.observe(p, NOW, 30)[0])
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM products").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)

    def test_migration_preserves_legacy_catalog_history_separately(self):
        with test_directory() as directory:
            path = directory / "test.db"
            db = Database(path)
            old = replace(offer(), external_id="catalog:MLB900", product_id="MLB900", catalog_product_id=None)
            legacy_id, _ = db.observe(old, NOW, 30)
            with db.connect() as con:
                con.execute("PRAGMA user_version=2")
            db = Database(path)
            new_id, history = db.observe(offer(catalog_product_id="MLB900"), NOW + timedelta(hours=1), 30)
            self.assertNotEqual(legacy_id, new_id)
            self.assertEqual(history, [])
            with db.connect() as con:
                self.assertEqual(con.execute("PRAGMA user_version").fetchone()[0], 3)
                self.assertEqual(con.execute("SELECT catalog_product_id FROM products WHERE id=?", (legacy_id,)).fetchone()[0], "MLB900")
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history WHERE product_id=?", (legacy_id,)).fetchone()[0], 1)

    def test_engine_deduplicates_across_terms_and_keeps_best_relevance(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            p = offer()
            low = replace(p, relevance_score=0, search_term="headset")
            source = Mock()
            source.search.side_effect = [
                SearchBatch(products=[low, low], found_ids={"MLB100"}),
                SearchBatch(products=[p, p], found_ids={"MLB100"}),
            ]
            telegram = Mock()
            cfg = Config(search_terms=("headset", "mouse gamer"), search_terms_per_cycle=2)
            engine = OfferEngine(cfg, db, source, telegram, clock=lambda: NOW, reporter=Mock())
            with patch.object(engine._stop, "wait", return_value=False):
                engine.run_search()
            self.assertEqual(engine.last_report.status, "completed")
            self.assertEqual(len(engine.last_report.offers), 1)
            self.assertEqual(engine.last_report.offers[0].product.relevance_score, 10)
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM offer_evaluations").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
            telegram.publish_offer.assert_not_called()

    def test_valid_but_irrelevant_observation_saved_without_approval(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            source = Mock()
            source.search.return_value = SearchBatch(products=[replace(offer(), relevance_score=0)], found_ids={"MLB100"})
            cfg = Config(search_terms=("mouse",), min_offer_score=0)
            engine = OfferEngine(cfg, db, source, Mock(), clock=lambda: NOW, reporter=Mock())
            engine.run_search()
            self.assertEqual(engine.last_report.valid_ids, {"MLB100"})
            self.assertFalse(engine.last_report.relevant_ids)
            self.assertFalse(engine.last_report.offers)
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT approved FROM offer_evaluations").fetchone()[0], 0)

    def test_republication_rules_still_apply_to_item_identity(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            p = offer()
            identity, _ = db.observe(p, NOW, 30)
            cfg = Config()
            reservation, _ = db.reserve(identity, p, 85, NOW, cfg, simulate=True)
            self.assertIsNotNone(reservation)
            reservation, reason = db.reserve(identity, p, 85, NOW + timedelta(hours=1), cfg, simulate=True)
            self.assertIsNone(reservation)
            self.assertIn("recentemente", reason)
            reservation, _ = db.reserve(identity, replace(p, price=5000), 85, NOW + timedelta(hours=2), cfg, simulate=True)
            self.assertIsNotNone(reservation)
            with db.connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)

    def test_recent_legacy_catalog_publication_blocks_same_item_after_migration(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            old = replace(offer(), external_id="catalog:MLB900", product_id="MLB900")
            old_id, _ = db.observe(old, NOW, 30)
            publication, _ = db.reserve(old_id, old, 85, NOW, Config(), simulate=False)
            db.confirm_publication(publication, 1, NOW)
            current = offer()
            new_id, history = db.observe(current, NOW + timedelta(hours=1), 30)
            self.assertFalse(history)
            self.assertTrue(db.recently_published(new_id, NOW + timedelta(hours=1), 72))
            for price in (current.price, 1000):
                reservation, reason = db.reserve(new_id, replace(current, price=price), 85,
                                                 NOW + timedelta(hours=1), Config(), simulate=True)
                self.assertIsNone(reservation)
                self.assertIn("recentemente", reason)

    def test_unconfirmed_legacy_send_also_blocks_new_item_identity(self):
        with test_directory() as directory:
            db = Database(directory / "test.db")
            old = replace(offer(), external_id="MLB100", item_id=None)
            old_id, _ = db.observe(old, NOW, 30)
            db.reserve(old_id, old, 85, NOW, Config(), simulate=False)
            db.recover_interrupted()
            db.set_state("paused", False)
            current = offer()
            new_id, _ = db.observe(current, NOW + timedelta(hours=80), 30)
            reservation, reason = db.reserve(new_id, current, 85, NOW + timedelta(hours=80),
                                             Config(), simulate=True)
            self.assertIsNone(reservation)
            self.assertIn("sem confirmação", reason)

    def test_default_cli_pipeline_forces_false_and_never_contacts_telegram(self):
        with test_directory() as directory:
            cfg = Config(database_path=directory / "test.db", auto_publish=True,
                         search_terms=("notebook",), meli_item_ids=("MLB100", "MLB101"),
                         min_offer_score=50)
            output = io.StringIO()
            with patch("services.mercado_livre.requests.Session.get", autospec=True,
                       side_effect=[response(bulk(listing(), listing("MLB101", title="Mousepad isolado"))),
                                    response(sale_price()), response(sale_price())]) as get, \
                 patch("services.mercado_livre.get_valid_access_token", return_value="fake-access"), \
                 patch("services.mercado_livre.MercadoLivreSource._pace"), \
                 patch.object(TelegramService, "publish_offer") as publish, \
                 patch.object(TelegramService, "reply") as reply, \
                 patch.object(TelegramService, "get_updates") as poll, redirect_stdout(output):
                result = main.run(cfg, search_now=True, search_limit=5)
            self.assertEqual(result, 0)
            self.assertEqual(get.call_count, 3)
            publish.assert_not_called()
            reply.assert_not_called()
            poll.assert_not_called()
            text = output.getvalue()
            for line in ("Termos pesquisados: 1", "Itens encontrados: 2", "Itens válidos: 2",
                         "Itens relevantes: 1", "TODAS AS OFERTAS ELEGÍVEIS", "TOP OFERTAS:",
                         "Item ID: MLB100", "baixa relevância", "SERIA PUBLICADA"):
                self.assertIn(line, text)
            with Database(cfg.database_path).connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM simulations").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT value FROM system_state WHERE key='auto_publish'").fetchone()[0], "false")

    def test_admin_buscar_uses_engine_and_returns_private_completion_summary(self):
        with test_directory() as directory:
            cfg = Config(telegram_admin_id=42, search_terms=("mouse gamer",))
            db = Database(directory / "test.db")
            source = Mock()
            source.search.return_value = SearchBatch(products=[offer()], found_ids={"MLB100"})
            telegram = Mock()
            engine = OfferEngine(cfg, db, source, telegram, clock=lambda: NOW, reporter=Mock())
            admin = AdminBot(cfg, db, engine, telegram)
            admin.handle_update(update("/buscar"))
            self.assertTrue(engine._requested.is_set())
            engine.run_search()
            self.assertEqual(telegram.reply.call_count, 2)
            self.assertEqual(telegram.reply.call_args.args[0], 42)
            self.assertIn("Itens relevantes: 1", telegram.reply.call_args.args[1])
            self.assertIn("nenhuma oferta", telegram.reply.call_args.args[1])
            telegram.publish_offer.assert_not_called()

    def test_unauthorized_buscar_never_queues_or_leaks_summary(self):
        with test_directory() as directory:
            cfg = Config(telegram_admin_id=42)
            db = Database(directory / "test.db")
            telegram, engine = Mock(), Mock()
            AdminBot(cfg, db, engine, telegram).handle_update(update("/buscar", user_id=7))
            engine.request_search.assert_not_called()
            telegram.reply.assert_called_once_with(7, "Não autorizado.")

    def test_duplicate_search_requests_do_not_redirect_completion(self):
        with test_directory() as directory:
            cfg = Config(telegram_admin_id=42, search_terms=("mouse",))
            db = Database(directory / "test.db")
            source, telegram = Mock(), Mock()
            source.search.return_value = SearchBatch()
            engine = OfferEngine(cfg, db, source, telegram, clock=lambda: NOW, reporter=Mock())
            self.assertTrue(engine.request_search(42))
            self.assertFalse(engine.request_search(7))
            engine.run_search()
            telegram.reply.assert_called_once()
            self.assertEqual(telegram.reply.call_args.args[0], 42)
            self.assertTrue(engine.request_search(7))
            engine.run_search()
            self.assertEqual(telegram.reply.call_count, 1)

    def test_all_scores_are_visible_and_top_is_limited(self):
        entries = []
        for number in range(15):
            p = replace(offer(), item_id="MLB" + str(number), title="Mouse gamer " + str(number))
            entries.append(EvaluatedOffer(p, score_offer(p, []), "score abaixo do threshold"))
        report = SearchReport(offers=entries)
        text = format_dry_run_report(report, 70)
        self.assertEqual(text.count("Radar Score: 60/100"), 15)
        self.assertEqual(text.count("Status: REPROVADO"), 15)
        top = text.split("TOP OFERTAS:")[1]
        self.assertIn("10. ", top)
        self.assertNotIn("11. ", top)
        self.assertNotIn("Vendas:", text)
        self.assertNotIn("Avaliação:", text)

    def test_private_summary_does_not_claim_dry_run_in_real_mode(self):
        self.assertNotIn("nenhuma oferta enviada", format_search_summary(SearchReport(), 70, auto_publish=True))


class ItemConfigTests(IsolatedTest):
    def test_ids_and_negatives_from_mapping_without_dotenv(self):
        config = load_config({"TELEGRAM_BOT_TOKEN": "fake-test-token", "MELI_ITEM_IDS": " MLB100,MLB100,MLB101 ",
                              "NEGATIVE_KEYWORDS": "réplica,peça"})
        self.assertEqual(config.meli_item_ids, ("MLB100", "MLB101"))
        self.assertEqual(config.negative_keywords, ("réplica", "peça"))
        self.assertFalse(config.auto_publish)

    def test_invalid_ids_never_echo_input(self):
        for value in ("fake-private-token", "MLA100", "MLB1/other", "MLB1?access_token=fake"):
            with self.assertRaises(ConfigError) as caught:
                parse_item_ids(value)
            self.assertNotIn(value, str(caught.exception))

    def test_cli_item_ids_are_validated(self):
        self.assertEqual(main._item_ids("MLB100,MLB101"), ("MLB100", "MLB101"))
        import argparse
        with self.assertRaises(argparse.ArgumentTypeError):
            main._item_ids("")
