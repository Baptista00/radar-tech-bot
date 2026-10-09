"""Ofertas oficiais por item; transporte sequencial e catálogo opcional."""

import logging
import math
import random
import re
import threading
import time
import unicodedata
from dataclasses import dataclass, field, replace
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from email.utils import parsedate_to_datetime
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from models import CandidateDiagnostic, Product
from safety import SafeError, clean_text, redact
from services.mercado_livre_auth import get_valid_access_token
from services.mercado_livre_http import API_TIMEOUT, create_meli_session

API_PRODUCTS = "https://api.mercadolibre.com/products"
API_SEARCH = API_PRODUCTS + "/search"
API_ITEMS = "https://api.mercadolibre.com/items"
MAX_ATTEMPTS = 3
BACKOFF_BASE_SECONDS = 5.0
RATE_LIMIT_HEADERS = (
    "RateLimit-Limit", "RateLimit-Remaining", "RateLimit-Reset",
    "X-RateLimit-Limit", "X-RateLimit-Remaining", "X-RateLimit-Reset",
    "X-Rate-Limit-Limit", "X-Rate-Limit-Remaining", "X-Rate-Limit-Reset",
)
logger = logging.getLogger(__name__)
PRODUCT_HOSTS = {"produto.mercadolivre.com.br", "www.mercadolivre.com.br", "mercadolivre.com.br"}
TECH_PATTERNS = (
    ("mousepad", r"\bmouse\s*pad\b"),
    ("mouse", r"\bmouse\b"),
    ("teclado", r"\b(teclado|keyboard)\b"),
    ("headset", r"\b(headset|headphone|fone.*gamer)\b"),
    ("monitor", r"\bmonitor\b"),
    ("notebook", r"\b(notebook|laptop|macbook)\b"),
    ("SSD", r"\b(ssd|nvme)\b"),
    ("memória RAM", r"\b(ram|ddr[345])\b"),
    ("placa de vídeo", r"\b(placa de video|geforce|radeon|rtx|gtx)\b"),
    ("processador", r"\b(processador|ryzen|intel core)\b"),
    ("controle", r"\b(joystick|gamepad|controle.*(?:gamer|xbox|playstation|ps[45]|switch|bluetooth))\b"),
    ("microfone", r"\b(microfone|microphone)\b"),
    ("webcam", r"\bwebcam\b"),
    ("carregador", r"\bcarregador\b"),
    ("hub", r"\b(hub.*(?:usb|tipo c)|dock.*(?:usb|notebook))\b"),
    ("power bank", r"\b(power\s*bank|bateria portatil)\b"),
    ("acessório para setup", r"\b(braco articulado|suporte.*(?:monitor|notebook|headset)|light bar|stream deck|fita led)\b"),
    ("gadget", r"\b(smart plug|tomada inteligente|rastreador bluetooth|adaptador.*(?:usb|bluetooth|hdmi))\b"),
)


@dataclass
class SearchBatch:
    products: list[Product] = field(default_factory=list)
    found_ids: set[str] = field(default_factory=set)
    errors: list[SafeError] = field(default_factory=list)
    stop: bool = False
    diagnostics: list[CandidateDiagnostic] = field(default_factory=list)
    discovery: str = ""


class ProductSource(Protocol):
    def begin_cycle(self, stop_event: threading.Event | None = None) -> None: ...

    def search(self, term: str, limit: int) -> SearchBatch: ...


def category_for(title: str) -> str | None:
    folded = unicodedata.normalize("NFKD", title).encode("ascii", "ignore").decode().lower()
    if re.search(r"\b(teclado musical|piano|orgao musical)\b", folded):
        return None
    for category, pattern in TECH_PATTERNS:
        if re.search(pattern, folded):
            return category
    return None


def cents(value: Any) -> int | None:
    if isinstance(value, bool) or not isinstance(value, (str, int, float)):
        return None
    try:
        number = Decimal(str(value))
        if not number.is_finite() or not 0 < number < 100_000_000:
            return None
        result = int((number * 100).quantize(Decimal("1"), rounding=ROUND_HALF_UP))
        return result if result > 0 else None
    except (InvalidOperation, ValueError):
        return None


