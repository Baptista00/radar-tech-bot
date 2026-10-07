"""Score normalizado só pelos critérios observados; exige evidência de economia."""

import math
from collections.abc import Sequence

from models import Product, ScoreResult

KNOWN_BRANDS = {
    "logitech", "razer", "hyperx", "corsair", "redragon", "dell", "lg", "samsung",
    "asus", "acer", "lenovo", "hp", "kingston", "crucial", "western digital",
    "sandisk", "intel", "amd", "nvidia", "msi", "gigabyte", "anker", "ugreen",
    "baseus", "apple", "microsoft", "sony", "nintendo", "jbl", "fifine", "elgato",
}
WEIGHTS = {"discount": 40, "history": 35, "rating": 15, "sold": 5, "brand": 5}


def score_offer(product: Product, previous_prices: Sequence[int]) -> ScoreResult:
    criteria: dict[str, float] = {}
    discount = product.discount_percentage
    if discount is not None:
        criteria["discount"] = min(discount / 50, 1.0)
    history = [price for price in previous_prices if price > 0]
    if history:
        lowest = min(history)
        drop = max(0, (lowest - product.price) / lowest)
        criteria["history"] = min(drop / 0.20, 1.0)
    if product.rating is not None:
        criteria["rating"] = max(0, min((product.rating - 3) / 2, 1.0))
    if product.sold_quantity is not None:
        criteria["sold"] = min(math.log1p(max(0, product.sold_quantity)) / math.log1p(1000), 1.0)
    if product.brand:
        criteria["brand"] = float(product.brand.casefold() in KNOWN_BRANDS)
    denominator = sum(WEIGHTS[name] for name in criteria)
    score = round(100 * sum(WEIGHTS[name] * value for name, value in criteria.items()) / denominator) if denominator else 0
    evidence = criteria.get("discount", 0) > 0 or criteria.get("history", 0) > 0
    return ScoreResult(max(0, min(score, 100)), criteria, evidence)
