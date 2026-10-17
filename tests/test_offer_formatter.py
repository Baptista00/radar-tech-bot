"""Layout HTML, campos opcionais e transporte, sempre com dados locais."""

from dataclasses import asdict, replace
from html import unescape
from html.parser import HTMLParser
from io import StringIO
from pathlib import Path
from unittest.mock import patch

from config import Config, dotenv_values, load_config, load_public_channel_url
from models import Product, ScoreResult
from safety import SafeError
from services.categories import CATEGORY_ICONS
from services.offer_formatter import CATEGORY_DESCRIPTIONS, PROMOTION_NOTICE, format_offer
from services.publisher import OfflinePublisher
from telegram_service import TelegramService
from test_mvp import IsolatedTest, test_directory


class ParsedMessage(HTMLParser):
    def __init__(self, message):
        super().__init__(convert_charrefs=True)
        self.tags = []
        self.text = []
        self.feed(message)

    def handle_starttag(self, tag, attrs):
        self.tags.append((tag, attrs))

    def handle_data(self, data):
        self.text.append(data)


class OfferFormatterTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.product = Product("LOCAL-MOUSE", "Logitech G305 Wireless", 17990,
                               "https://example.com/product", source="local", category="mouse")
        self.result = ScoreResult(84, {}, True)

    def message(self, **changes):
        return format_offer(replace(self.product, **changes), self.result)

    def test_current_price_is_bold_and_brazilian_currency(self):
        self.assertIn("🔥 <b>Por: R$ 179,90</b>", self.message())
        self.assertIn("🔥 <b>Por: R$ 2.499,00</b>", self.message(price=249900))

    def test_valid_reference_is_struck_through_before_current_price(self):
        self.assertIn("De: <s>R$ 299,90</s>\n🔥 <b>Por: R$ 179,90</b>",
                      self.message(original_price=29990))

    def test_missing_reference_omits_de_and_discount(self):
        text = self.message()
        self.assertNotIn("De:", text)
        self.assertNotIn("<s>", text)
        self.assertNotIn("OFF", text)

    def test_invalid_references_never_become_discount(self):
        for original in (0, -1, 17990, 10000, True, "29990", 29990.0, float("nan"), 10_000_000_000):
            with self.subTest(original=original):
                text = self.message(original_price=original)
                self.assertNotIn("De:", text)
                self.assertNotIn("OFF", text)

    def test_discount_comes_from_valid_reference_only(self):
        self.assertIn("📉 40% OFF", self.message(price=18000, original_price=30000))
        self.assertIn("📉 28,01% OFF", self.message(original_price=24990))

    def test_rounding_never_advertises_zero_or_one_hundred_percent_off(self):
        for price, reference in ((9999999, 10000000), (1, 10000000)):
            with self.subTest(price=price):
                self.assertNotIn("% OFF", self.message(price=price, original_price=reference))

    def test_history_evidence_does_not_fabricate_reference_price(self):
        result = ScoreResult(84, {"history": 25}, True, history_claim_days=30)
        text = format_offer(self.product, result)
        self.assertNotIn("De:", text)
        self.assertNotIn("OFF", text)
        self.assertIn("30 dias do histórico local", text)

    def test_confirmed_free_shipping_is_shown(self):
        self.assertIn("🚚 Frete grátis", self.message(free_shipping=True))

    def test_unknown_paid_or_truthy_shipping_is_omitted(self):
        for shipping in (None, False, "true", 1):
            with self.subTest(shipping=shipping):
                self.assertNotIn("Frete", self.message(free_shipping=shipping))

    def test_radar_score_preserves_all_existing_bands_and_boundaries(self):
        bands = {0: "Fraco", 39: "Fraco", 40: "Regular", 59: "Regular", 60: "Interessante",
                 69: "Interessante", 70: "Boa oferta", 84: "Boa oferta", 85: "Oferta excelente", 100: "Oferta excelente"}
        for score, band in bands.items():
            with self.subTest(score=score):
                text = format_offer(self.product, ScoreResult(score, {}, False))
                self.assertIn(f"📡 Radar Score: {score}/100 — {band}", text)
                self.assertNotIn("<b>📡 Radar Score", text)

    def test_short_description_uses_only_category_and_no_unverified_attributes(self):
        description = self.message().split("\n\n")[1]
        self.assertEqual(description, "Uma opção para quem está procurando melhorar o setup.")
        self.assertNotIn("sem fio", description)
        self.assertNotIn("leve", description)
        for category in CATEGORY_ICONS:
            with self.subTest(category=category):
                description = self.message(category=category).split("\n\n")[1]
                self.assertEqual(len(description.splitlines()), 1)
                self.assertLessEqual(len(description), 90)

    def test_title_preserves_name_brand_and_model_without_appending_duplicates(self):
        title = "  Mouse Gamer Logitech G305 Wireless  "
        text = self.message(title=title, brand="Logitech")
        self.assertEqual(text.splitlines()[0], "<b>🖱️ Mouse Gamer Logitech G305 Wireless</b>")
        self.assertEqual(text.count("Logitech"), 1)
        self.assertEqual(text.count("G305"), 1)

    def test_category_emojis_and_descriptions_cover_all_categories(self):
        expected = {"mouse": "🖱️", "teclado": "⌨️", "headset": "🎧", "ssd": "💾", "ram": "💾",
                    "monitor": "🖥️", "notebook": "💻", "gpu": "🎮", "cpu": "⚙️", "microfone": "🎙️",
                    "webcam": "📷", "controle": "🎮", "power_bank": "🔋", "hub": "🔌",
                    "carregador": "🔌", "setup": "🛠️", "other": "✨"}
        self.assertEqual(set(CATEGORY_DESCRIPTIONS), set(expected))
        for category, icon in expected.items():
            with self.subTest(category=category):
                self.assertTrue(self.message(category=category).startswith(f"<b>{icon} "))

    def test_category_aliases_and_unknown_category_have_safe_fallback(self):
        for alias in ("SSD", "memória RAM"):
            self.assertTrue(self.message(category=alias).startswith("<b>💾 "))
        for category in (None, "unknown", '<b>custom</b>'):
            text = self.message(category=category)
            self.assertTrue(text.startswith("<b>✨ "))
            self.assertIn("Confira as características e condições do produto no anúncio.", text)

    def test_cta_appears_only_when_configured_before_notice(self):
        text = format_offer(self.product, self.result, public_channel_url="https://t.me/example")
        self.assertTrue(text.endswith("Envie o Radar Tech pra galera 👇\nhttps://t.me/example\n\n" + PROMOTION_NOTICE))

    def test_absent_cta_omits_the_entire_block(self):
        for url in ("", "   ", None):
            text = format_offer(self.product, self.result, public_channel_url=url)
            self.assertNotIn("Envie o Radar Tech", text)
            self.assertNotIn("t.me/", text)
            self.assertNotIn("\n\n\n", text)

    def test_invalid_cta_does_not_prevent_formatting_or_leak_credentials(self):
        for url in ("javascript:alert(1)", "https://", "https://user:private@example.com/",
                    "https://example.com:bad/", "https://example.com\n<script>", "https://example.com\\test"):
            with self.subTest(url=url):
                text = format_offer(self.product, self.result, public_channel_url=url)
                self.assertNotIn("Envie o Radar Tech", text)
                self.assertNotIn("private", text)
                self.assertTrue(text.endswith(PROMOTION_NOTICE))

    def test_product_html_and_quotes_cannot_create_markup(self):
        title = 'Mouse & <Pro> "Special" <a href="https://example.com">modelo</a>'
        text = self.message(title=title, brand='<script>brand</script>')
        self.assertIn('Mouse &amp; &lt;Pro&gt; &quot;Special&quot;', text)
        parsed = ParsedMessage(text)
        self.assertIn(title, "".join(parsed.text))
        self.assertEqual(parsed.tags, [("b", []), ("b", []), ("i", [])])

    def test_names_with_ampersands_and_angle_brackets_survive_rendering(self):
        title = "Mouse A&B <Modelo> > Versão"
        parsed = ParsedMessage(self.message(title=title))
        self.assertIn(title, "".join(parsed.text))

    def test_pipeline_link_is_preserved_including_affiliate_parameters(self):
        url = 'https://shop.example/product?affiliate=radar&variant=2#details'
        for source in ("store_a", "future_affiliate"):
            text = self.message(url=url, source=source)
            line = next(line for line in text.splitlines() if line.startswith("Compre aqui"))
            self.assertEqual(unescape(line), "Compre aqui 👉 " + url)
            self.assertIn("&amp;variant=2", line)

    def test_product_and_cta_links_escape_html_without_creating_tags(self):
        url = 'https://example.com/product?q="mouse"&a=<b>1</b>'
        text = format_offer(replace(self.product, url=url), self.result, public_channel_url=url)
        parsed = ParsedMessage(text)
        self.assertEqual(parsed.tags, [("b", []), ("b", []), ("i", [])])
        self.assertEqual("".join(parsed.text).count(url), 2)

    def test_invalid_product_url_is_not_rendered_as_purchase_link(self):
        for url in ("javascript:alert(1)", "https://user:private@example.com/x", "https://[invalid"):
            text = self.message(url=url)
            self.assertNotIn("Compre aqui", text)
            self.assertNotIn(url, text)

    def test_message_without_optional_fields_has_exact_compact_layout(self):
        product = Product("LOCAL-SSD", "SSD Kingston NV2 1TB", 34990,
                          "https://example.com/product", source="local", category="ssd")
        text = format_offer(product, ScoreResult(73, {}, True))
        self.assertEqual(text, "<b>💾 SSD Kingston NV2 1TB</b>\n\n"
                         "Uma opção para quem está buscando mais armazenamento para o PC.\n\n"
                         "🔥 <b>Por: R$ 349,90</b>\n\n"
                         "📡 Radar Score: 73/100 — Boa oferta\n\n"
                         "Compre aqui 👉 https://example.com/product\n\n" + PROMOTION_NOTICE)

    def test_complete_message_has_exact_layout(self):
        product = replace(self.product, price=18000, original_price=30000, free_shipping=True)
        text = format_offer(product, self.result, public_channel_url="https://t.me/example")
        self.assertEqual(text, "<b>🖱️ Logitech G305 Wireless</b>\n\n"
                         "Uma opção para quem está procurando melhorar o setup.\n\n"
                         "De: <s>R$ 300,00</s>\n🔥 <b>Por: R$ 180,00</b>\n📉 40% OFF\n\n"
                         "📡 Radar Score: 84/100 — Boa oferta\n🚚 Frete grátis\n\n"
                         "Compre aqui 👉 https://example.com/product\n\n"
                         "Envie o Radar Tech pra galera 👇\nhttps://t.me/example\n\n" + PROMOTION_NOTICE)

    def test_no_invented_claims_with_unknown_metadata(self):
        product = Product("LOCAL-UNKNOWN", "Produto", 10000, "https://example.com/product", source="local")
        text = format_offer(product, ScoreResult(0, {}, False)).lower()
        for claim in ("ótimo", "super resistente", "alta performance", "melhor custo-benefício",
                      "perfeito para", "produto excelente", "frete", "estoque", "avaliação", "off",
                      "sem fio", "leve", "compatível", "menor preço", "de:"):
            self.assertNotIn(claim, text)

    def test_rating_only_appears_for_valid_supplied_values(self):
        self.assertIn("Avaliação: 4.7", self.message(rating=4.7))
        for rating in (None, True, "5", float("nan"), float("inf"), -1, 6):
            self.assertNotIn("Avaliação:", self.message(rating=rating))

    def test_notice_is_always_last_with_one_blank_line_before_it(self):
        for changes in ({}, {"original_price": 29990}, {"free_shipping": True}, {"source": "fixture"}):
            text = self.message(**changes)
            self.assertTrue(text.endswith("\n\n" + PROMOTION_NOTICE))
            self.assertNotIn("\n\n\n", text)
            self.assertEqual(text.strip(), text)

    def test_formatter_does_not_mutate_product_or_score(self):
        before = (asdict(self.product), asdict(self.result))
        format_offer(self.product, self.result, public_channel_url="https://t.me/example")
        self.assertEqual(before, (asdict(self.product), asdict(self.result)))

    def test_current_price_prefix_contains_fire_codepoint(self):
        line = next(line for line in self.message().splitlines() if "<b>Por:" in line)
        self.assertEqual(ord(line[0]), 0x1F525)
        self.assertTrue(line.startswith(chr(0x1F525) + " <b>Por:"))

    def test_discount_prefix_contains_chart_decreasing_codepoint(self):
        text = self.message(price=17900, original_price=29900)
        line = next(line for line in text.splitlines() if "% OFF" in line)
        self.assertEqual(line, chr(0x1F4C9) + " 40,13% OFF")

    def test_discount_emoji_is_absent_without_valid_discount(self):
        for reference in (None, 0, 17990, 10000):
            with self.subTest(reference=reference):
                self.assertNotIn(chr(0x1F4C9), self.message(original_price=reference))

    def test_radar_score_prefix_contains_satellite_antenna_codepoint(self):
        line = next(line for line in self.message().splitlines() if "Radar Score:" in line)
        self.assertEqual(ord(line[0]), 0x1F4E1)
        self.assertTrue(line.startswith(chr(0x1F4E1) + " Radar Score:"))

    def test_sharing_suffix_contains_down_pointing_finger_codepoint(self):
        text = format_offer(self.product, self.result, public_channel_url="https://t.me/example")
        line = next(line for line in text.splitlines() if line.startswith("Envie o Radar Tech"))
        self.assertEqual(line, "Envie o Radar Tech pra galera " + chr(0x1F447))

    def test_offer_layout_does_not_contain_old_icons(self):
        for reference in (None, 29900):
            with self.subTest(reference=reference):
                text = format_offer(replace(self.product, original_price=reference), self.result,
                                    public_channel_url="https://t.me/example")
                for codepoint in (0x1F4B0, 0x1F3F7, 0x2B50, 0x1F4E2):
                    self.assertNotIn(chr(codepoint), text)

    def test_message_utf8_keeps_all_icons_and_html(self):
        text = format_offer(replace(self.product, price=17900, original_price=29900, free_shipping=True),
                            self.result, public_channel_url="https://t.me/example")
        encoded = text.encode("utf-8")
        for codepoints in ((0x1F525,), (0x1F4C9,), (0x1F4E1,), (0x1F447,), (0x1F69A,), (0x1F5B1, 0xFE0F)):
            self.assertIn("".join(map(chr, codepoints)).encode("utf-8"), encoded)
        self.assertEqual(encoded.decode("utf-8"), text)
        self.assertNotIn("\ufffd", text)
        self.assertIn("<b>Por: R$ 179,00</b>", text)
        self.assertIn("<s>R$ 299,00</s>", text)

    def test_user_provided_public_url_is_after_purchase_link_before_notice(self):
        config = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder",
                              "PUBLIC_CHANNEL_URL": "https://t.me/RadarTechOfertas"})
        text = format_offer(self.product, self.result, public_channel_url=config.public_channel_url)
        self.assertTrue(text.endswith("Compre aqui 👉 https://example.com/product\n\n"
                                     "Envie o Radar Tech pra galera 👇\nhttps://t.me/RadarTechOfertas\n\n"
                                     + PROMOTION_NOTICE))


