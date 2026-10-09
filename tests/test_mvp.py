"""Testes locais: rede bloqueada e .env nunca carregado."""

import logging
import shutil
import threading
import unittest
import uuid
from concurrent.futures import ThreadPoolExecutor
from contextlib import contextmanager
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path
from unittest.mock import Mock, patch

import requests

from admin_bot import AdminBot
from config import Config, ConfigError, load_config
from database import Database
from engine import OfferEngine
from main import poll_admin, run
from models import Product
from runtime_lock import instance_lock
from safety import SafeError, SecretFilter
from services.mercado_livre import CatalogSource as MercadoLivreSource, SearchBatch, normalize_item
from services.offer_filter import score_offer
from services.offer_formatter import format_offer
from telegram_service import TelegramService

NOW = datetime(2026, 10, 7, 15, tzinfo=timezone.utc)


@contextmanager
def test_directory():
    # mkdir padrão evita o ACL privado de tempfile no sandbox Windows.
    root = (Path(__file__).resolve().parents[1] / "data").resolve()
    root.mkdir(exist_ok=True)
    directory = root / (".test-" + uuid.uuid4().hex)
    directory.mkdir()
    try:
        yield directory
    finally:
        resolved = directory.resolve()
        if resolved.parent != root or not resolved.name.startswith(".test-"):
            raise RuntimeError("Pasta de teste fora do local esperado")
        shutil.rmtree(resolved)


def product(**changes):
    values = dict(external_id="MLB100", title="Mouse de teste", price=10000,
                  original_price=20000, url="https://produto.mercadolivre.com.br/MLB-100-teste",
                  category="mouse", search_term="mouse", brand="Logitech", sold_quantity=1000,
                  free_shipping=True, seller_reputation="GREEN")
    values.update(changes)
    return Product(**values)


def update(command, user_id=42, chat_type="private", **sender_extra):
    return {"update_id": 1, "message": {"text": command,
            "from": {"id": user_id, **sender_extra}, "chat": {"id": user_id, "type": chat_type}}}


class IsolatedTest(unittest.TestCase):
    def setUp(self):
        self.network = patch("requests.sessions.Session.request", side_effect=AssertionError("Rede real proibida nos testes"))
        self.dotenv = patch("config.load_dotenv", side_effect=AssertionError(".env proibido nos testes"))
        self.network.start()
        self.dotenv.start()
        self.oauth_dotenv = patch("services.mercado_livre_auth.load_dotenv", side_effect=AssertionError(".env proibido nos testes"))
        self.oauth_dotenv.start()
        self.addCleanup(self.oauth_dotenv.stop)
        self.addCleanup(self.network.stop)
        self.addCleanup(self.dotenv.stop)


class ConfigAndSafetyTests(IsolatedTest):
    def test_defaults_and_forced_simulation(self):
        cfg = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder"})
        self.assertFalse(cfg.auto_publish)
        self.assertIsNone(cfg.telegram_admin_id)
        self.assertEqual(cfg.min_offer_score, 70)
        self.assertNotIn("test-only-placeholder", repr(cfg))
        forced = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", "AUTO_PUBLISH": "true"}, force_dry_run=True)
        self.assertFalse(forced.auto_publish)

    def test_invalid_options_fail_without_echoing_input(self):
        for options in ({"AUTO_PUBLISH": "typo"}, {"TELEGRAM_ADMIN_ID": "private-value"},
                        {"MIN_OFFER_SCORE": "101"}, {"MAX_POSTS_PER_DAY": "0"}, {"AUTO_PUBLISH": "true"}):
            with self.subTest(options=list(options)), self.assertRaises(ConfigError) as caught:
                load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", **options})
            self.assertNotIn("private-value", str(caught.exception))

    def test_logging_removes_secrets_and_tracebacks(self):
        record = logging.LogRecord("test", logging.ERROR, "", 0,
                                   "oops %s https://api.telegram.org/botFAKE/sendMessage", ("example-private-value",), None)
        record.exc_text = "example-private-value"
        SecretFilter(("example-private-value",)).filter(record)
        self.assertNotIn("example-private-value", record.getMessage())
        self.assertNotIn("api.telegram.org", record.getMessage())
        self.assertIsNone(record.exc_text)

    def test_single_instance_lock(self):
        with test_directory() as temporary:
            path = Path(temporary) / "radar.lock"
            with instance_lock(path):
                with self.assertRaises(SafeError):
                    with instance_lock(path):
                        self.fail("Dois processos não podem adquirir o lock")
            with instance_lock(path):
                self.assertTrue(path.exists())