def normal_product_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if parts.scheme not in {"http", "https"} or parts.hostname not in PRODUCT_HOSTS:
            return None
        if parts.username or parts.password or parts.port not in {None, 80, 443}:
            return None
        if not re.search(r"(?:/MLB-?\d+|/p/MLB\d+)", parts.path, re.IGNORECASE):
            return None
        # Descarta rastreamento, fragments e quaisquer parâmetros de afiliado.
        return urlunsplit(("https", parts.hostname, parts.path, "", ""))
    except ValueError:
        return None


def normal_image_url(value: Any) -> str | None:
    if not isinstance(value, str):
        return None
    try:
        parts = urlsplit(value)
        if (parts.scheme not in {"http", "https"} or not parts.hostname
                or not parts.hostname.endswith(".mlstatic.com") or parts.username or parts.password
                or parts.port not in {None, 80, 443}):
            return None
        return urlunsplit(("https", parts.hostname, parts.path, "", ""))
    except ValueError:
        return None


def normalize_item(item: Any, term: str, *, category_hint: str | None = None) -> Product | None:
    if not isinstance(item, dict) or item.get("currency_id") != "BRL":
        return None
    if item.get("status", "active") != "active" or item.get("condition", "new") != "new":
        return None
    if item.get("available_quantity") == 0:
        return None
    external_id, title = item.get("id"), item.get("title")
    if not isinstance(external_id, str) or not re.fullmatch(r"MLB\d+", external_id):
        return None
    if not isinstance(title, str) or not title.strip():
        return None
    price, url = cents(item.get("price")), normal_product_url(item.get("permalink"))
    category = category_for(title) or category_hint
    if price is None or url is None or category is None:
        return None
    original = cents(item.get("original_price"))
    if original is not None and original <= price:
        original = None
    reviews = item.get("reviews")
    rating = reviews.get("rating_average") if isinstance(reviews, dict) else None
    if isinstance(rating, bool) or not isinstance(rating, (int, float)) or not math.isfinite(rating) or not 0 <= rating <= 5:
        rating = None
    sold = item.get("sold_quantity")
    if type(sold) is not int or sold < 0:
        sold = None
    attributes = item.get("attributes")
    brand = None
    if isinstance(attributes, list):
        for attribute in attributes:
            if isinstance(attribute, dict) and attribute.get("id") == "BRAND" and isinstance(attribute.get("value_name"), str):
                brand = clean_text(attribute["value_name"], 80) or None
                break
    image = normal_image_url(item.get("thumbnail"))
    return Product(
        external_id=external_id, title=clean_text(title, 180), price=price,
        original_price=original, url=url, rating=rating, sold_quantity=sold,
        image_url=image, brand=brand, category=category, search_term=clean_text(term, 80),
    )


DOMAIN_CATEGORIES = {
    "COMPUTER_MICE": "mouse", "MICE": "mouse", "KEYBOARDS": "teclado",
    "HEADPHONES": "headset", "HEADSETS": "headset", "MOUSE_PADS": "mousepad",
    "MONITORS": "monitor", "NOTEBOOKS": "notebook", "SOLID_STATE_DRIVES": "SSD",
    "RAM_MEMORIES": "memória RAM", "VIDEO_CARDS": "placa de vídeo", "PROCESSORS": "processador",
    "MICROPHONES": "microfone", "WEBCAMS": "webcam", "GAMEPADS": "controle",
}


def _mlb_id(value: Any) -> bool:
    return isinstance(value, str) and re.fullmatch(r"MLB\d{1,20}", value) is not None


def _quantity(value: Any) -> int | None:
    return value if type(value) is int and value >= 0 else None


