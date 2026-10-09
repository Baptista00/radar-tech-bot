"""Configuração sem efeitos no import; o .env só é carregado ao iniciar o app."""

import math
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Mapping
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
from services.offer_relevance import DEFAULT_NEGATIVE_KEYWORDS

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_TERMS = (
    "mouse gamer", "headset gamer", "ssd nvme",
)
SUGGESTED_TERMS = (
    "teclado mecânico", "mousepad", "monitor gamer", "notebook",
    "memória ram ddr4", "memória ram ddr5", "placa de vídeo", "processador",
    "microfone usb", "webcam", "controle xbox", "power bank", "hub usb", "carregador usb c",
)
DEFAULT_KNOWN_BRANDS = (
    "Logitech", "HyperX", "Kingston", "Samsung", "Crucial", "Corsair", "Razer",
    "Redragon", "Lenovo", "Acer", "Asus", "Dell", "LG", "AMD", "Intel", "Nvidia",
)


class ConfigError(ValueError):
    """Mensagem segura: identifica a opção, nunca seu valor."""


@dataclass(frozen=True)
class Config:
    telegram_bot_token: str = field(default="", repr=False)
    telegram_chat_id: str = "@RadarTechOfertas"
    telegram_admin_id: int | None = None
    auto_publish: bool = False
    min_offer_score: int = 70
    max_posts_per_day: int = 12
    min_minutes_between_posts: int = 30
    republish_after_hours: int = 72
    significant_price_drop_percent: int = 10
    search_interval_minutes: int = 60
    search_terms: tuple[str, ...] = DEFAULT_TERMS
    search_terms_per_cycle: int = 3
    search_limit_per_term: int = 10
    known_brands: tuple[str, ...] = DEFAULT_KNOWN_BRANDS
    request_interval_seconds: int = 2
    meli_request_delay_seconds: float = 1.0
    meli_item_ids: tuple[str, ...] = ()
    negative_keywords: tuple[str, ...] = DEFAULT_NEGATIVE_KEYWORDS
    history_days: int = 30
    post_timezone: str = "America/Sao_Paulo"
    database_path: Path = BASE_DIR / "data" / "radartech.db"


def _integer(env: Mapping[str, str], name: str, default: int,
             minimum: int, maximum: int) -> int:
    try:
        value = int(env.get(name, str(default)))
    except (TypeError, ValueError):
        raise ConfigError(f"{name} precisa ser um número inteiro.") from None
    if not minimum <= value <= maximum:
        raise ConfigError(f"{name} precisa ficar entre {minimum} e {maximum}.")
    return value


def _request_delay(env: Mapping[str, str]) -> float:
    try:
        value = float(env.get("MELI_REQUEST_DELAY_SECONDS", "1.0"))
    except (TypeError, ValueError):
        raise ConfigError("MELI_REQUEST_DELAY_SECONDS precisa ser um número.") from None
    if not math.isfinite(value) or not 1 <= value <= 60:
        raise ConfigError("MELI_REQUEST_DELAY_SECONDS precisa ficar entre 1 e 60 segundos.")
    return value


def parse_item_ids(value: str) -> tuple[str, ...]:
    identities = tuple(dict.fromkeys(part.strip() for part in value.split(",") if part.strip()))
    if len(identities) > 50 or any(not re.fullmatch(r"MLB[0-9]{1,20}", identity) for identity in identities):
        raise ConfigError("MELI_ITEM_IDS exige até 50 IDs MLB válidos, separados por vírgulas.")
    return identities


