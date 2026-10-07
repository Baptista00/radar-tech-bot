"""Agendador único: coleta, registra, pontua e simula/publica com limites."""

import logging
import threading
from collections.abc import Callable
from datetime import datetime, timedelta

from config import Config
from database import Database
from models import Product, ScoreResult, timestamp, utcnow
from safety import SafeError
from services.mercado_livre import ProductSource
from services.offer_filter import score_offer
from services.offer_formatter import format_offer
from telegram_service import TelegramService

logger = logging.getLogger(__name__)


class OfferEngine:
    def __init__(self, config: Config, database: Database, source: ProductSource,
                 telegram: TelegramService, *, clock: Callable[[], datetime] = utcnow):
        self.config = config
        self.database = database
        self.source = source
        self.telegram = telegram
        self.clock = clock
        self._stop = threading.Event()
        self._requested = threading.Event()
        self._search_lock = threading.Lock()
        self._thread: threading.Thread | None = None

    @property
    def online(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        self._thread = threading.Thread(target=self._loop, name="radar-search", daemon=True)
        self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._requested.set()
        if self._thread:
            self._thread.join(timeout=35)

    def request_search(self) -> bool:
        if self._search_lock.locked() or self._requested.is_set():
            return False
        self._requested.set()
        return True

    def _loop(self) -> None:
        due = self.clock()
        while not self._stop.is_set():
            try:
                self.database.set_state("next_search", timestamp(due))
                self._requested.wait(max(0, (due - self.clock()).total_seconds()))
                self._requested.clear()
                if self._stop.is_set():
                    break
                self.run_search()
            except Exception:
                # Não incluir traceback: dependências HTTP podem carregar segredos.
                logger.error("Falha interna no agendador; próxima tentativa no próximo ciclo.")
            due = self.clock() + timedelta(minutes=self.config.search_interval_minutes)

    def _terms(self) -> tuple[str, ...]:
        terms = self.config.search_terms
        cursor = self.database.get_state("term_cursor", 0) % len(terms)
        count = min(len(terms), self.config.search_terms_per_cycle)
        selected = tuple(terms[(cursor + index) % len(terms)] for index in range(count))
        self.database.set_state("term_cursor", (cursor + count) % len(terms))
        return selected

    def _collect(self) -> tuple[list[Product], SafeError | None]:
        found: dict[tuple[str, str], Product] = {}
        for index, term in enumerate(self._terms()):
            if self._stop.is_set() or (index and self._stop.wait(self.config.request_interval_seconds)):
                break
            try:
                products = self.source.search(term, self.config.search_limit_per_term)
            except SafeError as error:
                return list(found.values()), error
            for product in products:
                found.setdefault((product.source, product.external_id), product)
        return list(found.values()), None

    def run_search(self) -> bool:
        if not self._search_lock.acquire(blocking=False):
            return False
        run_id = None
        analyzed = approved = published = simulated = 0
        state = "completed"
        logger.info("Início de busca.")
        try:
            run_id = self.database.start_search(self.clock())
            products, source_error = self._collect()
            candidates: list[tuple[int, Product, ScoreResult]] = []
            for product in products:
                product_id, history = self.database.observe(product, self.clock(), self.config.history_days)
                result = score_offer(product, history)
                accepted = result.approved(self.config.min_offer_score)
                self.database.save_evaluation(run_id, product_id, result, accepted)
                analyzed += 1
                if accepted:
                    approved += 1
                    candidates.append((product_id, product, result))
            logger.info("Busca encontrou %d produtos válidos; %d ofertas aprovadas.", analyzed, approved)
            if source_error:
                state = "partial" if products else "failed"
                self._record_error(source_error)
            candidates.sort(key=lambda candidate: (-candidate[2].score, candidate[1].price))
            for product_id, product, result in candidates:
                if self._stop.is_set():
                    break
                reservation, reason = self.database.reserve(
                    product_id, product, result.score, self.clock(), self.config,
                    simulate=not self.config.auto_publish,
                )
                if reservation is None:
                    logger.info("Oferta %s retida: %s.", product.external_id, reason)
                    continue
                text = format_offer(product, result)
                if not self.config.auto_publish:
                    simulated += 1
                    logger.info("SIMULAÇÃO — seria publicada:\n%s", text)
                    continue
                try:
                    message_id = self.telegram.publish_offer(text)
                    self.database.confirm_publication(reservation, message_id, self.clock())
                    published += 1
                    logger.info("Publicação confirmada: produto %s; mensagem %d.", product.external_id, message_id)
                except SafeError as error:
                    self.database.fail_publication(reservation, error, self.clock())
                    self._record_error(error)
                    state = "partial"
                    break  # Nenhum retry automático de sendMessage.
                except Exception:
                    error = SafeError("Falha ao registrar confirmação; conferir canal antes de reenviar.", uncertain=True)
                    self.database.fail_publication(reservation, error, self.clock())
                    raise
        except Exception:
            state = "failed"
            self._record_error(SafeError("Falha interna na busca; detalhes sensíveis omitidos."))
        finally:
            try:
                if run_id is not None:
                    self.database.finish_search(run_id, self.clock(), state, analyzed, approved, published, simulated)
                logger.info("Fim de busca (%s): %d analisados, %d aprovados, %d publicados, %d simulados.",
                            state, analyzed, approved, published, simulated)
            finally:
                self._search_lock.release()
        return True

    def _record_error(self, error: SafeError) -> None:
        logger.error("%s", error)
        self.database.add_error(str(error), self.clock())
