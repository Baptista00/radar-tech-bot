"""Fixtures completas com bloqueios de rede, credenciais e publicação."""

import ast
import io
import json
import os
import socket
import threading
from contextlib import redirect_stdout
from dataclasses import replace
from datetime import timedelta
from pathlib import Path
from unittest.mock import Mock, patch

import main
from admin_bot import AdminBot
from config import Config, ConfigError, load_config
from database import Database
from engine import OfferEngine
from models import OfferCandidate, Product
from runtime_lock import instance_lock
from safety import SafeError
from services.fixture_runner import fixture_config, run_fixture_simulation, seed_fixture_history
from services.fixture_source import FIXTURE_PATH, FIXTURE_TERMS, FixtureSource
from services.offer_normalizer import normalize_candidate
from services.offer_formatter import PROMOTION_NOTICE, format_offer
from services.offer_source import AmazonSource, MercadoLivreAdapter, OfferBatch, OfferSource, ShopeeSource
from services.publisher import OfflinePublisher
from telegram_service import TelegramService
from test_mvp import IsolatedTest, NOW, test_directory, update


class FixtureTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.directory = self.enterContext(test_directory())
        self.path = self.directory / "fixture.db"
        self.config = fixture_config(database_path=self.path)
        self.connect = self.enterContext(patch.object(socket.socket, "connect", side_effect=AssertionError("Rede proibida")))
        self.dns = self.enterContext(patch.object(socket, "getaddrinfo", side_effect=AssertionError("DNS proibido")))
        self.telegram = self.enterContext(patch.object(TelegramService, "_call", side_effect=AssertionError("Telegram proibido")))

    def simulate(self, **kwargs):
        return run_fixture_simulation(database_path=self.path, reporter=Mock(), clock=lambda: NOW, **kwargs)

    def test_fixture_file_has_all_scenarios_only_fictitious_ids_and_links(self):
        rows = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))["offers"]
        self.assertGreaterEqual(len(rows), 20)
        self.assertEqual(len(rows), 26)
        self.assertEqual(len({row["id"] for row in rows}), 25)
        for row in rows:
            self.assertTrue(row["id"].startswith("FIXTURE-"))
            self.assertTrue(row["url"].startswith("https://example.com/"))
        self.assertTrue(any(row["price"] is None for row in rows))
        self.assertTrue(any(row.get("original_price") is None for row in rows))
        self.assertTrue(any(row.get("free_shipping") is None for row in rows))
        self.assertTrue(any(row.get("brand") is None for row in rows))

    def test_fixture_source_implements_common_protocol_and_limits_results(self):
        source = FixtureSource()
        self.assertIsInstance(source, OfferSource)
        batch = source.search("mouse gamer", 2)
        self.assertEqual(len(batch.candidates), 2)
        self.assertEqual(len(batch.found_ids), 2)
        self.assertTrue(all(isinstance(row, OfferCandidate) for row in batch.candidates))
        self.assertEqual(source.search("sem resultados", 10).candidates, [])
        for limit in (0, -1, 101, True, None):
            with self.subTest(limit=limit), self.assertRaises(SafeError):
                source.search("mouse gamer", limit)

    def test_cancelled_fixture_cycle_never_returns_candidates(self):
        source, event = FixtureSource(), threading.Event()
        event.set()
        source.begin_cycle(event)
        self.assertTrue(source.search("mouse gamer", 10).stop)
        self.assertEqual(source.search("mouse gamer", 10).candidates, [])

    def test_bad_fixture_schema_has_safe_error_without_echoing_payload(self):
        path = self.directory / "malformed.json"
        for value in ("secret=synthetic-sensitive-content", "null", '{"schema_version": 1, "offers": [null]}'):
            path.write_text(value, encoding="utf-8")
            with self.assertRaises(SafeError) as caught:
                FixtureSource(path)
            self.assertNotIn("synthetic-sensitive", str(caught.exception))

    def test_all_candidates_are_normalized_and_missing_price_is_rejected(self):
        source = FixtureSource()
        results = [normalize_candidate(candidate, term) for term in FIXTURE_TERMS
                   for candidate in source.search(term, 10).candidates]
        self.assertEqual(sum(product is None for product, _ in results), 1)
        rejected = [reason for product, reason in results if product is None]
        self.assertIn("preço ausente ou inválido", rejected)
        products = {product.external_id: product for product, _ in results if product}
        self.assertEqual(products["FIXTURE-IRRELEVANT-001"].relevance_score, 0)
        self.assertEqual(products["FIXTURE-MOUSE-NEGATIVE"].relevance_score, 0)
        self.assertIsNone(products["FIXTURE-NOTEBOOK-NO-REFERENCE"].discount_percentage)
        self.assertIsNone(products["FIXTURE-MOUSE-NO-SHIPPING"].free_shipping)

    def test_normalizer_rejects_untrusted_price_links_and_identity(self):
        candidate = FixtureSource().search("mouse gamer", 1).candidates[0]
        changes = ({"price": True}, {"price": 0}, {"price": "1"}, {"price": None},
                   {"url": "https://user:fake-password@example.com/x"}, {"url": "http://example.com/x"},
                   {"url": "https://real-market.example/x"}, {"external_id": "MLB100"},
                   {"currency_id": "USD"}, {"title": ""})
        for change in changes:
            with self.subTest(fields=list(change)):
                self.assertIsNone(normalize_candidate(replace(candidate, **change), "mouse gamer")[0])

    def test_bad_optional_values_remain_unknown_instead_of_breaking_batch(self):
        candidate = FixtureSource().search("mouse gamer", 1).candidates[0]
        candidate = replace(candidate, original_price=True, free_shipping="true", rating=float("nan"),
                            seller_reputation={}, sold_quantity=True, item_id={})
        product, _ = normalize_candidate(candidate, "mouse gamer")
        for key in ("original_price", "free_shipping", "rating", "seller_reputation", "sold_quantity", "item_id"):
            self.assertIsNone(getattr(product, key))

    def test_full_pipeline_saves_history_scores_and_deduplicates_without_network(self):
        report = self.simulate()
        self.assertEqual(report.status, "completed")
        self.assertEqual(len(report.found_ids), 25)
        self.assertEqual(len(report.valid_ids), 24)
        self.assertEqual(len(report.relevant_ids), 22)
        self.assertEqual(len(report.offers), 22)
        self.assertEqual(report.errors, [])
        self.assertEqual(report.simulated, 1)  # Intervalo global continua valendo.
        db = Database(self.path)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 24)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 30)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM offer_evaluations").fetchone()[0], 24)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM publications").fetchone()[0], 0)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products WHERE source!='fixture'").fetchone()[0], 0)
        self.connect.assert_not_called()
        self.dns.assert_not_called()
        self.telegram.assert_not_called()

    def test_history_seeding_is_idempotent_and_second_cycle_keeps_identity(self):
        self.simulate()
        db, source = Database(self.path), FixtureSource()
        seed_fixture_history(db, source, NOW + timedelta(days=30))
        report = run_fixture_simulation(database_path=self.path, reporter=Mock(), clock=lambda: NOW + timedelta(minutes=1))
        self.assertEqual(report.simulated, 0)
        with db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 24)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 54)
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM simulations").fetchone()[0], 1)

    def test_fixture_ignores_real_source_cooldown(self):
        db = Database(self.path)
        db.set_state("meli_retry_not_before", (NOW + timedelta(days=1)).isoformat())
        self.assertEqual(self.simulate().status, "completed")

    def test_fixture_forces_false_even_when_caller_requests_publication(self):
        base = Config(auto_publish=True, telegram_bot_token="fake-never-use-token", telegram_admin_id=42)
        cfg = fixture_config(base, database_path=self.path)
        self.assertFalse(cfg.auto_publish)
        self.assertEqual(cfg.telegram_bot_token, "")
        self.assertIsNone(cfg.telegram_admin_id)
        report = self.simulate(base=base)
        self.assertGreater(len(report.offers), 0)
        self.telegram.assert_not_called()

    def test_engine_and_database_both_block_fixture_publication(self):
        db = Database(self.path)
        config = replace(self.config, auto_publish=True)
        publisher = Mock(spec=TelegramService)
        engine = OfferEngine(config, db, FixtureSource(), publisher, clock=lambda: NOW, reporter=Mock())
        self.assertFalse(engine.config.auto_publish)
        engine.run_search()
        publisher.publish_offer.assert_not_called()
        product = engine.last_report.offers[0].product
        product_id, _ = db.observe(product, NOW, 30)
        self.assertEqual(db.reserve(product_id, product, 100, NOW, config, simulate=False)[0], None)

    def test_cli_fixture_does_not_load_config_construct_network_services_or_poll(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--dry-run", "--fixture"]), \
             patch("services.fixture_runner.fixture_config", return_value=self.config), \
             patch("main.load_public_channel_url", return_value="") as public_config, \
             patch("main.load_config", side_effect=AssertionError("Config secreta proibida")) as config, \
             patch("main.MercadoLivreSource", side_effect=AssertionError("ML proibido")) as ml, \
             patch("main.TelegramService", side_effect=AssertionError("Telegram proibido")) as telegram, \
             patch("main.poll_admin") as poll, patch("main.configure_logging"), redirect_stdout(output), \
             patch.dict(os.environ, {"AUTO_PUBLISH": "true", "OFFER_SOURCE": "mercado_livre"}):
            self.assertEqual(main.main(), 0)
        config.assert_not_called()
        public_config.assert_called_once_with()
        ml.assert_not_called()
        telegram.assert_not_called()
        poll.assert_not_called()
        self.assertIn("RADAR TECH — DRY RUN", output.getvalue())
        self.assertIn("TOP OFERTAS", output.getvalue())
        self.assertIn("FIXTURE", output.getvalue())
        self.assertIn("PRÉVIAS DAS MENSAGENS", output.getvalue())
        self.assertEqual(output.getvalue().count(PROMOTION_NOTICE), 3)

    def test_fixture_previews_use_formatter_for_complete_missing_reference_and_unknown_shipping(self):
        output = Mock()
        base = replace(self.config, public_channel_url="https://t.me/example")
        report = run_fixture_simulation(base, database_path=self.path, clock=lambda: NOW, reporter=output)
        text = output.call_args.args[0]
        self.assertEqual(text.count(PROMOTION_NOTICE), 3)
        previews = [entry for entry in report.offers if format_offer(
            entry.product, entry.result, public_channel_url=base.public_channel_url) in text]
        self.assertEqual(len(previews), 3)
        self.assertTrue(any(entry.product.original_price and entry.product.free_shipping is True for entry in previews))
        self.assertTrue(any(entry.product.original_price is None and entry.product.free_shipping is None for entry in previews))
        self.assertTrue(any(entry.product.original_price and entry.product.free_shipping is None for entry in previews))
        self.assertEqual(text.count("Envie o Radar Tech pra galera"), 3)
        self.telegram.assert_not_called()
        self.connect.assert_not_called()
        self.dns.assert_not_called()

    def test_cli_fixture_uses_public_channel_url_and_unicode_without_publication(self):
        output = io.StringIO()
        with patch("sys.argv", ["main.py", "--dry-run", "--fixture"]), \
             patch("services.fixture_runner.fixture_config", side_effect=lambda base, **kwargs: fixture_config(base, database_path=self.path)), \
             patch("main.load_public_channel_url", return_value="https://t.me/RadarTechOfertas") as public_config, \
             patch("main.load_config", side_effect=AssertionError("Config secreta proibida")) as config, \
             patch("main.MercadoLivreSource", side_effect=AssertionError("ML proibido")) as ml, \
             patch("main.TelegramService", side_effect=AssertionError("Telegram proibido")) as telegram, \
             patch("main.poll_admin") as poll, patch("main.configure_logging"), redirect_stdout(output), \
             patch.dict(os.environ, {"AUTO_PUBLISH": "true", "OFFER_SOURCE": "mercado_livre"}):
            self.assertEqual(main.main(), 0)
        text = output.getvalue()
        self.assertEqual(text.count("Envie o Radar Tech pra galera 👇\nhttps://t.me/RadarTechOfertas"), 3)
        for codepoints in ((0x1F525,), (0x1F4C9,), (0x1F4E1,), (0x1F447,)):
            self.assertIn("".join(map(chr, codepoints)), text)
        for codepoint in (0x1F4B0, 0x1F3F7, 0x2B50, 0x1F4E2):
            self.assertNotIn(chr(codepoint), text)
        self.assertEqual(text.count(PROMOTION_NOTICE), 3)
        self.assertFalse(Database(self.path).get_state("auto_publish"))
        public_config.assert_called_once_with()
        for forbidden in (config, ml, telegram, poll, self.telegram, self.connect, self.dns):
            forbidden.assert_not_called()

    def test_engine_passes_public_cta_to_formatter_without_publishing(self):
        config = replace(self.config, public_channel_url="https://t.me/example")
        db = Database(self.path)
        publisher = Mock(spec=TelegramService)
        with patch("engine.format_offer", wraps=format_offer) as formatter:
            engine = OfferEngine(config, db, FixtureSource(), publisher, clock=lambda: NOW, reporter=Mock())
            engine.run_search()
        self.assertEqual(engine.last_report.status, "completed")
        self.assertGreater(formatter.call_count, 0)
        for call in formatter.call_args_list:
            self.assertEqual(call.kwargs["public_channel_url"], config.public_channel_url)
        publisher.publish_offer.assert_not_called()
        publisher.reply.assert_not_called()

    def test_second_fixture_cycle_still_previews_messages_when_antispam_blocks_posts(self):
        self.simulate()
        output = Mock()
        report = run_fixture_simulation(database_path=self.path, reporter=output, clock=lambda: NOW)
        self.assertEqual(report.simulated, 0)
        self.assertEqual(output.call_args.args[0].count(PROMOTION_NOTICE), 3)
        self.telegram.assert_not_called()

    def test_cli_rejects_conflicting_options_before_loading_env(self):
        for option in ("--admin-only", "--search-now", "--item-ids=MLB100"):
            with patch("sys.argv", ["main.py", "--fixture", option]), \
                 patch("sys.stderr", io.StringIO()), self.assertRaises(SystemExit) as error:
                main.main()
            self.assertEqual(error.exception.code, 2)

    def test_runtime_lock_blocks_second_fixture_instance(self):
        with instance_lock(self.path.with_suffix(".lock")):
            with self.assertRaises(SafeError):
                self.simulate()
        self.assertEqual(self.simulate().status, "completed")

    def test_sensitive_extra_json_fields_never_reach_report_or_logs(self):
        payload = json.loads(FIXTURE_PATH.read_text(encoding="utf-8"))
        for row in payload["offers"]:
            row["access_token"] = "synthetic-private-value-never-print"
            row["cookie"] = "synthetic-private-value-never-print"
        path = self.directory / "safe-test.json"
        path.write_text(json.dumps(payload), encoding="utf-8")
        output = io.StringIO()
        with patch("services.fixture_runner.FixtureSource", return_value=FixtureSource(path)), \
             self.assertLogs(level="INFO") as logs:
            run_fixture_simulation(database_path=self.path, clock=lambda: NOW, reporter=lambda text: output.write(text))
        self.assertNotIn("synthetic-private-value", output.getvalue() + "\n".join(logs.output))

    def test_fixture_data_does_not_create_or_touch_production_database(self):
        production = self.directory / "radartech.db"
        production.write_bytes(b"untouched-production-marker")
        run_fixture_simulation(Config(database_path=production), reporter=Mock(), clock=lambda: NOW)
        self.assertEqual(production.read_bytes(), b"untouched-production-marker")
        self.assertTrue((self.directory / "radartech-fixture.db").exists())


class SourceContractTests(IsolatedTest):
    def test_engine_has_no_marketplace_module_dependency(self):
        path = Path(__file__).resolve().parents[1] / "engine.py"
        imports = [node.module for node in ast.walk(ast.parse(path.read_text(encoding="utf-8")))
                   if isinstance(node, ast.ImportFrom)]
        self.assertFalse(any("mercado_livre" in (module or "") for module in imports))
        self.assertIn("services.offer_source", imports)

    def test_pending_sources_are_explicit_offline_placeholders(self):
        for source in (ShopeeSource(), AmazonSource()):
            self.assertIsInstance(source, OfferSource)
            source.begin_cycle()
            with self.assertRaises(SafeError):
                source.search("mouse gamer", 5)

    def test_legacy_adapter_only_delegates_existing_transport(self):
        from services.mercado_livre import SearchBatch
        legacy = Mock(search=Mock(return_value=SearchBatch()))
        source = MercadoLivreAdapter(legacy)
        self.assertIsInstance(source, OfferSource)
        event = threading.Event()
        source.begin_cycle(event)
        self.assertIsInstance(source.search("mouse", 5), OfferBatch)
        legacy.begin_cycle.assert_called_once_with(event)
        legacy.search.assert_called_once_with("mouse", 5)

    def test_new_sources_use_same_engine_and_same_id_does_not_merge_sources(self):
        class LocalSource:
            simulation_only = True

            def __init__(self, identity):
                self.source_id = identity

            def begin_cycle(self, stop_event=None):
                pass

            def search(self, term, limit):
                row = OfferCandidate("SAME-ID", "Mouse Gamer", 10000, "https://example.com/mouse",
                                     source=self.source_id, currency_id="BRL", original_price=20000)
                return OfferBatch([row, row], {row.external_id})

        with test_directory() as directory:
            config = Config(database_path=directory / "source.db", search_terms=("mouse gamer",), request_interval_seconds=0)
            db = Database(config.database_path)
            for identity in ("store_a", "store_b"):
                source = LocalSource(identity)
                self.assertIsInstance(source, OfferSource)
                engine = OfferEngine(config, db, source, OfflinePublisher(), clock=lambda: NOW, reporter=Mock())
                engine.run_search()
                self.assertEqual(engine.last_report.status, "completed")
            with db.connect() as connection:
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM products").fetchone()[0], 2)
                self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)

    def test_unknown_source_fails_closed(self):
        with self.assertRaises(ConfigError), patch("main.MercadoLivreSource") as ml:
            main.make_source(Config(offer_source="unknown"))
        ml.assert_not_called()


class FixtureAdminTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.directory = self.enterContext(test_directory())
        self.config = Config(database_path=self.directory / "admin.db", telegram_admin_id=42,
                             telegram_bot_token="fake-admin-secret", search_terms=("mouse gamer",), request_interval_seconds=0)
        self.db = Database(self.config.database_path)
        self.telegram = Mock(spec=TelegramService)
        self.engine = OfferEngine(self.config, self.db, FixtureSource(), self.telegram, clock=lambda: NOW, reporter=Mock())
        self.admin = AdminBot(self.engine.config, self.db, self.engine, self.telegram)

    def test_config_uses_allowlist_and_shows_source_without_credentials(self):
        self.admin.handle_update(update("/config"))
        text = self.telegram.reply.call_args.args[1]
        for expected in ("AUTO_PUBLISH=false", "MIN_OFFER_SCORE=70", "REPOST_AFTER_HOURS=72", "Fonte atual: Fixture", "Categorias ativas:"):
            self.assertIn(expected, text)
        for forbidden in ("fake-admin-secret", "TELEGRAM_ADMIN_ID", "TELEGRAM_BOT_TOKEN", "MELI_CLIENT_SECRET"):
            self.assertNotIn(forbidden, text)

    def test_status_shows_safe_mode_source_approvals_and_schedule(self):
        self.engine.run_search()
        text = self.admin.status()
        for expected in ("Auto publish: OFF", "Fonte: Fixture", "Ofertas aprovadas", "Próxima busca:", "Erros recentes:"):
            self.assertIn(expected, text)

    def test_fixture_runs_isolated_simulation_and_replies_only_to_admin(self):
        self.admin.handle_update(update("/fixture"))
        self.telegram.reply.assert_called_once()
        chat_id, text = self.telegram.reply.call_args.args
        self.assertEqual(chat_id, 42)
        self.assertIn("FIXTURE", text)
        self.assertIn("Resultado: completed", text)
        self.assertIn("nenhuma oferta enviada", text)
        self.telegram.publish_offer.assert_not_called()
        self.assertEqual(self.db.stats(NOW, "UTC")["products"], 0)
        self.assertTrue((self.directory / "radartech-admin-fixture.db").exists())

    def test_new_commands_reject_non_admin_and_group_before_running(self):
        with patch("services.fixture_runner.run_fixture_simulation") as simulate:
            for command in ("/config", "/fixture"):
                for sender, kind in ((7, "private"), (42, "group")):
                    self.admin.handle_update(update(command, sender, kind))
                    self.assertEqual(self.telegram.reply.call_args.args[1], "Não autorizado.")
            simulate.assert_not_called()

    def test_fixture_already_running_returns_safe_reply(self):
        with instance_lock(self.directory / "radartech-admin-fixture.lock"):
            self.assertIn("já em execução", self.admin.fixture())
        self.telegram.publish_offer.assert_not_called()

    def test_new_configuration_names_and_legacy_aliases(self):
        base = {"TELEGRAM_BOT_TOKEN": "fake-test-token"}
        config = load_config({**base, "REPOST_AFTER_HOURS": "96", "REPOST_PRICE_DROP_PERCENT": "15"})
        self.assertEqual((config.republish_after_hours, config.significant_price_drop_percent), (96, 15))
        old = load_config({**base, "REPUBLISH_AFTER_HOURS": "48", "SIGNIFICANT_PRICE_DROP_PERCENT": "12"})
        self.assertEqual((old.republish_after_hours, old.significant_price_drop_percent), (48, 12))
        priority = load_config({**base, "REPUBLISH_AFTER_HOURS": "48", "REPOST_AFTER_HOURS": "96"})
        self.assertEqual(priority.republish_after_hours, 96)
        self.assertEqual(Config().search_interval_minutes, 30)
        self.assertFalse(load_config({**base, "OFFER_SOURCE": "fixture", "AUTO_PUBLISH": "true"}).auto_publish)

    def test_category_configuration_rejects_unknown_names_without_echo(self):
        with self.assertRaises(ConfigError) as caught:
            load_config({"TELEGRAM_BOT_TOKEN": "fake-test-token", "ACTIVE_CATEGORIES": "fake-secret-category"})
        self.assertNotIn("fake-secret-category", str(caught.exception))