def load_config(environ: Mapping[str, str] | None = None, *, force_dry_run: bool = False) -> Config:
    # Nos testes, passar um mapping evita qualquer leitura do .env.
    if environ is None:
        load_dotenv(BASE_DIR / ".env", override=False)
        environ = os.environ
    mode = "false" if force_dry_run else environ.get("AUTO_PUBLISH", "false").strip().lower()
    if mode not in {"true", "false"}:
        raise ConfigError("AUTO_PUBLISH aceita somente true ou false.")
    raw_admin = environ.get("TELEGRAM_ADMIN_ID", "").strip()
    admin_id = None
    if raw_admin:
        admin_id = _integer(environ, "TELEGRAM_ADMIN_ID", 0, 1, 2**52)
    token = environ.get("TELEGRAM_BOT_TOKEN", "").strip()
    if not token or token.lower() in {"cole_seu_token_aqui", "seu_token_aqui"}:
        raise ConfigError("Configure TELEGRAM_BOT_TOKEN antes de iniciar.")
    chat_id = environ.get("TELEGRAM_CHAT_ID", "@RadarTechOfertas").strip()
    if not chat_id:
        raise ConfigError("Configure TELEGRAM_CHAT_ID antes de iniciar.")
    if mode == "true" and admin_id is None:
        raise ConfigError("AUTO_PUBLISH=true exige TELEGRAM_ADMIN_ID configurado.")
    timezone_name = environ.get("POST_TIMEZONE", "America/Sao_Paulo").strip()
    try:
        ZoneInfo(timezone_name)
    except (ZoneInfoNotFoundError, ValueError):
        raise ConfigError("POST_TIMEZONE inválido ou tzdata não instalado.") from None
    raw_terms = environ.get("SEARCH_TERMS", ",".join(DEFAULT_TERMS))
    terms = tuple(dict.fromkeys(t.strip() for t in raw_terms.split(",") if t.strip()))
    if not terms or len(terms) > 50 or any(len(term) > 80 for term in terms):
        raise ConfigError("SEARCH_TERMS exige de 1 a 50 termos de até 80 caracteres.")
    brands = tuple(dict.fromkeys(b.strip() for b in environ.get("KNOWN_BRANDS", ",".join(DEFAULT_KNOWN_BRANDS)).split(",") if b.strip()))
    if len(brands) > 100 or any(len(brand) > 80 for brand in brands):
        raise ConfigError("KNOWN_BRANDS aceita até 100 marcas de até 80 caracteres.")
    negatives = tuple(dict.fromkeys(word.strip() for word in environ.get("NEGATIVE_KEYWORDS", ",".join(DEFAULT_NEGATIVE_KEYWORDS)).split(",") if word.strip()))
    if len(negatives) > 100 or any(len(word) > 80 for word in negatives):
        raise ConfigError("NEGATIVE_KEYWORDS aceita até 100 expressões de até 80 caracteres.")
    return Config(
        telegram_bot_token=token, telegram_chat_id=chat_id,
        telegram_admin_id=admin_id, auto_publish=mode == "true",
        min_offer_score=_integer(environ, "MIN_OFFER_SCORE", 70, 0, 100),
        max_posts_per_day=_integer(environ, "MAX_POSTS_PER_DAY", 12, 1, 100),
        min_minutes_between_posts=_integer(environ, "MIN_MINUTES_BETWEEN_POSTS", 30, 1, 1440),
        republish_after_hours=_integer(environ, "REPUBLISH_AFTER_HOURS", 72, 1, 8760),
        significant_price_drop_percent=_integer(environ, "SIGNIFICANT_PRICE_DROP_PERCENT", 10, 1, 90),
        search_interval_minutes=_integer(environ, "SEARCH_INTERVAL_MINUTES", 60, 5, 1440),
        search_terms=terms,
        # Limites desta etapa também protegem configurações antigas do ambiente.
        search_terms_per_cycle=min(3, _integer(environ, "SEARCH_TERMS_PER_CYCLE", 3, 1, 10)),
        search_limit_per_term=min(10, _integer(environ, "SEARCH_LIMIT_PER_TERM", 10, 1, 50)),
        known_brands=brands,
        request_interval_seconds=_integer(environ, "REQUEST_INTERVAL_SECONDS", 2, 1, 60),
        meli_request_delay_seconds=_request_delay(environ),
        meli_item_ids=parse_item_ids(environ.get("MELI_ITEM_IDS", "")),
        negative_keywords=negatives,
        history_days=_integer(environ, "HISTORY_DAYS", 30, 1, 365),
        post_timezone=timezone_name,
    )
