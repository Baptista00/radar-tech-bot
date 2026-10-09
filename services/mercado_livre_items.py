"""Contrato público de anúncio + sale_price; nenhum preço vem do catálogo."""

import re
from urllib.parse import parse_qs, urlsplit, urlunsplit

from models import Product
from safety import clean_text, redact
from services.mercado_livre import (
    DOMAIN_CATEGORIES, _mlb_id, category_for, cents, normal_image_url, normal_product_url,
)
from services.offer_relevance import DEFAULT_NEGATIVE_KEYWORDS, assess_relevance

# Seleção reduz dados recebidos; não solicitamos endereços, contato ou inventário.
ITEM_FIELDS = (
    "id", "site_id", "status", "title", "category_id", "domain_id", "catalog_product_id",
    "permalink", "thumbnail", "pictures", "attributes", "condition", "buying_mode",
    "currency_id", "shipping", "tags", "channels",
)
DIAGNOSTIC_FIELDS = (*ITEM_FIELDS, "amount", "regular_amount", "price", "original_price",
                     "sold_quantity", "available_quantity", "buy_box_winner")
# /items documenta 206 para detalhes parciais. Campos essenciais ainda são validados.
# https://developers.mercadolivre.com.br/pt_br/publicacao-de-produtos
ITEM_SUCCESS_STATUSES = {200, 206}


def bulk_item_id(envelope: object) -> str | None:
    if not isinstance(envelope, dict):
        return None
    if _mlb_id(envelope.get("id")):
        return envelope["id"]
    body = envelope.get("body")
    return body["id"] if isinstance(body, dict) and _mlb_id(body.get("id")) else None


def bulk_item_status(envelope: object) -> int | None:
    if not isinstance(envelope, dict):
        return None
    # Um status_code presente nunca é sobrescrito por code, mesmo se inválido.
    status = envelope.get("status_code", envelope.get("code"))
    return status if type(status) is int and 100 <= status <= 599 else None


def safe_bulk_schema(envelope: object, http_status: int | None) -> tuple[str, ...]:
    root = envelope if isinstance(envelope, dict) else {}
    known = ("id", "status_code", "code", "body", "error", "message", "data", "results")
    keys = [key for key in known if key in root]
    if any(key not in known for key in root):
        keys.append("<outras>")  # Chaves livres também podem carregar credenciais.

    def status_field(name: str) -> str:
        if name not in root:
            return "ausente"
        value = root[name]
        return str(value) if type(value) is int and 100 <= value <= 599 else "invalido:" + type(value).__name__

    return (
        "root_keys=[" + ",".join(keys) + "]",
        "body_present=" + str("body" in root).lower(),
        "body_type=" + (type(root["body"]).__name__ if "body" in root else "ausente"),
        "status_code=" + status_field("status_code"), "code=" + status_field("code"),
        "http_status=" + (str(http_status) if type(http_status) is int else "ausente"),
    )


def listing_data_problem(item: object, item_id: str) -> str | None:
    """Distingue detalhe incompleto de anúncio completo porém inelegível."""
    if not isinstance(item, dict):
        return "body ausente ou não é objeto"
    if item.get("id") != item_id:
        return "ID do detalhe não corresponde ao solicitado"
    for name in ("site_id", "status", "title", "buying_mode"):
        if not isinstance(item.get(name), str) or not item[name].strip():
            return "campo essencial ausente ou inválido: " + name
    attributes = item.get("attributes")
    has_condition = isinstance(attributes, list) and any(
        isinstance(attribute, dict) and attribute.get("id") == "ITEM_CONDITION"
        and isinstance(attribute.get("value_id"), str) and attribute["value_id"]
        for attribute in attributes
    )
    if not has_condition and (not isinstance(item.get("condition"), str) or not item["condition"].strip()):
        return "campo essencial ausente ou inválido: condition"
    if not item_public_url(item.get("permalink"), item_id):
        return "permalink ausente, inválido ou não identifica este item"
    return None


def bulk_item_body(envelope: dict, item_id: str) -> tuple[dict | None, str | None]:
    body = envelope.get("body")
    if isinstance(body, dict):
        body = dict(body)
        # O id raiz é a identidade oficial; não exigir que a projeção o repita.
        if body.get("id") is None:
            body["id"] = item_id
    problem = listing_data_problem(body, item_id)
    return (None, problem) if problem else (body, None)


def safe_fields(payload: object) -> tuple[str, ...]:
    if not isinstance(payload, dict):
        return ("resposta:" + type(payload).__name__,)
    # Só nomes conhecidos + tipos. Nunca valores, chaves livres ou subobjetos.
    return tuple(f"{name}:{type(payload[name]).__name__}" for name in DIAGNOSTIC_FIELDS if name in payload)


def item_public_url(value: object, item_id: str) -> str | None:
    url = normal_product_url(value)
    if not url:
        return None
    parts = urlsplit(url)
    # Permalink oficial da PDP só identifica a oferta se vier com seleção do item.
    query = parse_qs(urlsplit(value).query)
    if re.search(r"/p/MLB[0-9]+(?:/|$)", parts.path):
        if query.get("pdp_filters") == ["item_id:" + item_id]:
            return urlunsplit((parts.scheme, parts.netloc, parts.path, "pdp_filters=item_id%3A" + item_id, ""))
        return None
    direct = re.search(r"/MLB-?([0-9]+)(?:[-/_]|$)", parts.path, re.IGNORECASE)
    if direct:
        return url if "MLB" + direct[1] == item_id else None
    return None


