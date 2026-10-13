"""Simulação completa e isolada: SQLite de fixtures, sem configuração secreta."""

import logging
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from config import Config
from database import Database
from engine import OfferEngine
from models import SearchReport, timestamp, utcnow
from runtime_lock import instance_lock
from services.fixture_source import FIXTURE_TERMS, FixtureSource
from services.offer_normalizer import normalize_candidate
from services.publisher import OfflinePublisher


def fixture_config(base: Config | None = None, *, database_path: Path | None = None,
                   offline: bool = True) -> Config:
    base = base or Config()
    config = replace(base, auto_publish=False, offer_source="fixture",
                     database_path=database_path or base.database_path.parent / "radartech-fixture.db",
                     search_terms=FIXTURE_TERMS, search_terms_per_cycle=len(FIXTURE_TERMS),
                     search_limit_per_term=10, request_interval_seconds=0)
    if offline:
        config = replace(config, telegram_bot_token="", telegram_chat_id="", telegram_admin_id=None)
    return config


def seed_fixture_history(database: Database, source: FixtureSource, now: datetime) -> None:
    if database.get_state("fixture_history_seeded", False):
        return
    # Âncora persistente: reiniciar durante a carga não inventa datas novas.
    origin = database.get_state("fixture_history_origin")
    if origin is None:
        origin = timestamp(now)
        database.set_state("fixture_history_origin", origin)
    anchor = datetime.fromisoformat(origin)
    for candidate, term, prices in source.history_samples():
        product, _ = normalize_candidate(candidate, term)
        if product is not None:
            for index, price in enumerate(prices):
                database.observe(replace(product, price=price, original_price=None),
                                 anchor - timedelta(days=len(prices) - index), max(30, len(prices)))
    database.set_state("fixture_history_seeded", True)


def run_fixture_simulation(base: Config | None = None, *, database_path: Path | None = None,
                           clock=utcnow, reporter=print) -> SearchReport:
    config = fixture_config(base, database_path=database_path)
    with instance_lock(config.database_path.with_suffix(".lock")):
        database = Database(config.database_path)
        database.recover_interrupted()
        source = FixtureSource()
        seed_fixture_history(database, source, clock())
        engine = OfferEngine(config, database, source, OfflinePublisher(), clock=clock, reporter=reporter)
        database.set_state("online", True)
        database.set_state("auto_publish", False)
        logging.getLogger(__name__).info("Fixture offline: AUTO_PUBLISH=false. Telegram desativado. Histórico sintético.")
        try:
            engine.run_search()
            return engine.last_report
        finally:
            engine.stop()
            database.set_state("online", False)
