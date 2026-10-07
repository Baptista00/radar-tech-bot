"""Valores de preço são inteiros em centavos; dados ausentes continuam None."""

from dataclasses import dataclass
from datetime import datetime, timezone


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def timestamp(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="microseconds")


@dataclass(frozen=True)
class Product:
    external_id: str
    title: str
    price: int
    url: str
    source: str = "mercado_livre"
    original_price: int | None = None
    rating: float | None = None
    sold_quantity: int | None = None
    image_url: str | None = None
    brand: str | None = None
    category: str | None = None
    search_term: str | None = None

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

    def approved(self, threshold: int) -> bool:
        return self.has_saving_evidence and self.score >= threshold