class FormatterConfigurationAndTransportTests(IsolatedTest):
    def test_public_channel_configuration_is_optional_and_auto_publish_stays_false(self):
        config = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder"})
        self.assertEqual(config.public_channel_url, "")
        self.assertEqual(Config().public_channel_url, "")
        self.assertFalse(config.auto_publish)

    def test_public_channel_configuration_is_loaded_without_reading_dotenv(self):
        config = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder",
                              "PUBLIC_CHANNEL_URL": "  https://t.me/example  "})
        self.assertEqual(config.public_channel_url, "https://t.me/example")
        self.assertFalse(config.auto_publish)

    def test_empty_or_whitespace_public_channel_configuration_is_optional(self):
        for value in ("", " ", "\t \n"):
            with self.subTest(value=value):
                config = load_config({"TELEGRAM_BOT_TOKEN": "test-only-placeholder", "PUBLIC_CHANNEL_URL": value})
                self.assertEqual(config.public_channel_url, "")
                self.assertFalse(config.auto_publish)

    def test_offer_transport_uses_html_and_preserves_link_preview_with_mock_only(self):
        telegram = TelegramService("test-only-placeholder", "@example", auto_publish=True)
        with patch.object(telegram, "_call", return_value={"message_id": 7}) as call:
            self.assertEqual(telegram.publish_offer("<b>Oferta</b>"), 7)
        self.assertEqual(call.call_args.args, ("sendMessage", {
            "chat_id": "@example", "text": "<b>Oferta</b>", "parse_mode": "HTML",
            "link_preview_options": {"is_disabled": False}}))

    def test_admin_reply_remains_plain_text_with_preview_disabled(self):
        telegram = TelegramService("test-only-placeholder", "@example")
        with patch.object(telegram, "_call", return_value={"message_id": 7}) as call:
            telegram.reply(42, "<admin> & dados")
        data = call.call_args.args[1]
        self.assertNotIn("parse_mode", data)
        self.assertEqual(data["text"], "<admin> & dados")
        self.assertEqual(data["link_preview_options"], {"is_disabled": True})

    def test_auto_publish_false_prevents_even_a_mock_transport_call(self):
        telegram = TelegramService("test-only-placeholder", "@example")
        with patch.object(telegram, "_call") as call, self.assertRaises(SafeError):
            telegram.publish_offer("<b>Oferta</b>")
        call.assert_not_called()

    def test_offline_publisher_never_sends_formatted_messages(self):
        publisher = OfflinePublisher()
        with self.assertRaises(SafeError):
            publisher.publish_offer("<b>Oferta</b>")


