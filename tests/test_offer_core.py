"""Calibração, histórico, antispam, mensagens e agendador com dados locais."""

from dataclasses import replace
from datetime import timedelta
from unittest.mock import Mock, patch

from config import Config
from database import Database
from engine import OfferEngine
from models import Product, ScoreResult, score_band, timestamp
from services.categories import CATEGORY_ICONS, canonical_category, category_for, category_icon
from services.fixture_runner import fixture_config
from services.fixture_source import FixtureSource
from services.offer_filter import score_offer
from services.offer_formatter import format_offer
from services.offer_normalizer import normalize_candidate
from telegram_service import TelegramService
from test_mvp import IsolatedTest, NOW, test_directory


def fixture_product():
    candidate = FixtureSource().search("mouse gamer", 1).candidates[0]
    return normalize_candidate(candidate, "mouse gamer")[0]


class ScoreAndPresentationTests(IsolatedTest):
    def test_score_band_boundaries(self):
        expected = {0: "Fraco", 39: "Fraco", 40: "Regular", 59: "Regular",
                    60: "Interessante", 69: "Interessante", 70: "Boa oferta", 84: "Boa oferta",
                    85: "Oferta excelente", 100: "Oferta excelente"}
        for score, label in expected.items():
            with self.subTest(score=score):
                self.assertEqual(score_band(score), label)
                self.assertEqual(ScoreResult(score, {}, False).band, label)

    def test_strong_deal_scores_significantly_above_common_product(self):
        good = fixture_product()
        common = replace(good, original_price=None, free_shipping=None, sold_quantity=None, seller_reputation=None)
        strong_score, common_score = score_offer(good, []), score_offer(common, [])
        self.assertGreaterEqual(strong_score.score, 70)
        self.assertGreaterEqual(strong_score.score - common_score.score, 40)
        self.assertFalse(common_score.approved(0))

    def test_known_brand_alone_is_only_five_points_and_never_approval(self):
        product = Product("FIXTURE-BRAND", "Mouse", 19900, "https://example.com/brand", source="fixture", brand="Logitech")
        result = score_offer(product, [])
        self.assertEqual(result.score, 5)
        self.assertEqual(result.band, "Fraco")
        self.assertFalse(result.approved(0))

    def test_history_adds_evidence_and_can_reach_excellent_band(self):
        product = fixture_product()
        result = score_offer(product, [29900, 27900, 24900])
        self.assertEqual(result.criteria["history"], 25)
        self.assertEqual(result.band, "Oferta excelente")
        self.assertGreater(result.score, score_offer(product, []).score)

    def test_generic_suspicious_ssd_stays_below_good_deals(self):
        candidates = FixtureSource().search("ssd nvme", 10).candidates
        good = normalize_candidate(candidates[0], "ssd nvme")[0]
        suspect = normalize_candidate(candidates[1], "ssd nvme")[0]
        result = score_offer(suspect, [29900, 27900, 24900])
        self.assertLess(result.score, 40)
        self.assertEqual(result.criteria["price_inconsistency"], -25)
        self.assertFalse(result.approved(70))
        self.assertGreater(score_offer(good, []).score, result.score)

    def test_missing_reference_never_becomes_discount_or_savings(self):
        for original in (None, 19900, 19000):
            # Sem vendas/avaliação para isolar a ausência de referência de preço.
            product = replace(fixture_product(), original_price=original, sold_quantity=None, rating=None)
            result = score_offer(product, [])
            self.assertNotIn("discount", result.criteria)
            self.assertFalse(result.approved(0))
            self.assertNotIn("% OFF", format_offer(product, result))

    def test_irrelevant_product_is_never_approved_even_with_discount(self):
        product = replace(fixture_product(), relevance_score=0)
        self.assertFalse(score_offer(product, [29900] * 3).approved(0))

    def test_formatter_complete_data_short_html_and_category_icon(self):
        product = replace(fixture_product(), title="Logitech G305 Wireless", price=17990, original_price=24990,
                          rating=4.7, source="store_a")
        text = format_offer(product, ScoreResult(84, {}, True))
        for expected in ("<b>🖱️ Logitech G305 Wireless</b>", "<s>R$ 249,90</s>", "<b>Por: R$ 179,90</b>", "28,01% OFF",
                         "📡 Radar Score: 84/100 — Boa oferta", "Frete grátis", "⭐ 4.7", "🔥 1.000 vendas", "Compre aqui 👉"):
            self.assertIn(expected, text)
        self.assertLess(len(text), 700)
        self.assertNotIn("ótimo custo-benefício", text)

    def test_formatter_omits_unknown_fields_and_marks_simulation(self):
        product = replace(fixture_product(), original_price=None, free_shipping=None, rating=None, sold_quantity=None)
        text = format_offer(product, ScoreResult(15, {}, False))
        self.assertIn("SIMULAÇÃO — dados fictícios", text)
        for absent in ("OFF", "Frete grátis", "Avaliação:", "Vendas:", "últimos"):
            self.assertNotIn(absent, text)

    def test_paid_shipping_is_not_reported_as_free(self):
        text = format_offer(replace(fixture_product(), free_shipping=False), ScoreResult(70, {}, True))
        self.assertNotIn("Frete grátis", text)

    def test_formatter_escapes_html_uses_html_parse_mode_and_limits_title(self):
        product = replace(fixture_product(), title="<b>[produto]</b>\n" * 100)
        text = format_offer(product, ScoreResult(80, {}, True))
        self.assertLess(len(text), 1000)
        telegram = TelegramService("fake-test-token", "fake-channel", auto_publish=True)
        response = Mock(status_code=200, json=Mock(return_value={"ok": True, "result": {"message_id": 1}}))
        with patch("telegram_service.requests.post", return_value=response) as post:
            telegram.publish_offer(text)
        self.assertEqual(post.call_args.kwargs["json"]["parse_mode"], "HTML")
        self.assertIn("&lt;b&gt;[produto]&lt;/b&gt;", post.call_args.kwargs["json"]["text"])
        self.assertNotIn("<b>[produto]</b>", post.call_args.kwargs["json"]["text"])

    def test_formatter_history_statement_requires_qualified_evidence(self):
        product = fixture_product()
        insufficient = format_offer(product, ScoreResult(80, {"history": 25}, True))
        self.assertIn("histórico local", insufficient)
        self.assertNotIn("últimos", insufficient)
        covered = format_offer(product, ScoreResult(80, {"history": 25}, True, history_claim_days=30))
        self.assertIn("Menor preço observado pelo Radar nos últimos 30 dias", covered)
        self.assertNotIn("menor preço do mercado", covered.lower())

    def test_all_categories_and_icons_are_deterministic(self):
        examples = {
            "mouse": "Mouse gamer", "teclado": "Teclado mecânico", "headset": "Headset gamer HyperX",
            "ssd": "SSD NVMe Kingston", "ram": "Memória RAM DDR4", "monitor": "Monitor LG",
            "notebook": "Notebook Lenovo", "gpu": "Placa de vídeo RTX", "cpu": "Processador Ryzen",
            "microfone": "Microfone USB", "webcam": "Webcam Logitech", "controle": "Controle Xbox",
            "power_bank": "Power Bank", "hub": "Hub USB", "carregador": "Carregador USB C",
            "setup": "Fita LED para setup", "other": "Panela de cozinha",
        }
        self.assertEqual(set(examples), set(CATEGORY_ICONS))
        for category, title in examples.items():
            with self.subTest(category=category):
                self.assertEqual(category_for(title), category)
                self.assertTrue(category_icon(category))
        self.assertEqual(category_for("Teclado musical"), "other")
        self.assertEqual(category_for("Mousepad"), "setup")
        self.assertEqual(canonical_category("SSD"), "ssd")
        self.assertEqual(canonical_category("memória RAM"), "ram")
        self.assertEqual(category_icon(None), CATEGORY_ICONS["other"])


class HistoryAndRepostTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(test_directory())
        self.config = Config(database_path=directory / "history.db")
        self.db = Database(self.config.database_path)
        self.product = fixture_product()

    def test_four_days_of_real_pipeline_history_detect_299_279_249_199(self):
        history = []
        for day, price in enumerate((29900, 27900, 24900, 19900)):
            product = replace(self.product, price=price)
            product_id, history = self.db.observe(product, NOW + timedelta(days=day), 30)
        self.assertEqual(history, [29900, 27900, 24900])
        self.assertEqual(score_offer(product, history).criteria["history"], 25)
        self.assertFalse(self.db.local_lowest_claim(product_id, 19900, NOW + timedelta(days=3), 30))
        self.assertTrue(self.db.local_lowest_claim(product_id, 19900, NOW + timedelta(days=3), 3))

    def test_many_observations_on_one_day_do_not_prove_daily_coverage(self):
        for minute in range(40):
            identity, _ = self.db.observe(self.product, NOW - timedelta(minutes=minute + 1), 30)
        self.assertFalse(self.db.local_lowest_claim(identity, self.product.price, NOW, 3))

    def test_gap_or_future_sample_cannot_complete_history_window(self):
        for days in (-3, -1, 1):
            identity, _ = self.db.observe(self.product, NOW + timedelta(days=days), 30)
        self.assertFalse(self.db.local_lowest_claim(identity, self.product.price, NOW, 3))

    def test_higher_current_price_is_not_local_lowest(self):
        for day in range(1, 4):
            identity, _ = self.db.observe(self.product, NOW - timedelta(days=day), 30)
        self.assertFalse(self.db.local_lowest_claim(identity, self.product.price + 1, NOW, 3))
        self.assertTrue(self.db.local_lowest_claim(identity, self.product.price, NOW, 3))

    def test_history_does_not_mix_other_items_or_sources(self):
        for day in range(1, 4):
            for product in (replace(self.product, external_id="FIXTURE-OTHER"), replace(self.product, source="other_source")):
                self.db.observe(product, NOW - timedelta(days=day), 30)
        identity, history = self.db.observe(self.product, NOW, 30)
        self.assertEqual(history, [])
        self.assertFalse(self.db.local_lowest_claim(identity, self.product.price, NOW, 3))

    def test_exact_timestamp_observation_does_not_duplicate(self):
        for _ in range(2):
            self.db.observe(self.product, NOW, 30)
        with self.db.connect() as connection:
            self.assertEqual(connection.execute("SELECT COUNT(*) FROM price_history").fetchone()[0], 1)

    def reserve(self, product, when):
        identity, _ = self.db.observe(product, when, 30)
        return self.db.reserve(identity, product, 100, when, self.config, simulate=True)

    def test_repost_blocked_before_72h_and_allowed_at_exact_boundary(self):
        self.assertIsNotNone(self.reserve(self.product, NOW)[0])
        self.assertIsNone(self.reserve(self.product, NOW + timedelta(hours=71))[0])
        self.assertIsNotNone(self.reserve(self.product, NOW + timedelta(hours=72))[0])

    def test_repost_with_drop_over_ten_percent_before_72h(self):
        self.reserve(self.product, NOW)
        lower = replace(self.product, price=self.product.price * 80 // 100)
        self.assertIsNotNone(self.reserve(lower, NOW + timedelta(hours=1))[0])

    def test_repost_threshold_is_at_least_ten_percent(self):
        product = replace(self.product, price=10000)
        self.reserve(product, NOW)
        self.assertIsNone(self.reserve(replace(product, price=9001), NOW + timedelta(hours=1))[0])
        self.assertIsNotNone(self.reserve(replace(product, price=9000), NOW + timedelta(hours=1))[0])

    def test_significant_drop_does_not_bypass_minimum_interval(self):
        self.reserve(self.product, NOW)
        lower = replace(self.product, price=10000)
        reservation, reason = self.reserve(lower, NOW + timedelta(minutes=1))
        self.assertIsNone(reservation)
        self.assertIn("intervalo mínimo", reason)

    def test_significant_drop_does_not_bypass_daily_limit(self):
        self.config = replace(self.config, max_posts_per_day=1)
        self.reserve(self.product, NOW)
        reservation, reason = self.reserve(replace(self.product, price=10000), NOW + timedelta(hours=1))
        self.assertIsNone(reservation)
        self.assertIn("limite diário", reason)


class SchedulerTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        directory = self.enterContext(test_directory())
        self.config = replace(fixture_config(database_path=directory / "schedule.db"),
                              search_terms=("mouse gamer",), search_terms_per_cycle=1)
        self.db = Database(self.config.database_path)
        self.source = FixtureSource()
        self.source.search = Mock(wraps=self.source.search)
        self.publisher = Mock(spec=TelegramService)
        self.clock = Mock(return_value=NOW)
        self.engine = OfferEngine(self.config, self.db, self.source, self.publisher,
                                  clock=self.clock, reporter=Mock())

    def test_active_scheduler_runs_at_start_and_only_after_30_minutes(self):
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 1)
        self.clock.return_value = NOW + timedelta(minutes=29)
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 1)
        self.clock.return_value = NOW + timedelta(minutes=30)
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 2)
        self.publisher.publish_offer.assert_not_called()
        self.assertEqual(self.db.stats(self.clock(), "UTC")["publications"], 0)

    def test_paused_scheduler_does_not_collect_until_resumed(self):
        self.db.set_state("paused", True)
        self.engine.scheduler_step()
        self.source.search.assert_not_called()
        self.assertIsNone(self.db.get_state("next_search"))
        self.clock.return_value = NOW + timedelta(days=1)
        self.engine.scheduler_step()
        self.source.search.assert_not_called()
        self.db.set_state("paused", False)
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 1)

    def test_explicit_manual_search_while_paused_collects_but_never_publishes(self):
        self.db.set_state("paused", True)
        self.assertTrue(self.engine.request_search())
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 1)
        self.assertEqual(self.engine.last_report.simulated, 0)
        self.publisher.publish_offer.assert_not_called()

    def test_restart_respects_persisted_next_search(self):
        due = NOW + timedelta(minutes=20)
        self.db.set_state("next_search", timestamp(due))
        engine = OfferEngine(self.config, self.db, self.source, self.publisher, clock=self.clock, reporter=Mock())
        engine.scheduler_step()
        self.source.search.assert_not_called()
        self.clock.return_value = due
        engine.scheduler_step()
        self.source.search.assert_called_once()

    def test_duplicate_manual_request_is_refused(self):
        self.assertTrue(self.engine.request_search())
        self.assertFalse(self.engine.request_search())
        self.engine.scheduler_step()
        self.assertEqual(self.source.search.call_count, 1)
        self.assertFalse(self.engine._requested.is_set())

    def test_start_is_idempotent_and_stop_rejects_new_requests(self):
        with patch("engine.threading.Thread") as thread:
            self.engine.start()
            self.engine.start()
            thread.assert_called_once()
            thread.return_value.start.assert_called_once()
            self.engine.stop()
            self.assertFalse(self.engine.request_search())

    def test_disabled_category_is_saved_but_cannot_enter_ranking(self):
        self.engine.config = replace(self.config, active_categories=("ssd",))
        self.engine.run_search()
        self.assertEqual(self.engine.last_report.offers, [])
        self.assertGreater(self.db.stats(NOW, "UTC")["products"], 0)
        self.publisher.publish_offer.assert_not_called()

    def test_unexpected_source_failure_never_logs_raw_exception(self):
        self.source.search.side_effect = RuntimeError("fake-secret-never-log")
        with self.assertLogs(level="ERROR") as logs:
            self.engine.run_search()
        self.assertEqual(self.engine.last_report.status, "failed")
        self.assertNotIn("fake-secret-never-log", "\n".join(logs.output))
