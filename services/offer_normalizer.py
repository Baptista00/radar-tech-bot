"""Normalização comum de candidatos; nenhum acesso a fontes ou credenciais."""

import re
from dataclasses import asdict
from urllib.parse import urlsplit, urlunsplit

from models import OfferCandidate, Product
from safety import clean_text
from services.categories import category_for
from services.offer_relevance import DEFAULT_NEGATIVE_KEYWORDS, assess_relevance


def normalize_candidate(candidate: OfferCandidate, term: str, *,
                        negative_keywords=DEFAULT_NEGATIVE_KEYWORDS) -> tuple[Product | None, str]:
    # O adaptador legado já faz normalização estrita e cálculo de relevância.
    if isinstance(candidate, Product):
        return candidate, "normalizado pela fonte existente"
    if not isinstance(candidate, OfferCandidate):
        return None, "candidato inválido"
    if not isinstance(candidate.external_id, str) or not re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", candidate.external_id):
        return None, "identidade inválida"
    if not isinstance(candidate.source, str) or not re.fullmatch(r"[a-z_]{1,40}", candidate.source):
        return None, "fonte inválida"
    if not isinstance(candidate.title, str) or not candidate.title.strip():
        return None, "título ausente"
    if type(candidate.price) is not int or not 0 < candidate.price < 10_000_000_000:
        return None, "preço ausente ou inválido"
    if candidate.currency_id != "BRL":
        return None, "moeda ausente ou inválida"
    try:
        link = urlsplit(candidate.url)
        if (link.scheme != "https" or not link.hostname or link.username or link.password
                or link.port not in {None, 443}):
            return None, "link público inválido"
        if candidate.source == "fixture" and (link.hostname != "example.com" or not candidate.external_id.startswith("FIXTURE-")):
            return None, "fixture exige identidade fictícia e domínio example.com"
        url = urlunsplit(("https", link.hostname, link.path, "", ""))
    except (TypeError, ValueError, AttributeError):
        return None, "link público inválido"
    title = clean_text(candidate.title, 180)
    relevance = assess_relevance(title, term, negative_keywords=negative_keywords)
    original = candidate.original_price
    original = original if type(original) is int and candidate.price < original < 10_000_000_000 else None
    def identifier(value):
        return value if isinstance(value, str) and re.fullmatch(r"[A-Za-z0-9:_-]{1,100}", value) else None

    values = asdict(candidate)
    values.update(title=title, url=url, original_price=original, category=category_for(title),
                  search_term=clean_text(term, 80), relevance_score=relevance.points,
                  quality_penalty=relevance.penalty,
                  brand=clean_text(candidate.brand, 80) if isinstance(candidate.brand, str) else None,
                  free_shipping=candidate.free_shipping if type(candidate.free_shipping) is bool else None,
                  sold_quantity=candidate.sold_quantity if type(candidate.sold_quantity) is int and candidate.sold_quantity >= 0 else None,
                  rating=candidate.rating if type(candidate.rating) in {int, float} and 0 <= candidate.rating <= 5 else None,
                  seller_reputation=candidate.seller_reputation if isinstance(candidate.seller_reputation, str) and candidate.seller_reputation in {"GREEN", "YELLOW", "RED"} else None,
                  item_id=identifier(candidate.item_id), product_id=identifier(candidate.product_id),
                  catalog_product_id=identifier(candidate.catalog_product_id), category_id=identifier(candidate.category_id),
                  domain_id=identifier(candidate.domain_id), price_source=identifier(candidate.price_source),
                  available_quantity=candidate.available_quantity if type(candidate.available_quantity) is int and candidate.available_quantity >= 0 else None,
                  image_url=None)
    return Product(**values), relevance.reason
