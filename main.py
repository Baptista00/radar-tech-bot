"""Entrada do Radar Tech. Executar explicitamente; imports não iniciam rede."""

import argparse
import logging
import time
from dataclasses import replace

from admin_bot import AdminBot
from config import Config, ConfigError, load_config
from database import Database
from engine import OfferEngine
from models import timestamp, utcnow
from runtime_lock import instance_lock
from safety import SafeError, configure_logging
from services.mercado_livre import MercadoLivreSource
from telegram_service import TelegramService

logger = logging.getLogger(__name__)


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


def run(config: Config, *, once: bool = False, admin_only: bool = False) -> int:
    if admin_only:
        config = replace(config, auto_publish=False)
    with instance_lock(config.database_path.with_suffix(".lock")):
        database = Database(config.database_path)
        database.recover_interrupted()
        telegram = TelegramService(config.telegram_bot_token, config.telegram_chat_id,
                                   auto_publish=config.auto_publish)
        engine = OfferEngine(config, database, MercadoLivreSource(config.mercado_livre_access_token), telegram)
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
                return 1 if last and last["status"] == "failed" else 0
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


def main() -> int:
    parser = argparse.ArgumentParser(description="Radar Tech: busca de ofertas e painel privado no Telegram.")
    parser.add_argument("--dry-run", action="store_true", help="força simulação mesmo se AUTO_PUBLISH=true")
    parser.add_argument("--once", action="store_true", help="executa um ciclo de busca, sem painel, e encerra")
    parser.add_argument("--admin-only", action="store_true", help="somente painel privado, sem buscas nem ofertas")
    args = parser.parse_args()
    if args.once and args.admin_only:
        parser.error("--once não pode ser usado com --admin-only")
    configure_logging()
    try:
        config = load_config(force_dry_run=args.dry_run or args.admin_only)
        configure_logging((config.telegram_bot_token, config.mercado_livre_access_token))
        return run(config, once=args.once, admin_only=args.admin_only)
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
