"""Agendador único: coleta, registra, pontua e simula/publica com limites."""

import logging
import math
import threading
from collections.abc import Callable
from dataclasses import replace
from datetime import datetime, timedelta

from config import Config
from database import Database
from models import CandidateDiagnostic, EvaluatedOffer, Product, SearchReport, timestamp, utcnow
from safety import SafeError
from services.categories import canonical_category
from services.offer_normalizer import normalize_candidate
from services.offer_source import OfferSource, as_batch, source_id, source_name
from services.publisher import OfferPublisher
from services.offer_filter import score_offer
from services.offer_formatter import format_dry_run_report, format_offer, format_search_summary

logger = logging.getLogger(__name__)


class OfferEngine:
    def __init__(self, config: Config, database: Database, source: OfferSource,
                 telegram: OfferPublisher, *, clock: Callable[[], datetime] = utcnow,
                 reporter: Callable[[str], None] | None = None):
        self.config = replace(config, auto_publish=False) if getattr(source, "simulation_only", False) is True else config
        self.database = database
        self.source = source
        self.telegram = telegram
        self.clock = clock
        self.reporter = reporter or logger.info
        self.last_report = SearchReport()
        self._stop = threading.Event()
        self._requested = threading.Event()
        self._search_lock = threading.Lock()
        self._request_guard = threading.Lock()
        self._reply_chat_id: int | None = None
        self._thread: threading.Thread | None = None
        self._start_guard = threading.Lock()
        self._next_due = self.clock()
        saved_due = self.database.get_state("next_search")
        if isinstance(saved_due, str):
            try:
                parsed_due = datetime.fromisoformat(saved_due)
                if parsed_due.tzinfo is not None:
                    self._next_due = parsed_due
            except ValueError:
                pass

    @property
    def source_name(self) -> str:
        return source_name(self.source)

    @property
    def cooldown_key(self) -> str:
        identity = source_id(self.source)
        # Mantém o cooldown persistente do adaptador existente.
        return "meli_retry_not_before" if identity == "mercado_livre" else identity + "_retry_not_before"

    @property
    def online(self) -> bool:
        return bool(self._thread and self._thread.is_alive())

    def start(self) -> None:
        with self._start_guard:
            if self.online:
                return
            if self._stop.is_set():
                self._requested.clear()
                self._reply_chat_id = None
            self._stop.clear()
            self._thread = threading.Thread(target=self._loop, name="radar-search", daemon=True)
            self._thread.start()

    def stop(self) -> None:
        self._stop.set()
        self._requested.set()
        if self._thread:
            self._thread.join(timeout=35)

    def request_search(self, reply_chat_id: int | None = None) -> bool:
        with self._request_guard:
            if self._stop.is_set() or self._search_lock.locked() or self._requested.is_set():
                return False
            self._reply_chat_id = reply_chat_id if reply_chat_id == self.config.telegram_admin_id else None
            self._requested.set()
            return True

    def _loop(self) -> None:
        while not self._stop.is_set():
            try:
                delay = self.scheduler_step()
            except Exception:
                # Não incluir traceback: dependências HTTP podem carregar segredos.
                logger.error("Falha interna no agendador; próxima tentativa no próximo ciclo.")
                self._next_due = self.clock() + timedelta(minutes=self.config.search_interval_minutes)
                delay = 1.0
            # Reavalia pause/resume sem serviço externo e acorda imediatamente para /buscar.
            self._requested.wait(min(1.0, max(0.05, delay)))

    def scheduler_step(self) -> float:
        """Um passo determinístico, também testável com relógio simulado."""
        if self._stop.is_set():
            return 1.0
        manual = self._requested.is_set()
        if self.database.get_state("paused", False) and not manual:
            self.database.set_state("next_search", None)
            return 1.0
        now = self.clock()
        if manual or now >= self._next_due:
            if self.run_search():
                self._next_due = self.clock() + timedelta(minutes=self.config.search_interval_minutes)
        self.database.set_state("next_search", timestamp(self._next_due))
        return max(0.05, (self._next_due - self.clock()).total_seconds())

    def _terms(self) -> tuple[str, ...]:
        terms = self.config.search_terms
        cursor = self.database.get_state("term_cursor", 0) % len(terms)
        count = min(len(terms), self.config.search_terms_per_cycle)
        selected = tuple(terms[(cursor + index) % len(terms)] for index in range(count))
        self.database.set_state("term_cursor", (cursor + count) % len(terms))
        return selected

    def _collect(self, report: SearchReport) -> list[Product]:
        found: dict[tuple[str, str], Product] = {}
        item_keys: dict[tuple[str, str], tuple[str, str]] = {}
        not_before = self.database.get_state(self.cooldown_key)
        if not_before and self.clock() < datetime.fromisoformat(not_before):
            remaining = math.ceil((datetime.fromisoformat(not_before) - self.clock()).total_seconds())
            # Não estender o prazo por novas execuções durante o mesmo bloqueio.
            self._search_error(report, SafeError(f"{self.source_name}: aguardando backoff de consultas ({remaining} segundos restantes).", status=429))
            return []
        self.source.begin_cycle(self._stop)
        # Um ciclo por vez (_search_lock), termos e detalhes sempre sequenciais.
        for index, term in enumerate(self._terms()):
            if self._stop.is_set() or (index and self._stop.wait(self.config.request_interval_seconds)):
                break
            try:
                report.terms_searched += 1
                batch = as_batch(self.source.search(term, min(10, self.config.search_limit_per_term)))
            except SafeError as error:
                self._search_error(report, error)
                break
            report.found_ids.update(batch.found_ids)
            report.diagnostics.extend(batch.diagnostics)
            report.discovery = batch.discovery or report.discovery
            for candidate in batch.candidates:
                product, reason = normalize_candidate(candidate, term, negative_keywords=self.config.negative_keywords)
                if product is None:
                    report.diagnostics.append(CandidateDiagnostic("fixture" if source_id(self.source) == "fixture" else "fonte",
                        None, None, (), "REJEITADO — " + reason))
                    continue
                if canonical_category(product.category) not in self.config.active_categories:
                    product = replace(product, relevance_score=0)
                    reason = "categoria desativada"
                if not isinstance(candidate, Product):
                    report.diagnostics.append(CandidateDiagnostic("fonte local", None, product.external_id, (),
                        ("ELEGÍVEL — " if product.relevance_score else "REJEITADO — ") + reason))
                identity = product.item_id or product.external_id
                report.valid_ids.add(identity)
                if product.relevance_score != 0:
                    report.relevant_ids.add(identity)
                key = (product.source, product.external_id)
                item_key = (product.source, identity)
                existing_key = key if key in found else item_keys.get(item_key)
                if existing_key:
                    previous = found[existing_key]
                    if (product.relevance_score or 0) > (previous.relevance_score or 0):
                        found[existing_key] = product
                    continue
                found[key] = product
                item_keys[item_key] = key
            for error in batch.errors:
                self._search_error(report, error)
            if batch.stop:
                break
        return list(found.values())

    def _search_error(self, report: SearchReport, error: SafeError) -> None:
        report.errors.append(str(error))
        self._record_error(error)
        if error.retry_after:
            self.database.set_state(self.cooldown_key, timestamp(self.clock() + timedelta(seconds=error.retry_after)))

    def run_search(self) -> bool:
        if not self._search_lock.acquire(blocking=False):
            return False
        with self._request_guard:
            reply_chat_id, self._reply_chat_id = self._reply_chat_id, None
            self._requested.clear()
        run_id = None
        analyzed = approved = published = simulated = 0
        state = "completed"
        report = SearchReport()
        self.last_report = report
        logger.info("Início de busca.")
        try:
            run_id = self.database.start_search(self.clock())
            products = self._collect(report)
            candidates: list[tuple[int, EvaluatedOffer]] = []
            for product in products:
                observed_at = self.clock()
                product_id, history = self.database.observe(product, observed_at, self.config.history_days)
                result = score_offer(product, history, known_brands=self.config.known_brands,
                                     recently_published=self.database.recently_published(
                                         product_id, self.clock(), self.config.republish_after_hours))
                if self.database.local_lowest_claim(product_id, product.price, observed_at, self.config.history_days):
                    result = replace(result, history_claim_days=self.config.history_days)
                accepted = result.approved(self.config.min_offer_score)
                self.database.save_evaluation(run_id, product_id, result, accepted)
                analyzed += 1
                decision = "não seria publicada: score abaixo do threshold" if result.score < self.config.min_offer_score else "não seria publicada: sem evidência de economia"
                evaluation = EvaluatedOffer(product, result, decision)
                if product.relevance_score != 0:
                    report.offers.append(evaluation)
                if accepted:
                    approved += 1
                    candidates.append((product_id, evaluation))
            logger.info("Busca encontrou %d produtos válidos; %d ofertas aprovadas.", analyzed, approved)
            if report.errors:
                state = "partial" if products else "failed"
            candidates.sort(key=lambda candidate: (-candidate[1].result.score, candidate[1].product.price))
            for product_id, evaluation in candidates:
                product, result = evaluation.product, evaluation.result
                if self._stop.is_set():
                    evaluation.decision = "não seria publicada: busca interrompida"
                    break
                reservation, reason = self.database.reserve(
                    product_id, product, result.score, self.clock(), self.config,
                    simulate=not self.config.auto_publish or product.source == "fixture",
                )
                if reservation is None:
                    evaluation.decision = "não seria publicada: " + reason
                    logger.info("Oferta %s retida: %s.", product.external_id, reason)
                    continue
                text = format_offer(product, result)
                if not self.config.auto_publish or product.source == "fixture":
                    simulated += 1
                    evaluation.decision = "SERIA PUBLICADA (simulação registrada)"
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
            self._search_error(report, SafeError("Falha interna na busca; detalhes sensíveis omitidos."))
        finally:
            try:
                if run_id is not None:
                    self.database.finish_search(run_id, self.clock(), state, analyzed, approved, published, simulated,
                                                terms_searched=report.terms_searched, found=len(report.found_ids), errors=len(report.errors))
                report.status, report.simulated = state, simulated
                logger.info("Fim de busca (%s): %d analisados, %d aprovados, %d publicados, %d simulados.",
                            state, analyzed, approved, published, simulated)
                if not self.config.auto_publish:
                    self.reporter(format_dry_run_report(report, self.config.min_offer_score))
                if reply_chat_id is not None:
                    try:
                        self.telegram.reply(reply_chat_id, format_search_summary(
                            report, self.config.min_offer_score, auto_publish=self.config.auto_publish))
                    except Exception:
                        self._record_error(SafeError("Não foi possível entregar o resumo privado da busca; sem reenvio automático."))
            finally:
                self._search_lock.release()
        return True

    def _record_error(self, error: SafeError) -> None:
        logger.error("%s", error)
        self.database.add_error(str(error), self.clock())
