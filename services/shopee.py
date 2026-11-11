"""Integração Shopee Affiliate Open API (GraphQL). Rede apenas ao chamar métodos."""

import hashlib
import json
import logging
import threading
import time
from dataclasses import dataclass, field

import requests

from models import OfferCandidate
from safety import SafeError
from services.offer_source import OfferBatch

logger = logging.getLogger(__name__)

AUTH_ERROR_CODES = {10001, 10002, 10003}
RATE_LIMIT_CODE = 10030
SYSTEM_ERROR_CODE = 10000

PRODUCT_FIELDS = (
    "itemId productName commissionRate priceMin priceMax sales imageUrl "
    "shopName offerLink productLink ratingStar"
)


@dataclass(frozen=True)
class ShopeeConfig:
    app_id: str
    secret: str = field(repr=False)
    endpoint: str = "https://open-api.affiliate.shopee.com.br/graphql"
    timeout: int = 15
    sub_ids: tuple[str, ...] = ("radartech", "", "", "", "")


class ShopeeClient:
    def __init__(self, config: ShopeeConfig, session=None, clock=time.time):
        if not config.app_id or not config.secret:
            raise SafeError("Shopee: SHOPEE_APP_ID e SHOPEE_SECRET são obrigatórios.")
        self.config = config
        self.session = session or requests
        self.clock = clock

    def _sign(self, body: str, timestamp: int) -> str:
        raw = f"{self.config.app_id}{timestamp}{body}{self.config.secret}"
        return hashlib.sha256(raw.encode("utf-8")).hexdigest()

    def _post(self, query: str) -> dict:
        payload = {"query": query}
        # Serialização padrão do Python (com espaços), igual à de requests(json=...).
        # O corpo enviado é exatamente a string assinada.
        body = json.dumps(payload)
        timestamp = int(self.clock())
        signature = self._sign(body, timestamp)
        headers = {
            "Content-Type": "application/json",
            "Authorization": f"SHA256 Credential={self.config.app_id}, Signature={signature}, Timestamp={timestamp}",
        }
        logger.debug("Shopee payload: %s", json.dumps(payload, ensure_ascii=False)[:500])
        try:
            response = self.session.post(self.config.endpoint, data=body.encode("utf-8"),
                                         headers=headers, timeout=self.config.timeout)
        except requests.RequestException:
            raise SafeError("Shopee: falha de conexão com a API.", uncertain=True, retry_after=30) from None
        status = response.status_code
        if status == 429:
            raise SafeError("Shopee: limite de requisições atingido.", status=429, retry_after=60)
        if status >= 500:
            raise SafeError("Shopee: instabilidade no servidor da API.", status=status, retry_after=30)
        if status in (401, 403):
            raise SafeError("Shopee: autenticação recusada; verifique as credenciais.", status=status)
        if status != 200:
            raise SafeError("Shopee: resposta HTTP inesperada.", status=status)
        try:
            data = response.json()
        except ValueError:
            raise SafeError("Shopee: resposta inválida (JSON).", status=status) from None
        if not isinstance(data, dict):
            raise SafeError("Shopee: resposta inválida.", status=status)
        errors = data.get("errors")
        if errors:
            codes = set()
            for error in errors if isinstance(errors, list) else []:
                extensions = error.get("extensions") if isinstance(error, dict) else None
                code = extensions.get("code") if isinstance(extensions, dict) else None
                try:
                    codes.add(int(code))
                except (TypeError, ValueError):
                    pass
            if RATE_LIMIT_CODE in codes:
                raise SafeError("Shopee: limite de requisições atingido.", status=status, retry_after=60)
            if SYSTEM_ERROR_CODE in codes:
                raise SafeError("Shopee: erro interno da API (10000).", status=status, retry_after=30)
            if codes & AUTH_ERROR_CODES:
                raise SafeError("Shopee: falha de autenticação; verifique app id, secret e relógio.", status=status)
            raise SafeError("Shopee: a API retornou erro na consulta.", status=status)
        result = data.get("data")
        if not isinstance(result, dict):
            raise SafeError("Shopee: resposta sem dados.", status=status)
        return result

    def search_products(self, keyword: str, sort_type: int | None = None, limit: int = 10,
                        page: int = 1) -> list[dict]:
        """sort_type=None usa a ordem padrão da API (relevância); 2 ordena por vendas.

        Na API BR, sortType=1 explícito responde 10000 (System Error); por isso a
        relevância é obtida omitindo o parâmetro, junto com listType: 0.
        """
        if type(limit) is not int or not 1 <= limit <= 50:
            raise SafeError("Shopee: limite deve ficar entre 1 e 50.")
        sort = f"sortType: {int(sort_type)}, " if sort_type is not None else ""
        query = (
            f"{{productOfferV2(listType: 0, keyword: {json.dumps(keyword)}, {sort}"
            f"limit: {limit}, page: {int(page)}) {{nodes {{{PRODUCT_FIELDS}}} "
            f"pageInfo {{page limit hasNextPage}}}}}}"
        )
        offer = self._post(query).get("productOfferV2")
        nodes = offer.get("nodes") if isinstance(offer, dict) else None
        if not isinstance(nodes, list):
            raise SafeError("Shopee: resposta de produtos em formato inesperado.")
        return [node for node in nodes if isinstance(node, dict)]

    def generate_short_link(self, origin_url: str) -> str | None:
        query = (
            f"mutation {{generateShortLink(input: {{originUrl: {json.dumps(origin_url)}, "
            f"subIds: {json.dumps(list(self.config.sub_ids))}}}) {{shortLink}}}}"
        )
        try:
            result = self._post(query).get("generateShortLink")
        except SafeError:
            return None
        link = result.get("shortLink") if isinstance(result, dict) else None
        return link if isinstance(link, str) and link else None


