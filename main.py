"""Entrada do Radar Tech. Executar explicitamente; imports não iniciam rede."""

import argparse
import logging
import sys
import time
from dataclasses import replace

from admin_bot import AdminBot
from config import DEFAULT_TERMS, Config, ConfigError, load_config, load_public_channel_url, parse_item_ids
from database import Database
from engine import OfferEngine
from models import timestamp, utcnow
from runtime_lock import instance_lock
from safety import SafeError, configure_logging
from services.offer_source import AmazonSource, MercadoLivreAdapter
from telegram_service import TelegramService

logger = logging.getLogger(__name__)


def MercadoLivreSource(**kwargs):
    # Import tardio: a CLI fixture nem carrega a integração bloqueada.
    from services.mercado_livre import MercadoLivreSource as implementation
    return implementation(**kwargs)


def make_source(config: Config):
    if config.offer_source == "fixture":
        from services.fixture_source import FixtureSource
        return FixtureSource()
    if config.offer_source == "shopee":
        # Import tardio: só carrega a integração quando a Shopee é a fonte ativa.
        from services.shopee import ShopeeConfig, ShopeeSource
        return ShopeeSource(ShopeeConfig(app_id=config.shopee_app_id, secret=config.shopee_secret))
    if config.offer_source == "amazon":
        return AmazonSource()
    if config.offer_source != "mercado_livre":
        raise ConfigError("OFFER_SOURCE inválida.")
    return MercadoLivreAdapter(MercadoLivreSource(request_interval_seconds=config.meli_request_delay_seconds,
                               item_ids=config.meli_item_ids, negative_keywords=config.negative_keywords))


def poll_admin(admin: AdminBot, telegram: TelegramService, database: Database) -> int:
    offset = database.get_state("telegram_update_offset")
    backoff = 2
    connected = False
    while True:
        try:
            updates = telegram.get_updates(offset)
            database.set_state("last_telegram_poll", timestamp(utcnow()))
            if not connected:
                logger.info("Painel conectado ao Telegram; aguardando comandos privados.")
                connected = True
            backoff = 2
            for update in updates:
                update_id = update.get("update_id")
                if type(update_id) is not int or (offset is not None and update_id < offset):
                    continue
                offset = update_id + 1
                # Persiste antes dos efeitos: um restart não repete /resume ou /buscar.
                database.set_state("telegram_update_offset", offset)
                try:
                    admin.handle_update(update)
                except SafeError as error:
                    logger.error("%s", error)
                    database.add_error(str(error), utcnow())
                except Exception:
                    logger.error("Falha ao processar comando administrativo; detalhes omitidos.")
                    database.add_error("Falha ao processar comando administrativo.", utcnow())
        except SafeError as error:
            logger.error("%s", error)
            database.add_error(str(error), utcnow())
            if error.status == 401:
                return 1
            time.sleep(max(backoff, error.retry_after))
            backoff = min(backoff * 2, 60)


def run(config: Config, *, once: bool = False, admin_only: bool = False,
        search_now: bool = False, search_limit: int | None = None) -> int:
    if search_limit is not None and (not search_now or type(search_limit) is not int or not 1 <= search_limit <= 10):
        raise ConfigError("--search-limit exige --search-now e um inteiro entre 1 e 10.")
    if search_now:
        once = True
        terms = ("mouse gamer",) if search_limit is not None else DEFAULT_TERMS
        config = replace(config, auto_publish=False, search_terms=terms,
                         search_terms_per_cycle=len(terms), search_limit_per_term=search_limit or 10)
    if admin_only:
        config = replace(config, auto_publish=False)
    if config.offer_source == "fixture":
        from services.fixture_runner import fixture_config
        config = fixture_config(config, offline=False)
    source = make_source(config)
    if getattr(source, "simulation_only", False) is True:
        config = replace(config, auto_publish=False)
    with instance_lock(config.database_path.with_suffix(".lock")):
        database = Database(config.database_path)
        database.recover_interrupted()
        if config.offer_source == "fixture":
            from services.fixture_runner import seed_fixture_history
            seed_fixture_history(database, source, utcnow())
        telegram = TelegramService(config.telegram_bot_token, config.telegram_chat_id,
                                   auto_publish=config.auto_publish)
        engine = OfferEngine(config, database, source, telegram, reporter=print if once else None)
        admin = AdminBot(config, database, engine, telegram, search_enabled=not admin_only)
        database.set_state("online", True)
        database.set_state("auto_publish", config.auto_publish)
        logger.info("Radar Tech iniciado. Modo: %s.", "publicação real" if config.auto_publish else "simulação")
        logger.info("AUTO_PUBLISH=%s.", str(config.auto_publish).lower())
        if config.telegram_admin_id is None:
            logger.info("Admin não configurado. Use /whoami na conversa privada para descobrir seu ID.")
        try:
            if once:
                engine.run_search()
                last = database.stats(utcnow(), config.post_timezone)["last_run"]
                return 1 if last and last["status"] in {"failed", "partial"} else 0
            if admin_only:
                database.set_state("next_search", None)
                logger.info("Validação do painel: buscas de ofertas desativadas.")
            else:
                engine.start()
            return poll_admin(admin, telegram, database)
        finally:
            engine.stop()
            database.set_state("online", False)
            logger.info("Radar Tech encerrado.")