class PublicFixtureConfigurationTests(IsolatedTest):
    def setUp(self):
        super().setUp()
        self.directory = self.enterContext(test_directory())
        self.path = self.directory / "public-settings-test.txt"

    def test_explicit_mapping_never_reads_a_file(self):
        with patch.object(Path, "open", side_effect=AssertionError("Arquivo proibido")) as opened:
            self.assertEqual(load_public_channel_url({}), "")
            for value in ("", "   ", "\t\n", " https://t.me/example "):
                self.assertEqual(load_public_channel_url({"PUBLIC_CHANNEL_URL": value}), value.strip())
        opened.assert_not_called()

    def test_only_public_assignment_is_parsed_without_loading_other_options(self):
        self.path.write_text("PRIVATE_TEST_VALUE=do-not-load\nAUTO_PUBLISH=true\n"
                             "PUBLIC_CHANNEL_URL=https://t.me/example\nOTHER_TEST_VALUE=ignore\n", encoding="utf-8")
        parsed_lines = []
        def parse_public_line(*, stream, interpolate):
            line = stream.getvalue()
            parsed_lines.append(line)
            self.assertFalse(interpolate)
            return dotenv_values(stream=StringIO(line), interpolate=interpolate)
        with patch("config.os.environ", {}) as environment, \
             patch("config.dotenv_values", side_effect=parse_public_line):
            value = load_public_channel_url({}, dotenv_path=self.path)
            self.assertEqual(environment, {})
        self.assertEqual(value, "https://t.me/example")
        self.assertEqual(parsed_lines, ["PUBLIC_CHANNEL_URL=https://t.me/example\n"])

    def test_public_file_absent_empty_or_whitespace_omits_cta(self):
        for content in ("OTHER_TEST_VALUE=ignore\n", "PUBLIC_CHANNEL_URL=\n",
                        'PUBLIC_CHANNEL_URL="   "\n', "PUBLIC_CHANNEL_URL=   # opcional\n"):
            with self.subTest(content=content):
                self.path.write_text(content, encoding="utf-8")
                self.assertEqual(load_public_channel_url({}, dotenv_path=self.path), "")

    def test_missing_optional_public_file_returns_empty(self):
        self.assertEqual(load_public_channel_url({}, dotenv_path=self.path), "")

    def test_environment_including_explicit_empty_value_takes_precedence(self):
        with patch.object(Path, "open", side_effect=AssertionError("Arquivo desnecessário")) as opened:
            for value in ("", "  ", "https://t.me/from_environment"):
                self.assertEqual(load_public_channel_url({"PUBLIC_CHANNEL_URL": value}, dotenv_path=self.path), value.strip())
        opened.assert_not_called()

    def test_public_file_accepts_utf8_bom_quotes_export_and_inline_comment(self):
        self.path.write_text('export PUBLIC_CHANNEL_URL = " https://t.me/example " # CTA\n', encoding="utf-8-sig")
        self.assertEqual(load_public_channel_url({}, dotenv_path=self.path), "https://t.me/example")

    def test_public_file_does_not_expand_private_environment_variables(self):
        self.path.write_text('PUBLIC_CHANNEL_URL="https://example.com/${PRIVATE_TEST_VALUE}"\n', encoding="utf-8")
        with patch("config.os.environ", {"PRIVATE_TEST_VALUE": "do-not-expand"}):
            value = load_public_channel_url({}, dotenv_path=self.path)
        self.assertEqual(value, "https://example.com/${PRIVATE_TEST_VALUE}")

    def test_default_cli_loader_uses_mock_file_without_reading_real_dotenv(self):
        with patch("config.os.environ", {}), \
             patch.object(Path, "open", return_value=StringIO("PUBLIC_CHANNEL_URL=https://t.me/example\n")) as opened:
            self.assertEqual(load_public_channel_url(), "https://t.me/example")
        opened.assert_called_once_with(encoding="utf-8-sig")
