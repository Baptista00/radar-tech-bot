"""Migração do bulk e recuperação de itens: respostas sintéticas, rede bloqueada."""

import io
from contextlib import redirect_stdout
from unittest.mock import Mock, patch

import main
from config import Config
from database import Database
from models import SearchReport
from services.mercado_livre import API_ITEMS, MercadoLivreSource
from services.mercado_livre_items import ITEM_FIELDS
from services.offer_formatter import format_dry_run_report
from telegram_service import TelegramService
from test_mercado_livre_catalog import response
from test_mercado_livre_items import bulk, listing, sale_price
from test_mvp import IsolatedTest, test_directory


# Apenas identidades reais; títulos, URLs, preços e respostas abaixo são mocks.
REAL_IDS = ("MLB7390921738", "MLB6484766092", "MLB4144790865")


class BulkMigrationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.source = MercadoLivreSource(item_ids=("MLB100",))
        self.get = self.enterContext(patch("services.mercado_livre.requests.Session.get", autospec=True))
        self.token = self.enterContext(patch("services.mercado_livre.get_valid_access_token",
                                            return_value="fake-bulk-access-token"))
        self.enterContext(patch.object(self.source, "_pace"))
        self.wait = self.enterContext(patch.object(self.source, "_wait"))
        self.enterContext(patch("services.mercado_livre.random.uniform", return_value=0))

    def urls(self):
        return [invocation.args[1] for invocation in self.get.call_args_list]

    def test_current_schema_multiple_items_can_arrive_out_of_order(self):
        self.source.item_ids = REAL_IDS
        self.get.side_effect = [response(bulk(*(listing(identity) for identity in reversed(REAL_IDS)))),
                                response(sale_price(amount=51)), response(sale_price(amount=62)),
                                response(sale_price(amount=73))]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.item_id for p in batch.products], list(REAL_IDS))
        self.assertEqual([p.price for p in batch.products], [5100, 6200, 7300])
        self.assertFalse(batch.stop)
        self.assertFalse(batch.errors)
        self.assertEqual(self.urls(), [API_ITEMS + "/bulk", *[API_ITEMS + "/" + i + "/sale_price" for i in REAL_IDS]])

    def test_legacy_code_and_body_are_accepted_without_fallback(self):
        self.get.side_effect = [response([{"code": 200, "body": listing()}]), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.item_id for p in batch.products], ["MLB100"])
        self.assertFalse(batch.errors)
        self.assertEqual(self.get.call_count, 2)

    def test_id_only_at_root_is_copied_into_normalized_body(self):
        item = listing()
        del item["id"]
        payload = [{"id": "MLB100", "status_code": 200, "body": item}]
        self.get.side_effect = [response(payload), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual((batch.products[0].item_id, batch.products[0].external_id), ("MLB100", "item:MLB100"))
        self.assertFalse(batch.errors)
        self.assertEqual(self.get.call_count, 2)
        self.assertNotIn("id", item)

    def test_id_only_inside_body_is_recognized_with_current_status(self):
        self.get.side_effect = [response([{"status_code": 200, "body": listing()}]), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual(batch.products[0].item_id, "MLB100")
        self.assertEqual(self.get.call_count, 2)

    def test_root_id_conflict_uses_fallback_for_expected_item_only(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200, "body": listing("MLB101")}]
                                         + bulk(listing("MLB101"))), response(listing()),
                                response(sale_price()), response(sale_price(amount=70))]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.item_id for p in batch.products], ["MLB100", "MLB101"])
        self.assertEqual(self.urls().count(API_ITEMS + "/MLB100"), 1)
        self.assertNotIn(API_ITEMS + "/MLB101", self.urls())
        self.assertIn("não corresponde", batch.diagnostics[0].reason)

    def test_status_code_has_priority_over_legacy_code(self):
        self.source.item_ids = ("MLB100", "MLB101")
        denied = {"id": "MLB100", "status_code": 403, "code": 200, "body": listing()}
        self.get.side_effect = [response([denied] + bulk(listing("MLB101"))), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        self.assertEqual(batch.errors[0].status, 403)
        self.assertFalse(batch.stop)
        self.assertEqual(self.get.call_count, 2)
        self.assertIn("http_status=200", batch.diagnostics[0].fields)
        self.assertIn("status_code=403", batch.diagnostics[0].fields)

    def test_invalid_current_status_cannot_be_masked_by_legacy_success(self):
        for status in (None, True, "200", [], {}):
            with self.subTest(status_type=type(status).__name__):
                self.source.begin_cycle()
                self.get.side_effect = [response([{"id": "MLB100", "status_code": status, "code": 200, "body": listing()}]),
                                        response(listing()), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(batch.products[0].item_id, "MLB100")
                self.assertIn("status individual", batch.diagnostics[0].reason)
                self.assertEqual(self.get.call_args_list[-2].args[1], API_ITEMS + "/MLB100")

    def test_missing_body_falls_back_once_with_same_secure_session_and_token(self):
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200}]),
                                response(listing()), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual(len(batch.products), 1)
        self.assertFalse(batch.errors)
        self.assertEqual(self.urls(), [API_ITEMS + "/bulk", API_ITEMS + "/MLB100", API_ITEMS + "/MLB100/sale_price"])
        session = self.get.call_args_list[0].args[0]
        for invocation in self.get.call_args_list:
            self.assertIs(invocation.args[0], session)
            self.assertFalse(session.trust_env)
            self.assertTrue(session.verify)
            self.assertFalse(invocation.kwargs["allow_redirects"])
            self.assertEqual(invocation.kwargs["timeout"], (5, 20))
            self.assertEqual(invocation.kwargs["headers"]["Authorization"], "Bearer fake-bulk-access-token")
        self.assertEqual(self.get.call_args_list[1].kwargs["params"], {"attributes": ",".join(ITEM_FIELDS)})
        self.assertIn("body_present=false", batch.diagnostics[0].fields)
        self.assertTrue(any("RECUPERADO" in d.reason for d in batch.diagnostics))
        self.source.search("headset gamer", 5)
        self.assertEqual(self.get.call_count, 3)

    def test_null_body_has_safe_diagnostic_and_individual_fallback(self):
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200, "body": None}]),
                                response(listing()), response(sale_price())]
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            batch = self.source.search("mouse gamer", 5)
        text = " ".join(logs.output)
        for field in ("Bulk item MLB100", "root_keys=[id,status_code,body]", "status_code=200",
                      "body_present=true", "body_type=NoneType", "http_status=200"):
            self.assertIn(field, text)
        self.assertEqual(len(batch.products), 1)

    def test_non_object_body_is_never_normalized(self):
        for body in ([], "unexpected body", 123, True):
            with self.subTest(body_type=type(body).__name__):
                self.source.begin_cycle()
                self.get.side_effect = [response([{"id": "MLB100", "status_code": 200, "body": body}]),
                                        response(listing()), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(batch.products[0].item_id, "MLB100")
                self.assertIn("body_type=" + type(body).__name__, batch.diagnostics[0].fields)

    def test_missing_required_fields_are_retried_as_an_individual_detail(self):
        for missing_field in ("title", "site_id", "status", "buying_mode", "condition", "permalink"):
            with self.subTest(field=missing_field):
                self.source.begin_cycle()
                self.get.side_effect = [response(bulk(listing(**{missing_field: None}))),
                                        response(listing()), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(batch.products[0].item_id, "MLB100")
                self.assertIn("FALLBACK", batch.diagnostics[0].reason)

    def test_unidentified_or_unexpected_envelope_uses_requested_id_for_fallback(self):
        item = listing()
        del item["id"]
        for envelope in (None, "unexpected", {}, {"status_code": 200, "body": item}):
            with self.subTest(envelope_type=type(envelope).__name__):
                self.source.begin_cycle()
                self.get.side_effect = [response([envelope]), response(listing()), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertEqual(batch.products[0].item_id, "MLB100")
                self.assertEqual(self.get.call_args_list[-2].args[1], API_ITEMS + "/MLB100")

    def test_missing_first_item_and_individual_403_do_not_stop_second(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response(bulk(listing("MLB101"))), response(status=403), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        self.assertEqual(batch.errors[0].status, 403)
        self.assertEqual(self.urls(), [API_ITEMS + "/bulk", API_ITEMS + "/MLB100", API_ITEMS + "/MLB101/sale_price"])

    def test_individual_404_is_cached_and_later_items_continue(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200}] + bulk(listing("MLB101"))),
                                response(status=404), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertEqual(batch.errors[0].status, 404)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        second = self.source.search("headset gamer", 5)
        self.assertEqual(self.get.call_count, 3)
        self.assertTrue(any(d.item_id == "MLB100" and d.http_status == 404 for d in second.diagnostics))

    def test_invalid_individual_response_rejects_only_that_item(self):
        for detail in ([], None, {}, listing("MLB999"), listing(title=None)):
            with self.subTest(kind=type(detail).__name__):
                self.source.begin_cycle()
                self.source.item_ids = ("MLB100", "MLB101")
                self.get.side_effect = [response([{"id": "MLB100", "status_code": 200, "body": None}]
                                                 + bulk(listing("MLB101"))), response(detail), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertFalse(batch.stop)
                self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
                self.assertTrue(batch.errors)

    def test_duplicate_envelope_triggers_only_one_fallback_and_keeps_other_items(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response(bulk(listing(), listing(), listing("MLB101"))),
                                response(listing()), response(sale_price()), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertEqual([p.item_id for p in batch.products], ["MLB100", "MLB101"])
        self.assertEqual(self.urls().count(API_ITEMS + "/MLB100"), 1)

    def test_individual_error_in_http_200_does_not_stop_other_bulk_items(self):
        self.source.item_ids = ("MLB100", "MLB101")
        for status in (201, 400, 401, 403, 404, 500, 503):
            with self.subTest(status=status):
                self.source.begin_cycle()
                self.get.reset_mock()
                self.get.side_effect = [response(bulk(listing(), status=status) + bulk(listing("MLB101"))), response(sale_price())]
                batch = self.source.search("mouse gamer", 5)
                self.assertFalse(batch.stop)
                self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
                self.assertEqual(batch.errors[0].status, status)
                self.assertEqual(self.get.call_count, 2)

    def test_completely_invalid_bulk_response_stops_globally_without_fallback(self):
        for payload in (None, {}, "private-response", 123, []):
            with self.subTest(kind=type(payload).__name__):
                self.source.begin_cycle()
                self.get.reset_mock()
                self.get.return_value = response(payload)
                batch = self.source.search("mouse gamer", 5)
                self.assertTrue(batch.stop)
                self.assertTrue(batch.errors)
                self.assertFalse(batch.products)
                self.get.assert_called_once()
                self.assertNotIn("private-response", repr(batch))

    def test_invalid_bulk_json_stops_without_leaking_response(self):
        malformed = response()
        malformed._content = b"fake-private-body: not json"
        self.get.return_value = malformed
        batch = self.source.search("mouse gamer", 5)
        self.assertTrue(batch.stop)
        self.assertIn("JSON", str(batch.errors[0]))
        self.assertNotIn("private", repr(batch))
        self.get.assert_called_once()

    def test_attributes_are_prefixed_and_sale_price_remains_the_only_price_source(self):
        self.get.side_effect = [response(bulk(listing(price=1, original_price=900))), response(sale_price(regular_amount=None))]
        batch = self.source.search("mouse gamer", 5)
        fields = self.get.call_args_list[0].kwargs["params"]["attributes"].split(",")
        self.assertEqual(fields, ["body." + field for field in ITEM_FIELDS])
        self.assertNotIn("body.price", fields)
        self.assertNotIn("body.original_price", fields)
        self.assertNotIn("body.seller_id", fields)
        product = batch.products[0]
        self.assertEqual((product.price, product.price_source), (6000, "sale_price"))
        self.assertIsNone(product.original_price)
        self.assertIsNone(product.discount_percentage)

    def test_sale_price_403_skips_first_and_continues_second_without_fake_price(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response(bulk(listing(price=1), listing("MLB101"))), response(status=403), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        self.assertEqual(batch.errors[0].status, 403)
        self.assertTrue(any(d.item_id == "MLB100" and d.http_status == 403 and "sale_price.amount" in d.reason
                            for d in batch.diagnostics))

    def test_invalid_sale_price_payload_skips_only_that_item(self):
        self.source.item_ids = ("MLB100", "MLB101")
        self.get.side_effect = [response(bulk(listing(price=1), listing("MLB101"))), response([]), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertEqual([p.item_id for p in batch.products], ["MLB101"])
        self.assertTrue(batch.errors)

    def test_fallback_global_errors_do_not_repeat_the_individual_get(self):
        for status in (401, 429, 503):
            with self.subTest(status=status):
                self.source = MercadoLivreSource(item_ids=("MLB100", "MLB101"))
                self.get.reset_mock()
                self.get.side_effect = [response([{"id": "MLB100", "status_code": 200}] + bulk(listing("MLB101"))),
                                        response(status=status)]
                with patch.object(self.source, "_pace"), patch.object(self.source, "_wait") as wait:
                    batch = self.source.search("mouse gamer", 5)
                self.assertTrue(batch.stop)
                self.assertEqual(batch.errors[0].status, status)
                self.assertEqual(self.urls(), [API_ITEMS + "/bulk", API_ITEMS + "/MLB100"])
                self.assertEqual(batch.diagnostics[-1].item_id, "MLB100")
                wait.assert_not_called()

    def test_bulk_transient_server_failure_recovers_after_bounded_retry(self):
        self.get.side_effect = [response(status=503), response(bulk(listing())), response(sale_price())]
        batch = self.source.search("mouse gamer", 5)
        self.assertFalse(batch.stop)
        self.assertFalse(batch.errors)
        self.assertEqual(len(batch.products), 1)
        self.wait.assert_called_once_with(5)

    def test_fallback_can_run_again_in_next_cycle_but_not_again_in_same_cycle(self):
        self.get.side_effect = [response([{"id": "MLB100", "status_code": 200}]), response(status=404),
                                response([{"id": "MLB100", "status_code": 200}]), response(listing()), response(sale_price())]
        self.source.search("mouse gamer", 5)
        self.source.search("mouse gamer", 5)
        self.assertEqual(self.get.call_count, 2)
        self.source.begin_cycle()
        batch = self.source.search("mouse gamer", 5)
        self.assertEqual(len(batch.products), 1)
        self.assertEqual(self.urls().count(API_ITEMS + "/MLB100"), 2)

    def test_incompatible_envelope_never_leaks_credentials_keys_headers_or_body(self):
        secret = "fake-bulk-access-token"
        payload = [{"id": "MLB100", "status_code": {"Authorization": "Bearer " + secret},
                    "code": "Bearer fake-refresh-token", "body": {"seller_address": "private-address"},
                    "Authorization": "Bearer " + secret, secret: "private-email", "error": "private-body"}]
        self.get.side_effect = [response(payload), response({"message": "private-response"}, status=403)]
        with self.assertLogs("services.mercado_livre", level="WARNING") as logs:
            batch = self.source.search("mouse gamer", 5)
        text = " ".join(logs.output) + repr(batch) + format_dry_run_report(SearchReport(diagnostics=batch.diagnostics), 70)
        for private in (secret, "fake-refresh-token", "Authorization", "Bearer", "private-address",
                        "private-email", "private-body", "private-response", "seller_address"):
            self.assertNotIn(private, text)
        self.assertIn("status_code=invalido:dict", text)
        self.assertIn("code=invalido:str", text)
        self.assertIn("<outras>", text)


class BulkDryRunIntegrationTests(IsolatedTest):
    def test_three_provided_ids_with_mocked_partial_bulk_reach_engine_without_telegram(self):
        with test_directory() as directory:
            cfg = Config(database_path=directory / "test.db", auto_publish=True,
                         meli_item_ids=REAL_IDS, min_offer_score=50)
            first = listing(REAL_IDS[0])
            del first["id"]
            payload = [{"id": REAL_IDS[0], "status_code": 200, "body": first},
                       {"id": REAL_IDS[1], "status_code": 403, "body": {}},
                       {"id": REAL_IDS[2], "status_code": 200, "body": None}]
            telegram = Mock(spec=TelegramService)
            output = io.StringIO()
            with patch("services.mercado_livre.requests.Session.get", autospec=True,
                       side_effect=[response(payload), response(listing(REAL_IDS[2])), response(sale_price()),
                                    response(sale_price(amount=65))]) as get, \
                 patch("services.mercado_livre.get_valid_access_token", return_value="fake-bulk-access"), \
                 patch("services.mercado_livre.MercadoLivreSource._pace"), \
                 patch("main.TelegramService", return_value=telegram), redirect_stdout(output):
                result = main.run(cfg, search_now=True, search_limit=5)
            self.assertEqual(result, 1)  # Resultado parcial é reportado honestamente.
            self.assertEqual(get.call_count, 4)
            self.assertEqual(telegram.method_calls, [])
            self.assertNotIn("INTERROMPIDO", output.getvalue())
            self.assertIn("Itens válidos: 2", output.getvalue())
            for identity in REAL_IDS:
                self.assertIn(identity, output.getvalue())
            with Database(cfg.database_path).connect() as con:
                self.assertEqual(con.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
                # O intervalo antispam preservado permite só a primeira simulação.
                self.assertEqual(con.execute("SELECT COUNT(*) FROM simulations").fetchone()[0], 1)
                self.assertEqual(con.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)
                self.assertEqual(con.execute("SELECT value FROM system_state WHERE key='auto_publish'").fetchone()[0], "false")