def brand_of(item: dict) -> str | None:
    attributes = item.get("attributes")
    if not isinstance(attributes, list):
        return None
    for attribute in attributes:
        if isinstance(attribute, dict) and attribute.get("id") == "BRAND" and isinstance(attribute.get("value_name"), str):
            return attribute["value_name"].strip() or None
    return None


def image_of(item: dict) -> str | None:
    pictures = item.get("pictures")
    if isinstance(pictures, list):
        for picture in pictures:
            if isinstance(picture, dict):
                url = normal_image_url(picture.get("secure_url")) or normal_image_url(picture.get("url"))
                if url:
                    return url
    return normal_image_url(item.get("thumbnail"))


def has_brand_and_image(item: dict) -> bool:
    return bool(brand_of(item) and image_of(item))


def listing_problem(item: object, *, secrets: tuple[str, ...] = ()) -> str | None:
    if not isinstance(item, dict) or not _mlb_id(item.get("id")):
        return "item_id ausente ou inválido"
    if item.get("site_id") != "MLB":
        return "site_id ausente ou diferente de MLB"
    if item.get("status") != "active":
        return "status ausente ou diferente de active"
    if item.get("buying_mode") != "buy_it_now":
        return "buying_mode ausente ou diferente de buy_it_now"
    title = item.get("title")
    if not isinstance(title, str) or not title.strip():
        return "título ausente"
    condition = item.get("condition")
    attributes = item.get("attributes")
    if isinstance(attributes, list):
        item_condition = next((a for a in attributes if isinstance(a, dict) and a.get("id") == "ITEM_CONDITION"), None)
        if item_condition:
            condition = "new" if item_condition.get("value_id") == "2230284" else "other"
    if condition != "new":
        return "condição nova não confirmada"
    stock = item.get("available_quantity")
    if type(stock) is int and stock <= 0:
        return "estoque informado não positivo"
    tags = item.get("tags")
    if isinstance(tags, list) and any(t in tags for t in ("non_buyable_as_standalone", "test_item")):
        return "item de teste ou não comprável isoladamente"
    channels = item.get("channels")
    if isinstance(channels, list) and "marketplace" not in channels:
        return "canal marketplace indisponível"
    url = item_public_url(item.get("permalink"), item["id"])
    if not url or any(secret and secret in url for secret in secrets):
        return "permalink público ausente, inválido ou não identifica este item"
    return None


def normalize_listing(item: object, sale_price: object, term: str, *, enrichment: dict | None = None,
                      secrets: tuple[str, ...] = (),
                      negative_keywords: tuple[str, ...] = DEFAULT_NEGATIVE_KEYWORDS) -> tuple[Product | None, str]:
    problem = listing_problem(item, secrets=secrets)
    if problem:
        return None, "REJEITADO — " + problem
    if not isinstance(sale_price, dict) or cents(sale_price.get("amount")) is None:
        return None, "REJEITADO — price ausente/inválido em sale_price.amount"
    if sale_price.get("currency_id") != "BRL" or item.get("currency_id", "BRL") != "BRL":
        return None, "REJEITADO — moeda ausente ou diferente de BRL"
    price = cents(sale_price["amount"])
    original = cents(sale_price.get("regular_amount"))
    if original is not None and original <= price:
        original = None
    catalog_id = item.get("catalog_product_id")
    catalog_id = catalog_id if _mlb_id(catalog_id) else None
    enrichment = enrichment if enrichment and enrichment.get("id") == catalog_id else {}
    brand = brand_of(item) or brand_of(enrichment)
    picture = image_of(item) or image_of(enrichment)
    if picture and any(secret and secret in picture for secret in secrets):
        picture = None
    title = clean_text(redact(item["title"], secrets), 180)
    domain = item.get("domain_id")
    domain = domain if isinstance(domain, str) and re.fullmatch(r"MLB-[A-Z_]+", domain) else None
    relevance = assess_relevance(title, term, domain=domain, negative_keywords=negative_keywords)
    shipping = item.get("shipping")
    free = shipping.get("free_shipping") if isinstance(shipping, dict) else None
    product = Product(
        external_id="item:" + item["id"], item_id=item["id"], catalog_product_id=catalog_id,
        title=title, price=price, original_price=original, currency_id="BRL",
        url=item_public_url(item["permalink"], item["id"]), image_url=picture,
        brand=clean_text(redact(brand, secrets), 80) if brand else None,
        category=category_for(title) or DOMAIN_CATEGORIES.get(domain[4:] if domain else ""),
        domain_id=domain, category_id=item.get("category_id") if _mlb_id(item.get("category_id")) else None,
        search_term=clean_text(redact(term, secrets), 80),
        free_shipping=free if type(free) is bool else None,
        # Não converter contagens referenciais/públicas em quantidades exatas.
        sold_quantity=None, available_quantity=None, rating=None, seller_reputation=None,
        relevance_score=relevance.points, quality_penalty=relevance.penalty, price_source="sale_price",
    )
    return product, ("ELEGÍVEL — " if relevance.points else "REJEITADO — ") + relevance.reason
