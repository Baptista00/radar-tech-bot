"""Contrato neutro das fontes e ponte para o adaptador existente, sem I/O."""

import threading
from dataclasses import dataclass, field
from typing import Protocol, runtime_checkable

from models import CandidateDiagnostic, OfferCandidate
from safety import SafeError

SOURCE_NAMES = {"mercado_livre": "Mercado Livre", "fixture": "Fixture",
                "shopee": "Shopee", "amazon": "Amazon"}


@dataclass
class OfferBatch:
    candidates: list[OfferCandidate] = field(default_factory=list)
    found_ids: set[str] = field(default_factory=set)
    errors: list[SafeError] = field(default_factory=list)
    stop: bool = False
    diagnostics: list[CandidateDiagnostic] = field(default_factory=list)
    discovery: str = ""


@runtime_checkable
class OfferSource(Protocol):
    source_id: str
    simulation_only: bool

    def begin_cycle(self, stop_event: threading.Event | None = None) -> None: ...

    def search(self, term: str, limit: int) -> OfferBatch: ...


def as_batch(result) -> OfferBatch:
    if isinstance(result, OfferBatch):
        return result
    # Compatibilidade com SearchBatch já entregue pelo adaptador antigo.
    if isinstance(getattr(result, "products", None), list):
        return OfferBatch(result.products, result.found_ids, result.errors,
                          result.stop, result.diagnostics, result.discovery)
    raise SafeError("Fonte: resposta incompatível com o contrato de ofertas.")


def source_id(source) -> str:
    value = getattr(source, "source_id", None)
    # Adaptadores anteriores não possuem metadados; a ponte preserva seu estado.
    return value if isinstance(value, str) else "mercado_livre"


def source_name(source) -> str:
    return SOURCE_NAMES.get(source_id(source), "Fonte personalizada")


class MercadoLivreAdapter:
    """Adapta o contrato, sem modificar transporte, OAuth, parser ou erros."""
    source_id = "mercado_livre"
    simulation_only = False

    def __init__(self, source):
        self.source = source

    def begin_cycle(self, stop_event=None):
        self.source.begin_cycle(stop_event)

    def search(self, term: str, limit: int) -> OfferBatch:
        return as_batch(self.source.search(term, limit))


class PendingSource:
    simulation_only = True
    source_id = "pending"

    def begin_cycle(self, stop_event=None):
        pass

    def search(self, term: str, limit: int) -> OfferBatch:
        raise SafeError("Fonte aguardando habilitação e implementação oficial; nenhuma consulta executada.")


class ShopeeSource(PendingSource):
    source_id = "shopee"


class AmazonSource(PendingSource):
    source_id = "amazon"