class ProviderAndScoreTests(IsolatedTest):
    def item(self, **changes):
        raw = {"id": "MLB123", "title": "Mouse gamer", "price": 179.90, "currency_id": "BRL",
               "permalink": "https://produto.mercadolivre.com.br/MLB-123-mouse?utm_source=test#tracking"}
        raw.update(changes)
        return raw

    def test_normalize_missing_data_and_normal_link(self):
        p = normalize_item(self.item(), "mouse")
        self.assertEqual(p.price, 17990)
        self.assertIsNone(p.original_price)
        self.assertIsNone(p.rating)
        self.assertIsNone(p.sold_quantity)
        self.assertIsNone(p.discount_percentage)
        self.assertEqual(p.url, "https://produto.mercadolivre.com.br/MLB-123-mouse")
        self.assertIsNone(normalize_item(self.item(title="Sofá para sala"), "mouse"))

    def test_invalid_or_untrusted_products_rejected(self):
        for changes in ({"price": "NaN"}, {"price": 0}, {"price": True}, {"currency_id": "USD"},
                        {"permalink": "https://meli.la/affiliate"}, {"permalink": "https://evil.example/MLB123"},
                        {"condition": "used"}, {"available_quantity": 0}):
            self.assertIsNone(normalize_item(self.item(**changes), "mouse"))

    def test_no_fake_original_price_rating_or_sales(self):
        p = normalize_item(self.item(original_price=100, base_price=300, sold_quantity="50+",
                                     seller={"reputation": 5}, reviews={"rating_average": 9}), "mouse")
        self.assertIsNone(p.original_price)
        self.assertIsNone(p.rating)
        self.assertIsNone(p.sold_quantity)

    def test_public_api_requires_explicit_access(self):
        with patch("services.mercado_livre.get_valid_access_token", side_effect=SafeError("OAuth: autorização necessária.")):
            batch = MercadoLivreSource().search("mouse", 10)
        self.assertTrue(batch.stop)
        self.assertEqual(len(batch.errors), 1)
        self.assertEqual(batch.products, [])

    def test_provider_http_errors_are_safe_and_no_fallback(self):
        response = Mock(status_code=403)
        response.json.return_value = {"message": "do-not-echo-private-value"}
        with patch("services.mercado_livre.requests.Session.get", return_value=response) as request, \
             patch("services.mercado_livre.get_valid_access_token", return_value="test-only-placeholder"):
            batch = MercadoLivreSource().search("mouse", 10)
        self.assertEqual(batch.errors[0].status, 403)
        self.assertNotIn("do-not-echo", str(batch.errors[0]))
        response.json.assert_not_called()
        self.assertEqual(request.call_count, 1)
        self.assertFalse(request.call_args.kwargs["allow_redirects"])

    def test_provider_success_ignores_bad_items(self):
        response = Mock(status_code=200)
        response.json.return_value = {"results": [None, {"id": "MLA123"}, {"id": "untrusted/id"}]}
        with patch("services.mercado_livre.requests.Session.get", return_value=response) as request, \
             patch("services.mercado_livre.get_valid_access_token", return_value="test-only-placeholder"):
            batch = MercadoLivreSource().search("mouse", 10)
        self.assertEqual(batch.products, [])
        request.assert_called_once()

    def test_score_missing_fields_are_not_fabricated(self):
        p = product(original_price=None, brand=None, sold_quantity=None, free_shipping=None, seller_reputation=None)
        result = score_offer(p, [])
        self.assertEqual(result.score, 0)
        self.assertFalse(result.approved(0))
        brand_only = score_offer(replace(p, brand="Logitech"), [])
        self.assertFalse(brand_only.approved(70))
        self.assertNotIn("rating", brand_only.criteria)
        self.assertNotIn("discount", brand_only.criteria)

    def test_history_and_discount_control_approval(self):
        p = product(original_price=None)
        self.assertNotIn("history", score_offer(p, [15000, 14000]).criteria)
        self.assertTrue(score_offer(p, [15000, 14000, 15000]).approved(60))
        self.assertFalse(score_offer(p, [9000]).approved(70))
        self.assertTrue(score_offer(product(), []).approved(70))
        self.assertFalse(score_offer(product(original_price=10100), []).approved(70))

    def test_formatter_omits_unavailable_fields(self):
        p = product(original_price=None, rating=None)
        text = format_offer(p, score_offer(p, [15000, 15000, 15000]))
        self.assertNotIn("OFF", text)
        self.assertNotIn("Avaliação", text)
        self.assertIn("R$ 100,00", text)
        self.assertIn(p.url, text)
        self.assertIn("histórico local", text)