def normalize_product(detail: Any, term: str, *, secrets: tuple[str, ...] = ()) -> Product | None:
    """Só a oferta vencedora fornece preço/vendas; nunca o agregado do catálogo."""
    if not isinstance(detail, dict) or detail.get("status") != "active" or not _mlb_id(detail.get("id")):
        return None
    winner = detail.get("buy_box_winner")
    if not isinstance(winner, dict) or not _mlb_id(winner.get("item_id")):
        return None
    product_id = detail["id"]
    if detail.get("site_id", "MLB") != "MLB" or winner.get("site_id", "MLB") != "MLB":
        return None
    if winner.get("product_id", product_id) != product_id:
        return None
    stock = winner.get("available_quantity")
    if type(stock) is int and stock <= 0:
        return None
    domain = detail.get("domain_id")
    if not isinstance(domain, str) or not re.fullmatch(r"MLB-[A-Z_]+", domain):
        domain = None
    title = detail.get("name")
    if not isinstance(title, str):
        return None
    link = normal_product_url(winner.get("permalink")) or normal_product_url(detail.get("permalink"))
    if link is None or any(secret and secret in link for secret in secrets):
        return None
    pictures = detail.get("pictures")
    picture = pictures[0].get("url") if isinstance(pictures, list) and pictures and isinstance(pictures[0], dict) else None
    item = {
        "id": winner["item_id"], "title": redact(title, secrets), "price": winner.get("price"),
        "original_price": winner.get("original_price"), "currency_id": winner.get("currency_id"),
        "sold_quantity": winner.get("sold_quantity"), "available_quantity": stock,
        "condition": winner.get("condition", "new"), "status": winner.get("status", "active"),
        "permalink": link, "thumbnail": picture, "attributes": detail.get("attributes"),
    }
    product = normalize_item(item, term, category_hint=DOMAIN_CATEGORIES.get(domain[4:]) if domain else None)
    if product is None:
        return None
    shipping, seller = winner.get("shipping"), winner.get("seller")
    free_shipping = shipping.get("free_shipping") if isinstance(shipping, dict) else None
    reputation = seller.get("reputation_level_id") if isinstance(seller, dict) else None
    reputation = clean_text(redact(reputation, secrets), 40).upper() if isinstance(reputation, str) else None
    return replace(
        product, external_id="catalog:" + product_id, product_id=product_id, item_id=winner["item_id"],
        domain_id=domain, currency_id="BRL", available_quantity=_quantity(stock), rating=None,
        original_price=cents(winner.get("original_price")),
        brand=clean_text(redact(product.brand, secrets), 80) if product.brand else None,
        image_url=None if picture and any(secret and secret in str(picture) for secret in secrets) else product.image_url,
        free_shipping=free_shipping if type(free_shipping) is bool else None,
        seller_reputation=reputation or None,
    )


def retry_after_seconds(value: str | None, now: float) -> float | None:
    """Retry-After aceita segundos ou HTTP-date; nunca devolve o header bruto."""
    if not isinstance(value, str):
        return None
    value = value.strip()
    try:
        if re.fullmatch(r"[0-9]+", value):
            seconds = float(value)
        else:
            date = parsedate_to_datetime(value)
            if date.tzinfo is None:
                return None
            seconds = max(0.0, date.timestamp() - now)
        return seconds if math.isfinite(seconds) else None
    except (TypeError, ValueError, OverflowError):
        return None


def safe_endpoint(path: str) -> str:
    if re.fullmatch(r"/(?:products/(?:search|MLB[0-9]+)|items/(?:bulk|MLB[0-9]+(?:/sale_price)?))", path):
        return path
    return "/recurso-oficial"


