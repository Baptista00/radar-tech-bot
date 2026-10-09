"""Relevância local determinística, sem inferir reputação ou qualidade do vendedor."""

import re
import unicodedata
from dataclasses import dataclass

DEFAULT_NEGATIVE_KEYWORDS = (
    "adesivo", "adesivos", "reposição", "somente caixa", "não comprar", "defeito",
    "retirada de peças", "pezinhos", "skate", "skates", "conserto",
)


def fold(text: str) -> str:
    return unicodedata.normalize("NFKD", text).encode("ascii", "ignore").decode().lower()


@dataclass(frozen=True)
class Relevance:
    points: int
    reason: str
    penalty: int = 0


def assess_relevance(title: str, term: str, *, domain: str | None = None,
                     negative_keywords: tuple[str, ...] = DEFAULT_NEGATIVE_KEYWORDS) -> Relevance:
    # Import local evita ciclo: os helpers legados continuam disponíveis à suíte.
    from services.mercado_livre import DOMAIN_CATEGORIES, category_for

    text, query = fold(title), fold(term)
    for negative in negative_keywords:
        word = fold(negative).strip()
        if word and re.search(r"\b" + re.escape(word) + r"\b", text):
            return Relevance(0, "palavra negativa configurada")
    expected = category_for(term)
    actual = category_for(title)
    domain_category = DOMAIN_CATEGORIES.get(domain[4:]) if domain and domain.startswith("MLB-") else None
    if expected is None or (domain_category and domain_category != expected):
        return Relevance(0, "baixa relevância: categoria/domain incompatível com termo")
    if actual and actual != expected:
        return Relevance(0, "baixa relevância: título de outra categoria")
    if actual != expected and domain_category != expected:
        return Relevance(0, "baixa relevância: título/domain não identifica o produto buscado")
    if expected in {"mouse", "headset", "teclado", "SSD"} and re.search(
        r"^(?:kit (?:de )?)?(?:cabos?|fios?|capas?|cases?|peliculas?|adaptador(?:es)?|pecas?|borrachas?|botoes|botao|switch(?:es)?|receptor(?:es)?)\b", text
    ):
        return Relevance(0, "baixa relevância: acessório ou peça isolada")
    words = re.findall(r"\w+", text)
    suspicious = bool(re.search(r"[!?]{3,}", text) or any(words.count(w) >= 4 for w in set(words)))
    tokens = set(re.findall(r"\w+", query)) - {"de", "para", "com"}
    matched = sum(bool(re.search(r"\b" + re.escape(w) + r"\b", text)) for w in tokens)
    points = 10 if matched == len(tokens) and tokens else 5
    return Relevance(points, "relevante ao termo", 10 if suspicious else 0)
