"""Mensagem em texto simples; linhas ausentes nunca recebem dados inventados."""

from models import Product, ScoreResult


def format_price(cents: int) -> str:
    whole, fraction = divmod(cents, 100)
    return f"R$ {whole:,}".replace(",", ".") + f",{fraction:02d}"


def format_offer(product: Product, result: ScoreResult) -> str:
    lines = ["🔥 ACHADO RADAR TECH", "", product.title, "", format_price(product.price)]
    discount = product.discount_percentage
    if discount is not None:
        percent = f"{discount:.1f}".rstrip("0").rstrip(".").replace(".", ",")
        lines.append(f"{percent}% OFF")
    if product.rating is not None:
        lines.extend(["", f"⭐ Avaliação: {product.rating:g}"])
    lines.append(f"Radar Score: {result.score}/100")
    if result.criteria.get("history", 0) > 0:
        description = "Preço abaixo do menor valor observado no histórico local."
    else:
        description = "Confira as características e condições no anúncio."
    lines.extend(["", description, "", "Ver oferta:", product.url, "",
                  "Radar Tech — a gente encontra, você economiza."])
    return "\n".join(lines)