class CatalogSource:
    """Adaptador anterior preservado para regressão; não usado pelo aplicativo."""
    retry_server_errors = False

    def __init__(self, *, request_interval_seconds: float = 1.0):
        if not math.isfinite(request_interval_seconds) or not 1 <= request_interval_seconds <= 60:
            raise ValueError("Intervalo Mercado Livre precisa ficar entre 1 e 60 segundos.")
        self.interval = request_interval_seconds
        self._next_request_at = 0.0
        self._retry_not_before = 0.0
        self.begin_cycle()

    def begin_cycle(self, stop_event: threading.Event | None = None) -> None:
        self._stop_event = stop_event
        self._cache: dict[str, Product | None] = {}
        self._refreshed = False
        self._tokens: set[str] = set()
        self._statuses: dict[str, int] = {}

    def _interrupted(self) -> SafeError:
        remaining = max(0, math.ceil(self._retry_not_before - time.time()))
        return SafeError("Mercado Livre: busca interrompida.", retry_after=remaining)

    def _wait(self, delay: float) -> None:
        # Esperas canceláveis, inclusive Retry-After longo, sem exceder o limite
        # de Event.wait do Windows. Nenhuma nova requisição durante a espera.
        try:
            while delay > 0:
                step = min(delay, 60.0)
                if self._stop_event:
                    if self._stop_event.wait(step):
                        raise self._interrupted()
                else:
                    time.sleep(step)
                delay -= step
        except KeyboardInterrupt:
            if self._stop_event:
                self._stop_event.set()
            raise self._interrupted() from None

    def _pace(self) -> None:
        if self._stop_event and self._stop_event.is_set():
            raise self._interrupted()
        self._wait(max(0.0, self._next_request_at - time.monotonic()))

    def _rate_limit(self, response: requests.Response, url: str, attempt: int,
                    max_attempts: int = MAX_ATTEMPTS) -> float:
        retry_after = retry_after_seconds(response.headers.get("Retry-After"), time.time())
        backoff = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
        delay = max(retry_after or 0, backoff) + random.uniform(0, backoff * 0.25)
        if attempt == max_attempts:
            delay = max(60.0, delay)  # Também protege a próxima CLI ou /buscar.
        self._retry_not_before = time.time() + delay
        # Apenas caminhos conhecidos, sem query string, URLs ou headers privados.
        path = urlsplit(url).path
        endpoint = safe_endpoint(path)
        logger.warning("Mercado Livre retornou 429. Endpoint: %s. Tentativa: %d/%d.",
                       endpoint, attempt, max_attempts)
        if retry_after is not None:
            logger.warning("Retry-After: %d segundos.", math.ceil(retry_after))
        for name in RATE_LIMIT_HEADERS:
            value = response.headers.get(name, "")
            if isinstance(value, str) and re.fullmatch(r"[0-9]{1,16}(?:\.[0-9]{1,3})?", value):
                logger.warning("Rate limit: %s=%s.", name, value)
        if attempt < max_attempts:
            logger.warning("Nova tentativa após backoff de %.2f segundos.", delay)
        else:
            logger.warning("Limite de tentativas atingido; busca interrompida com segurança.")
        return delay

    def _get(self, session: requests.Session, url: str, *, params: dict | None = None,
             max_attempts: int = MAX_ATTEMPTS) -> dict | list:
        force_refresh = False
        # Um orçamento único de GETs, inclusive quando 401 e 429 se alternam.
        for attempt in range(1, max_attempts + 1):
            self._pace()
            token = get_valid_access_token(force_refresh=True) if force_refresh else get_valid_access_token()
            force_refresh = False
            self._tokens.add(token)
            try:
                response = session.get(url, params=params,
                                       headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                                       timeout=API_TIMEOUT, allow_redirects=False)
            except requests.RequestException:
                raise SafeError("Mercado Livre: falha de conexão, TLS ou timeout.") from None
            try:
                self._next_request_at = time.monotonic() + self.interval
                status = response.status_code
                self._statuses[url] = status
                if status == 200 or (status == 206 and url.startswith(API_ITEMS)):
                    try:
                        payload = response.json()
                    except ValueError:
                        raise SafeError("Mercado Livre: resposta JSON inválida.", status=status) from None
                    expected_type = list if url == API_ITEMS + "/bulk" else dict
                    if not isinstance(payload, expected_type):
                        if url == API_ITEMS + "/bulk":
                            from services.mercado_livre_items import safe_bulk_schema
                            for identity in (params or {}).get("ids", "").split(","):
                                if _mlb_id(identity):
                                    logger.warning("Bulk item %s: %s", identity, " ".join(safe_bulk_schema(payload, status)))
                        raise SafeError("Mercado Livre: formato de resposta inválido.", status=status)
                    self._retry_not_before = 0.0
                    return payload
                if status == 429:
                    delay = self._rate_limit(response, url, attempt, max_attempts)
                    if attempt == max_attempts:
                        raise SafeError(f"Mercado Livre HTTP 429: limite atingido após {max_attempts} tentativas; busca interrompida.",
                                        status=429, retry_after=math.ceil(delay))
                elif status == 401 and not self._refreshed and attempt < max_attempts:
                    self._refreshed = True
                    force_refresh = True
                elif status >= 500 and self.retry_server_errors and attempt < max_attempts:
                    delay = BACKOFF_BASE_SECONDS * 2 ** (attempt - 1)
                    logger.warning("Mercado Livre HTTP %d. Endpoint: %s. Tentativa: %d/%d; nova tentativa em %.2f segundos.",
                                   status, safe_endpoint(urlsplit(url).path), attempt, max_attempts, delay)
                else:
                    descriptions = {
                        401: "autenticação recusada após verificar o token",
                        403: "aplicação sem permissão para este recurso",
                        404: "recurso não encontrado",
                    }
                    description = descriptions.get(status, "serviço indisponível" if status >= 500 else "resposta recusada")
                    raise SafeError(f"Mercado Livre HTTP {status}: {description}.", status=status)
            finally:
                response.close()
            if status == 429 or status >= 500:
                self._wait(delay)
        raise SafeError("Mercado Livre: limite de tentativas atingido.")

    def search(self, term: str, limit: int) -> SearchBatch:
        batch = SearchBatch()
        remaining = math.ceil(self._retry_not_before - time.time())
        if remaining > 0:
            batch.errors.append(SafeError("Mercado Livre: aguardando backoff de consultas.", status=429, retry_after=remaining))
            batch.stop = True
            return batch
        limit = min(10, max(1, limit))
        try:
            with create_meli_session() as session:
                payload = self._get(session, API_SEARCH, params={"status": "active", "site_id": "MLB", "q": term, "limit": limit})
                results = payload.get("results")
                if not isinstance(results, list):
                    raise SafeError("Mercado Livre: formato de resultados não reconhecido.", status=200)
                identifiers = list(dict.fromkeys(row["id"] for row in results[:limit] if isinstance(row, dict) and _mlb_id(row.get("id"))))
                batch.found_ids.update(identifiers)
                for identifier in identifiers:
                    if identifier not in self._cache:
                        try:
                            detail = self._get(session, API_PRODUCTS + "/" + identifier)
                            if detail.get("id") != identifier:
                                raise SafeError("Mercado Livre: detalhe não corresponde ao produto consultado.", status=200)
                            self._cache[identifier] = normalize_product(detail, term, secrets=tuple(self._tokens))
                        except SafeError as error:
                            if error.status != 404:
                                raise
                            batch.errors.append(error)
                            self._cache[identifier] = None
                    product = self._cache[identifier]
                    if product is not None:
                        batch.products.append(replace(product, search_term=clean_text(term, 80)))
        except SafeError as error:
            batch.errors.append(error)
            batch.stop = True
        return batch