class DatabaseBase(IsolatedTest):
    def setUp(self):
        super().setUp()
        temporary = self.enterContext(test_directory())
        self.db = Database(temporary / "test.db")
        self.config = Config(telegram_admin_id=42, auto_publish=True)

    def observe(self, p=None, when=NOW):
        return self.db.observe(p or product(), when, 30)[0]

    def send(self, product_id, p, when=NOW, message_id=1):
        reservation, reason = self.db.reserve(product_id, p, 90, when, self.config, simulate=False)
        self.assertIsNotNone(reservation, reason)
        self.db.confirm_publication(reservation, message_id, when)
        return reservation


class DatabaseTests(DatabaseBase):
    def test_product_upsert_keeps_history_and_clears_missing_metadata(self):
        first = product(rating=4.8)
        identity, history = self.db.observe(first, NOW, 30)
        self.assertEqual(history, [])
        identity2, history = self.db.observe(replace(first, price=9000, original_price=None, rating=None), NOW + timedelta(hours=1), 30)
        self.assertEqual(identity, identity2)
        self.assertEqual(history, [10000])
        with self.db.connect() as connection:
            row = connection.execute("SELECT * FROM products").fetchone()
            self.assertIsNone(row["rating"])
            self.assertIsNone(row["original_price"])
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 2)

    def test_interval_cooldown_and_significant_drop(self):
        p = product()
        identity = self.observe(p)
        self.send(identity, p)
        for hours, price, allowed in ((0.1, 8000, False), (1, 9500, False), (1, 9000, True)):
            result, _ = self.db.reserve(identity, replace(p, price=price), 90, NOW + timedelta(hours=hours), self.config, simulate=False)
            self.assertEqual(result is not None, allowed)
        self.assertEqual(len(self.db.recent_publications()), 1)

    def test_cooldown_expires_at_72_hours(self):
        p = product()
        identity = self.observe(p)
        self.send(identity, p)
        result, _ = self.db.reserve(identity, p, 90, NOW + timedelta(hours=72), self.config, simulate=False)
        self.assertIsNotNone(result)

    def test_daily_limit_uses_sao_paulo_day(self):
        self.config = replace(self.config, max_posts_per_day=1)
        p = product()
        identity = self.observe(p)
        late = datetime(2026, 10, 8, 2, 0, tzinfo=timezone.utc)  # 23h em SP.
        self.send(identity, p, late)
        other = product(external_id="MLB200")
        other_id = self.observe(other)
        denied, _ = self.db.reserve(other_id, other, 90, late + timedelta(minutes=40), self.config, simulate=False)
        self.assertIsNone(denied)
        allowed, _ = self.db.reserve(other_id, other, 90, late + timedelta(hours=1), self.config, simulate=False)
        self.assertIsNotNone(allowed)

    def test_dry_run_does_not_consume_real_limits(self):
        p = product()
        identity = self.observe(p)
        self.db.reserve(identity, p, 90, NOW, self.config, simulate=True)
        again, _ = self.db.reserve(identity, p, 90, NOW, self.config, simulate=True)
        self.assertIsNone(again)
        self.send(identity, p)
        stats = self.db.stats(NOW, self.config.post_timezone)
        self.assertEqual(stats["simulations_today"], 1)
        self.assertEqual(stats["publications_today"], 1)

    def test_pause_and_uncertain_send_survive_restart(self):
        p = product()
        identity = self.observe(p)
        reservation, _ = self.db.reserve(identity, p, 90, NOW, self.config, simulate=False)
        self.db.fail_publication(reservation, SafeError("Timeout seguro", uncertain=True), NOW)
        restarted = Database(self.db.path)
        self.assertTrue(restarted.get_state("paused"))
        restarted.set_state("paused", False)
        result, _ = restarted.reserve(identity, replace(p, price=1), 90, NOW + timedelta(days=10), self.config, simulate=False)
        self.assertIsNone(result)
        self.assertEqual(restarted.recent_publications(), [])

    def test_pending_crash_is_not_retried(self):
        identity = self.observe()
        self.db.reserve(identity, product(), 90, NOW, self.config, simulate=False)
        self.db.recover_interrupted()
        self.assertTrue(self.db.get_state("paused"))
        self.assertEqual(self.db.stats(NOW, self.config.post_timezone)["uncertain"], 1)

    def test_reservation_is_atomic(self):
        p = product()
        identity = self.observe(p)
        def reserve(_):
            return self.db.reserve(identity, p, 90, NOW, self.config, simulate=False)[0]
        with ThreadPoolExecutor(max_workers=2) as pool:
            reservations = list(pool.map(reserve, range(2)))
        self.assertEqual(sum(result is not None for result in reservations), 1)


