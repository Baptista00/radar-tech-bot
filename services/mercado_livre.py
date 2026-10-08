"""Adaptador da API oficial. Sem scraping, cookies, login ou renovação OAuth."""

import math
import re
import unicodedata
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import requests

from models import Product
from safety import SafeError, clean_text
from services.mercado_livre_http import API_TIMEOUT, create_meli_session

API_SEARCH = "https://api.mercadolibre.com/sites/MLB/search"
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


class ProductSource(Protocol):
    def search(self, term: str, limit: int) -> list[Product]: ...


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


def normalize_item(item: Any, term: str) -> Product | None:
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
    price, url, category = cents(item.get("price")), normal_product_url(item.get("permalink")), category_for(title)
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
    image = item.get("thumbnail")
    if not isinstance(image, str) or not re.match(r"^https?://[^/]+\.mlstatic\.com/", image):
        image = None
    elif image.startswith("http:"):
        image = "https:" + image[5:]
    return Product(
        external_id=external_id, title=clean_text(title, 180), price=price,
        original_price=original, url=url, rating=rating, sold_quantity=sold,
        image_url=image, brand=brand, category=category, search_term=clean_text(term, 80),
    )


class MercadoLivreSource:
    def __init__(self, access_token: str = ""):
        self._access_token = access_token

    def search(self, term: str, limit: int) -> list[Product]:
        if not self._access_token:
            raise SafeError("Mercado Livre: acesso pendente. Configure um access token autorizado para a API pública de busca.")
        try:
            with create_meli_session() as session:
                response = session.get(
                    API_SEARCH, params={"q": term, "limit": min(max(limit, 1), 50), "condition": "new"},
                    headers={"Authorization": f"Bearer {self._access_token}"},
                    timeout=API_TIMEOUT, allow_redirects=False,
                )
        except requests.RequestException:
            raise SafeError("Mercado Livre: falha de conexão ou timeout.") from None
        code = response.status_code
        if code != 200:
            descriptions = {
                401: "token ausente, inválido ou expirado",
                403: "aplicação sem permissão para este recurso; token sozinho pode não liberar busca de terceiros",
                404: "recurso indisponível para esta integração",
                429: "limite de consultas atingido; próxima tentativa somente no próximo ciclo",
            }
            description = descriptions.get(code, "consulta recusada ou serviço indisponível")
            raise SafeError(f"Mercado Livre HTTP {code}: {description}.", status=code)
        try:
            payload = response.json()
        except ValueError:
            raise SafeError("Mercado Livre: resposta inválida.") from None
        if not isinstance(payload, dict) or not isinstance(payload.get("results"), list):
            raise SafeError("Mercado Livre: formato de resultados não reconhecido.")
        products = []
        for item in payload["results"][:limit]:
            product = normalize_item(item, term)
            if product is not None:
                products.append(product)
        return products