class MercadoLivreSource(CatalogSource):
    """Ofertas por item_id. Descoberta global por termo depende de acesso oficial.

    A lista explícita é uma entrada real de anúncios, não resultado inventado de
    /sites/MLB/search. Catálogo só enriquece metadados, nunca fornece preço aqui.
    """
    retry_server_errors = True

    def __init__(self, *, item_ids: tuple[str, ...] = (), request_interval_seconds: float = 1.0,
                 negative_keywords: tuple[str, ...] | None = None):
        from services.offer_relevance import DEFAULT_NEGATIVE_KEYWORDS
        if len(item_ids) > 50 or any(not _mlb_id(value) for value in item_ids):
            raise SafeError("MELI_ITEM_IDS aceita até 50 IDs MLB válidos, separados por vírgulas.")
        self.item_ids = tuple(dict.fromkeys(item_ids))
        self.negative_keywords = DEFAULT_NEGATIVE_KEYWORDS if negative_keywords is None else negative_keywords
        super().__init__(request_interval_seconds=request_interval_seconds)

    def begin_cycle(self, stop_event: threading.Event | None = None) -> None:
        super().begin_cycle(stop_event)
        self._items: dict[str, dict | None] = {}
        self._prices: dict[str, dict | None] = {}
        self._catalogs: dict[str, dict | None] = {}
        self._item_diagnostics: dict[str, CandidateDiagnostic] = {}
        self._item_endpoints: dict[str, str] = {}
        self._item_statuses: dict[str, int] = {}
        self._catalog_disabled = False

    def _diagnostic(self, batch: SearchBatch, item_id: str | None, endpoint: str,
                    status: int | None, payload: object, reason: str) -> None:
        from services.mercado_livre_items import safe_fields
        batch.diagnostics.append(CandidateDiagnostic(endpoint, status, item_id, safe_fields(payload), reason))

    @staticmethod
    def _global_item_error(error: SafeError) -> bool:
        return error.status is None or error.status in {401, 429} or error.status >= 500

    def _bulk_diagnostic(self, batch: SearchBatch, identity: str, envelope: object, reason: str) -> None:
        from services.mercado_livre_items import bulk_item_status, safe_bulk_schema
        fields = safe_bulk_schema(envelope, self._statuses.get(API_ITEMS + "/bulk"))
        logger.warning("Bulk item %s: %s", identity, " ".join(fields))
        diagnostic = CandidateDiagnostic("/items/bulk", bulk_item_status(envelope), identity, fields, reason)
        batch.diagnostics.append(diagnostic)
        self._item_diagnostics[identity] = diagnostic

    def _fallback_item(self, session: requests.Session, identity: str, batch: SearchBatch) -> None:
        from services.mercado_livre_items import ITEM_FIELDS, listing_data_problem
        endpoint = "/items/" + identity
        url = API_ITEMS + "/" + identity
        # Marca inclusive falhas: no máximo UM GET individual por ID neste ciclo.
        self._items[identity] = None
        self._item_endpoints[identity] = endpoint
        try:
            detail = self._get(session, url, params={"attributes": ",".join(ITEM_FIELDS)}, max_attempts=1)
            problem = listing_data_problem(detail, identity)
            if problem:
                raise SafeError("Mercado Livre: fallback individual — " + problem + ".",
                                status=self._statuses.get(url, 200))
        except SafeError as error:
            global_error = self._global_item_error(error)
            reason = ("INTERROMPIDO — " if global_error else "REJEITADO — ") + str(error)
            self._diagnostic(batch, identity, endpoint, error.status, {}, reason)
            self._item_diagnostics[identity] = batch.diagnostics[-1]
            if global_error:
                raise
            batch.errors.append(error)
            return
        self._items[identity] = detail
        self._item_statuses[identity] = self._statuses[url]
        self._diagnostic(batch, identity, endpoint, self._statuses[url], detail, "RECUPERADO — fallback oficial individual")
        self._item_diagnostics[identity] = batch.diagnostics[-1]

    def _load_items(self, session: requests.Session, identifiers: list[str], batch: SearchBatch) -> None:
        missing = [identity for identity in identifiers if identity not in self._items]
        if not missing:
            return
        from services.mercado_livre_items import (
            ITEM_FIELDS, ITEM_SUCCESS_STATUSES, bulk_item_body, bulk_item_id, bulk_item_status,
        )
        url = API_ITEMS + "/bulk"
        payload = self._get(session, url, params={"ids": ",".join(missing),
                            "attributes": ",".join("body." + key for key in ITEM_FIELDS)})
        if not payload:
            for identity in missing:
                self._bulk_diagnostic(batch, identity, None, "INTERROMPIDO — resposta bulk vazia; item ausente")
            raise SafeError("Mercado Livre: resposta bulk completamente inválida ou vazia.", status=self._statuses[url])
        envelopes: dict[str, list[dict]] = {}
        for envelope in payload:
            identity = bulk_item_id(envelope)
            if identity in missing:
                envelopes.setdefault(identity, []).append(envelope)
        for index, identity in enumerate(missing):
            matches = envelopes.get(identity, [])
            # Posição só ajuda o diagnóstico; NUNCA associa dados de outro ID.
            envelope = matches[0] if matches else payload[index] if index < len(payload) else None
            if len(matches) != 1:
                problem = "item duplicado no envelope" if matches else "item ausente ou envelope sem ID correspondente"
            else:
                status = bulk_item_status(envelope)
                if status is None:
                    problem = "status individual ausente ou inválido"
                elif status not in ITEM_SUCCESS_STATUSES:
                    self._items[identity] = None
                    self._bulk_diagnostic(batch, identity, envelope,
                                          f"REJEITADO — consulta de item recusada (status individual {status})")
                    error = SafeError(f"Mercado Livre: item {identity} recusado no bulk (status individual {status}).",
                                      status=status, retry_after=60 if status == 429 else 0)
                    if status == 429:
                        self._retry_not_before = time.time() + error.retry_after
                        raise error
                    batch.errors.append(error)
                    continue
                else:
                    body, problem = bulk_item_body(envelope, identity)
                    if problem is None:
                        self._items[identity] = body
                        self._item_endpoints[identity] = "/items/bulk"
                        self._item_statuses[identity] = status
                        continue
            self._bulk_diagnostic(batch, identity, envelope, "FALLBACK — " + problem)
            self._fallback_item(session, identity, batch)

    def _enrich(self, session: requests.Session, item: dict, batch: SearchBatch) -> dict | None:
        from services.mercado_livre_items import has_brand_and_image
        catalog_id = item.get("catalog_product_id")
        if not _mlb_id(catalog_id) or has_brand_and_image(item) or self._catalog_disabled:
            return None
        if catalog_id not in self._catalogs:
            url = API_PRODUCTS + "/" + catalog_id
            try:
                detail = self._get(session, url)
                if detail.get("id") != catalog_id:
                    raise SafeError("Mercado Livre: detalhe de catálogo incompatível.", status=200)
                self._catalogs[catalog_id] = detail
            except SafeError as error:
                self._catalogs[catalog_id] = None
                if error.status not in {403, 404}:
                    raise
                # Enriquecimento indisponível não elimina o item comprável.
                self._catalog_disabled = error.status == 403
                self._diagnostic(batch, item["id"], safe_endpoint(urlsplit(url).path), error.status, {},
                                 "ENRIQUECIMENTO INDISPONÍVEL — item preservado")
        return self._catalogs[catalog_id]

    def search(self, term: str, limit: int) -> SearchBatch:
        from services.mercado_livre_items import listing_problem, normalize_listing
        batch = SearchBatch(discovery="IDs de anúncios configurados; termo usado como filtro local. Descoberta global por termo pendente.")
        if not self.item_ids:
            batch.errors.append(SafeError("Descoberta global de anúncios por termo não confirmada na API oficial atual. "
                                          "Configure MELI_ITEM_IDS com anúncios reais para consulta direta; catálogo não substitui anúncios."))
            batch.stop = True
            return batch
        remaining = math.ceil(self._retry_not_before - time.time())
        if remaining > 0:
            batch.errors.append(SafeError("Mercado Livre: aguardando backoff de consultas.", status=429, retry_after=remaining))
            batch.stop = True
            return batch
        identifiers = list(self.item_ids[:min(10, max(1, limit))])
        batch.found_ids.update(identifiers)
        current_id, current_endpoint = None, "/items/bulk"
        try:
            with create_meli_session() as session:
                self._load_items(session, identifiers, batch)
                for identity in identifiers:
                    current_id, current_endpoint = identity, self._item_endpoints.get(identity, "/items/bulk")
                    item = self._items.get(identity)
                    if item is None:
                        diagnostic = self._item_diagnostics.get(identity)
                        if diagnostic and diagnostic not in batch.diagnostics:
                            batch.diagnostics.append(diagnostic)
                        continue
                    problem = listing_problem(item, secrets=tuple(self._tokens))
                    if problem:
                        self._diagnostic(batch, identity, current_endpoint, self._item_statuses.get(identity), item, "REJEITADO — " + problem)
                        continue
                    current_endpoint = "/items/" + identity + "/sale_price"
                    if identity not in self._prices:
                        try:
                            self._prices[identity] = self._get(session, API_ITEMS + "/" + identity + "/sale_price",
                                                            params={"context": "channel_marketplace"})
                        except SafeError as error:
                            if self._global_item_error(error):
                                raise
                            self._prices[identity] = None
                            batch.errors.append(error)
                    price = self._prices[identity]
                    product, reason = normalize_listing(item, price, term, secrets=tuple(self._tokens),
                                                        negative_keywords=self.negative_keywords)
                    if product is not None:
                        batch.products.append(product)
                        # Preço/link/estado pertencem ao item; buy_box_winner jamais
                        # substitui esses campos, mesmo quando existe no catálogo.
                        current_endpoint = "/products/" + item.get("catalog_product_id", "") if _mlb_id(item.get("catalog_product_id")) else current_endpoint
                        enriched = self._enrich(session, item, batch) if product.relevance_score else None
                        if enriched:
                            product, reason = normalize_listing(item, price, term, enrichment=enriched,
                                                                secrets=tuple(self._tokens), negative_keywords=self.negative_keywords)
                            batch.products[-1] = product
                    self._diagnostic(batch, identity, "/items/" + identity + "/sale_price",
                                     self._statuses.get(API_ITEMS + "/" + identity + "/sale_price"),
                                     {**item, **(price or {})}, reason)
        except SafeError as error:
            batch.errors.append(error)
            batch.stop = True
            if not batch.diagnostics or not batch.diagnostics[-1].reason.startswith("INTERROMPIDO"):
                self._diagnostic(batch, current_id, safe_endpoint(current_endpoint), error.status, {}, "INTERROMPIDO — " + str(error))
        return batch
