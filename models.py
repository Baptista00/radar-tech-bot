"""Valores de preço são inteiros em centavos; dados ausentes continuam None."""

from dataclasses import dataclass, field
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class OfferCandidate:
    """Contrato entre fontes e núcleo. Preços em centavos; ausências explícitas."""
    external_id: str
    title: str
    price: int | None
    url: str
    source: str = "mercado_livre"
    original_price: int | None = None
    rating: float | None = None
    sold_quantity: int | None = None
    image_url: str | None = None
    brand: str | None = None
    category: str | None = None
    search_term: str | None = None
    product_id: str | None = None
    item_id: str | None = None
    domain_id: str | None = None
    currency_id: str | None = None
    available_quantity: int | None = None
    free_shipping: bool | None = None
    seller_reputation: str | None = None
    catalog_product_id: str | None = None
    category_id: str | None = None
    relevance_score: int | None = None
    quality_penalty: int = 0
    price_source: str | None = None


@dataclass(frozen=True)
class Product(OfferCandidate):
    """Candidato normalizado, com preço válido e identidade da fonte."""
    price: int

    @property
    def discount_percentage(self) -> float | None:
        if self.original_price is None or self.original_price <= self.price:
            return None
        return round((self.original_price - self.price) * 100 / self.original_price, 2)


@dataclass(frozen=True)
class ScoreResult:
    score: int
    criteria: dict[str, float]
    has_saving_evidence: bool
    history_claim_days: int | None = None

    @property
    def band(self) -> str:
        return score_band(self.score)

    def approved(self, threshold: int) -> bool:
        return self.has_saving_evidence and self.score >= threshold


def score_band(score: int) -> str:
    for minimum, label in ((85, "Oferta excelente"), (70, "Boa oferta"),
                           (60, "Interessante"), (40, "Regular")):
        if score >= minimum:
            return label
    return "Fraco"


@dataclass
class EvaluatedOffer:
    product: Product
    result: ScoreResult
    decision: str


@dataclass(frozen=True)
class CandidateDiagnostic:
    endpoint: str
    http_status: int | None
    item_id: str | None
    fields: tuple[str, ...]
    reason: str


@dataclass
class SearchReport:
    terms_searched: int = 0
    found_ids: set[str] = field(default_factory=set)
    offers: list[EvaluatedOffer] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    simulated: int = 0
    status: str = "completed"
    valid_ids: set[str] = field(default_factory=set)
    relevant_ids: set[str] = field(default_factory=set)
    diagnostics: list[CandidateDiagnostic] = field(default_factory=list)
    discovery: str = ""