def _cents(value) -> int | None:
    try:
        cents = int(round(float(value) * 100))
    except (TypeError, ValueError):
        return None
    return cents if cents > 0 else None


def _float(value) -> float | None:
    try:
        return float(value)
    except (TypeError, ValueError):
        return None


class ShopeeSource:
    source_id = "shopee"
    simulation_only = False

    def __init__(self, config: ShopeeConfig, client: ShopeeClient | None = None):
        self.client = client or ShopeeClient(config)
        self._stop_event: threading.Event | None = None

    def begin_cycle(self, stop_event: threading.Event | None = None) -> None:
        self._stop_event = stop_event

    def _stopped(self) -> bool:
        return self._stop_event is not None and self._stop_event.is_set()

    def _candidate(self, node: dict, term: str) -> OfferCandidate | None:
        item_id = node.get("itemId")
        title = node.get("productName")
        offer_link = node.get("offerLink") or node.get("productLink")
        if item_id in (None, "") or not isinstance(title, str) or not isinstance(offer_link, str):
            return None
        price = _cents(node.get("priceMin"))
        url = offer_link
        if node.get("offerLink") and not self._stopped():
            url = self.client.generate_short_link(offer_link) or offer_link
        sales = node.get("sales")
        return OfferCandidate(
            external_id=f"SHOPEE{item_id}", title=title, price=price, url=url,
            source="shopee", original_price=None,  # priceMax é variação, não preço original
            rating=_float(node.get("ratingStar")),
            sold_quantity=sales if type(sales) is int else None,
            image_url=node.get("imageUrl") or None, search_term=term,
            item_id=f"SHOPEE{item_id}", currency_id="BRL", price_source="shopee",
        )

    def search(self, term: str, limit: int) -> OfferBatch:
        batch = OfferBatch(discovery="Shopee Affiliate Open API — busca por palavra-chave, ordenada por relevância.")
        if self._stopped():
            batch.stop = True
            return batch
        for node in self.client.search_products(term, limit=limit):
            if self._stopped():
                batch.stop = True
                break
            candidate = self._candidate(node, term)
            if candidate is not None:
                batch.candidates.append(candidate)
                batch.found_ids.add(candidate.external_id)
        return batch
