"""Score aditivo: dados ausentes não ganham pontos nem mudam o denominador."""

from collections.abc import Sequence

from config import DEFAULT_KNOWN_BRANDS
from models import Product, ScoreResult

MIN_HISTORY_OBSERVATIONS = 3


def _points(value: float, levels: tuple[tuple[float, int], ...]) -> int:
    return next((points for minimum, points in levels if value >= minimum), 0)


def score_offer(product: Product, previous_prices: Sequence[int], *,
                known_brands: Sequence[str] = DEFAULT_KNOWN_BRANDS,
                recently_published: bool = False) -> ScoreResult:
    criteria: dict[str, float] = {}
    discount = product.discount_percentage
    if discount is not None:
        criteria["discount"] = _points(discount, ((40, 35), (30, 30), (20, 22), (10, 12)))
    history = [price for price in previous_prices if type(price) is int and price > 0]
    drop = 0.0
    if len(history) >= MIN_HISTORY_OBSERVATIONS:
        lowest = min(history)
        drop = max(0, (lowest - product.price) * 100 / lowest)
        criteria["history"] = _points(drop, ((20, 25), (10, 20), (5, 10)))
        if product.price < lowest * 0.1:
            criteria["price_inconsistency"] = -25
    if product.free_shipping is not None:
        criteria["free_shipping"] = 10 if product.free_shipping else 0
    if product.seller_reputation is not None:
        criteria["reputation"] = 10 if product.seller_reputation.upper() == "GREEN" else 0
    if product.sold_quantity is not None:
        criteria["sold"] = _points(product.sold_quantity, ((1000, 10), (500, 8), (100, 6), (50, 4), (10, 2)))
    if product.brand:
        criteria["brand"] = 5 if product.brand.casefold() in {brand.casefold() for brand in known_brands} else 0
    if product.relevance_score is not None:
        criteria["relevance"] = min(10, max(0, product.relevance_score))
    if product.quality_penalty:
        criteria["suspicious_title"] = -min(20, max(0, product.quality_penalty))
    if recently_published:
        criteria["recent_publication"] = -10
    # Não pontuar estrelas inferidas, dados restritos ou campos ausentes.
    evidence = (discount is not None and discount > 0) or drop > 0
    if product.relevance_score == 0:
        evidence = False
    return ScoreResult(min(100, max(0, round(sum(criteria.values())))), criteria, evidence)