class EngineAndAdminTests(DatabaseBase):
    def make_engine(self, *, auto_publish=False, source=None):
        cfg = replace(self.config, auto_publish=auto_publish, search_terms=("mouse",), request_interval_seconds=0)
        source = source or Mock(search=Mock(return_value=SearchBatch(products=[product()], found_ids={"MLB100"})))
        telegram = Mock(spec=TelegramService)
        telegram.publish_offer.return_value = 500
        engine = OfferEngine(cfg, self.db, source, telegram, clock=lambda: NOW)
        return engine, telegram

    def test_simulation_pipeline_saves_all_without_posting(self):
        source = Mock(search=Mock(return_value=SearchBatch(
            products=[product(), product(), product(external_id="MLB200", original_price=None)], found_ids={"MLB100", "MLB200"})))
        engine, telegram = self.make_engine(source=source)
        engine.run_search()
        telegram.publish_offer.assert_not_called()
        telegram.reply.assert_not_called()
        stats = self.db.stats(NOW, self.config.post_timezone)
        self.assertEqual(stats["products"], 2)
        self.assertEqual(stats["analyzed"], 2)
        self.assertEqual(stats["approved"], 1)
        self.assertEqual(stats["publications"], 0)
        self.assertEqual(stats["simulations_today"], 1)

    def test_confirmed_publication_persists_message_id(self):
        engine, telegram = self.make_engine(auto_publish=True)
        engine.run_search()
        telegram.publish_offer.assert_called_once()
        self.assertEqual(self.db.recent_publications()[0]["telegram_message_id"], 500)
        engine.run_search()
        self.assertEqual(telegram.publish_offer.call_count, 1)

    def test_timeout_never_retries_and_pauses(self):
        engine, telegram = self.make_engine(auto_publish=True)
        telegram.publish_offer.side_effect = SafeError("Timeout seguro", uncertain=True)
        engine.run_search()
        engine.run_search()
        self.assertEqual(telegram.publish_offer.call_count, 1)
        self.assertTrue(self.db.get_state("paused"))
        self.assertEqual(self.db.recent_publications(), [])

    def test_provider_missing_access_keeps_admin_available(self):
        source = Mock(search=Mock(side_effect=SafeError("Mercado Livre: acesso pendente.")))
        engine, telegram = self.make_engine(source=source)
        engine.run_search()
        admin = AdminBot(engine.config, self.db, engine, telegram)
        admin.handle_update(update("/status"))
        self.assertIn("acesso pendente", telegram.reply.call_args.args[1])
        self.assertEqual(self.db.stats(NOW, self.config.post_timezone)["last_run"]["status"], "failed")

    def test_private_id_auth_whoami_and_username_spoof(self):
        engine, telegram = self.make_engine()
        admin = AdminBot(engine.config, self.db, engine, telegram)
        for command in ("/status", "/ultimas", "/stats", "/pause", "/resume", "/buscar"):
            admin.handle_update(update(command, 99, username="admin"))
            self.assertEqual(telegram.reply.call_args.args, (99, "Não autorizado."))
        self.assertFalse(self.db.get_state("paused", False))
        self.assertFalse(engine._requested.is_set())
        admin.handle_update(update("/whoami", 99))
        self.assertEqual(telegram.reply.call_args.args, (99, "99"))
        admin.handle_update(update("/status", 42, "group"))
        self.assertEqual(telegram.reply.call_args.args[1], "Não autorizado.")

    def test_no_admin_configured_denies_all_but_whoami(self):
        engine, telegram = self.make_engine()
        admin = AdminBot(replace(engine.config, telegram_admin_id=None), self.db, engine, telegram)
        admin.handle_update(update("/status"))
        self.assertEqual(telegram.reply.call_args.args[1], "Não autorizado.")
        admin.handle_update(update("/whoami"))
        self.assertEqual(telegram.reply.call_args.args[1], "42")

    def test_admin_only_never_starts_search_or_channel_publication(self):
        config = replace(self.config, database_path=self.db.path, auto_publish=True)
        with patch("main.OfferEngine.start") as start, \
             patch("main.TelegramService.publish_offer") as publish, \
             patch("main.poll_admin", return_value=0) as poll:
            self.assertEqual(run(config, admin_only=True), 0)
        start.assert_not_called()
        publish.assert_not_called()
        admin = poll.call_args.args[0]
        self.assertFalse(admin.config.auto_publish)
        self.assertIn("desativada", admin.search())
        self.assertFalse(self.db.get_state("auto_publish"))

    def test_authorized_commands_and_resume_stays_in_simulation(self):
        engine, telegram = self.make_engine()
        admin = AdminBot(engine.config, self.db, engine, telegram)
        admin.handle_update(update("/pause"))
        self.assertTrue(self.db.get_state("paused"))
        engine.run_search()
        self.assertEqual(self.db.stats(NOW, self.config.post_timezone)["simulations_today"], 0)
        admin.handle_update(update("/resume"))
        self.assertFalse(self.db.get_state("paused"))
        self.assertIn("simulação", telegram.reply.call_args.args[1])
        admin.handle_update(update("/buscar"))
        self.assertTrue(engine._requested.is_set())
        for command in ("/status", "/stats", "/ultimas"):
            admin.handle_update(update(command))
            self.assertNotEqual(telegram.reply.call_args.args[1], "Não autorizado.")
        telegram.publish_offer.assert_not_called()

    def test_scheduler_wakes_for_manual_search_and_stops(self):
        engine, telegram = self.make_engine()
        completed = threading.Event()
        original_search = engine.run_search

        def signal_search():
            result = original_search()
            completed.set()
            return result

        with patch.object(engine, "run_search", side_effect=signal_search):
            engine.start()
            try:
                self.assertTrue(completed.wait(5))
                completed.clear()
                self.assertTrue(engine.request_search())
                self.assertTrue(completed.wait(5))
            finally:
                engine.stop()
        self.assertFalse(engine.online)
        self.assertEqual(self.db.stats(NOW, self.config.post_timezone)["analyzed"], 2)
        telegram.publish_offer.assert_not_called()

    def test_polling_persists_offset_and_never_replays_command(self):
        admin = Mock()
        telegram = Mock(spec=TelegramService)
        event = update("/pause")
        telegram.get_updates.side_effect = [[event, event], KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            poll_admin(admin, telegram, self.db)
        admin.handle_update.assert_called_once_with(event)
        self.assertEqual(self.db.get_state("telegram_update_offset"), 2)
        admin.reset_mock()
        telegram.get_updates.side_effect = [[event], KeyboardInterrupt]
        with self.assertRaises(KeyboardInterrupt):
            poll_admin(admin, telegram, self.db)
        admin.handle_update.assert_not_called()


class TelegramTests(IsolatedTest):
    def test_simulation_has_transport_guard(self):
        with self.assertRaises(SafeError):
            TelegramService("test-only-placeholder", "@example").publish_offer("example")

    def test_timeout_error_has_no_url_or_token(self):
        with patch("telegram_service.requests.post", side_effect=requests.Timeout("private-internal-url")):
            with self.assertRaises(SafeError) as caught:
                TelegramService("test-only-placeholder", "@example", auto_publish=True).publish_offer("example")
        self.assertTrue(caught.exception.uncertain)
        self.assertNotIn("private-internal-url", str(caught.exception))
        self.assertNotIn("test-only-placeholder", str(caught.exception))

    def test_success_requires_message_id(self):
        response = Mock(status_code=200)
        response.json.return_value = {"ok": True, "result": {"message_id": 10}}
        with patch("telegram_service.requests.post", return_value=response):
            service = TelegramService("test-only-placeholder", "@example", auto_publish=True)
            self.assertEqual(service.publish_offer("example"), 10)
            response.json.return_value = {"ok": True, "result": {}}
            with self.assertRaises(SafeError) as caught:
                service.publish_offer("example")
        self.assertTrue(caught.exception.uncertain)

    def test_api_error_uses_safe_description_and_retry_after(self):
        response = Mock(status_code=429)
        response.json.return_value = {"ok": False, "error_code": 429,
                                      "description": "do-not-echo", "parameters": {"retry_after": 120}}
        with patch("telegram_service.requests.post", return_value=response):
            with self.assertRaises(SafeError) as caught:
                TelegramService("test-only-placeholder", "@example", auto_publish=True).publish_offer("example")
        self.assertFalse(caught.exception.uncertain)
        self.assertEqual(caught.exception.retry_after, 120)
        self.assertNotIn("do-not-echo", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