def _search_limit(value: str) -> int:
    try:
        limit = int(value)
    except ValueError:
        raise argparse.ArgumentTypeError("use um inteiro entre 1 e 10") from None
    if not 1 <= limit <= 10:
        raise argparse.ArgumentTypeError("use um inteiro entre 1 e 10")
    return limit


def _item_ids(value: str) -> tuple[str, ...]:
    try:
        identifiers = parse_item_ids(value)
        if not identifiers:
            raise ConfigError("Informe ao menos um ID MLB de anúncio.")
        return identifiers
    except ConfigError as error:
        raise argparse.ArgumentTypeError(str(error)) from None


def main() -> int:
    parser = argparse.ArgumentParser(description="Radar Tech: busca de ofertas e painel privado no Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="força simulação mesmo se AUTO_PUBLISH=true")
    parser.add_argument("--fixture", action="store_true", help="simulação local com fixtures e URL pública opcional, sem credenciais, rede ou Telegram; executa e encerra")
    parser.add_argument("--once", action="store_true", help="executa um ciclo de busca, sem painel, e encerra")
    parser.add_argument("--search-now", action="store_true", help="consulta IDs configurados e filtra por 3 termos tech; força simulação, sem Telegram, e encerra")
    parser.add_argument("--search-limit", type=_search_limit, metavar="N",
                        help="com --search-now, filtra por 'mouse gamer' e consulta de 1 a 10 IDs configurados")
    parser.add_argument("--item-ids", type=_item_ids, metavar="IDS",
                        help="IDs públicos MLB separados por vírgulas; entrada direta, sem descoberta global")
    parser.add_argument("--admin-only", action="store_true", help="somente painel privado, sem buscas nem ofertas")
    args = parser.parse_args()
    if args.fixture and (args.admin_only or args.search_now or args.search_limit is not None or args.item_ids is not None):
        parser.error("--fixture não combina com opções de fonte real ou painel")
    if (args.once or args.search_now) and args.admin_only:
        parser.error("--once/--search-now não podem ser usados com --admin-only")
    if args.search_limit is not None and not args.search_now:
        parser.error("--search-limit exige --search-now")
    if (args.once or args.search_now or args.fixture) and hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="backslashreplace")
    configure_logging()
    try:
        if args.fixture:
            from services.fixture_runner import run_fixture_simulation
            report = run_fixture_simulation(Config(public_channel_url=load_public_channel_url()))
            return 1 if report.status in {"failed", "partial"} else 0
        config = load_config(force_dry_run=args.dry_run or args.admin_only or args.search_now)
        if args.item_ids is not None:
            config = replace(config, meli_item_ids=args.item_ids)
        configure_logging(tuple(secret for secret in (config.telegram_bot_token, config.shopee_secret) if secret))
        return run(config, once=args.once, admin_only=args.admin_only,
                   search_now=args.search_now, search_limit=args.search_limit)
    except (ConfigError, SafeError) as error:
        logger.error("%s", error)
        return 1
    except KeyboardInterrupt:
        return 0
    except Exception:
        logger.error("Não foi possível continuar. Confira instalação e acesso ao banco; detalhes sensíveis omitidos.")
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
