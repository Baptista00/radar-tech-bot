"""Ofertas em HTML do Telegram e relatórios locais; sem dados inventados."""

from html import escape
from urllib.parse import urlsplit

from models import Product, ScoreResult, SearchReport
from safety import clean_text
from services.categories import canonical_category, category_icon
from services.offer_source import SOURCE_NAMES


CATEGORY_DESCRIPTIONS = {
    "mouse": "Uma opção para quem está procurando melhorar o setup.",
    "teclado": "Uma opção para quem está procurando um teclado para o setup.",
    "headset": "Uma opção para quem quer completar o setup com um headset.",
    "ssd": "Uma opção para quem está buscando mais armazenamento para o PC.",
    "ram": "Uma opção para quem está pesquisando memória RAM para o PC.",
    "monitor": "Uma opção para quem está pesquisando um monitor para o setup.",
    "notebook": "Uma opção para quem está pesquisando notebook para o dia a dia.",
    "gpu": "Uma opção para quem está pesquisando uma placa de vídeo para o PC.",
    "cpu": "Uma opção para quem está pesquisando um processador para o PC.",
    "microfone": "Uma opção para quem está procurando um microfone para o setup.",
    "webcam": "Uma opção para quem está pesquisando uma webcam para o setup.",
    "controle": "Uma opção para quem está procurando um controle para jogar.",
    "power_bank": "Uma opção para quem está pesquisando uma bateria portátil.",
    "hub": "Uma opção para quem está procurando um hub para o setup.",
    "carregador": "Uma opção para quem está pesquisando um carregador.",
    "setup": "Uma opção para quem está procurando complementar o setup.",
    "other": "Confira as características e condições do produto no anúncio.",
}
PROMOTION_NOTICE = "<i>Promoção sujeita a alteração a qualquer momento.</i>"
# Códigos Unicode explícitos preservam os símbolos mesmo em edições via terminal.
PRICE_ICON = "\U0001f525"  # fogo
DISCOUNT_ICON = "\U0001f4c9"  # gráfico em queda
SCORE_ICON = "\U0001f4e1"  # antena de satélite


def _public_url(value: str) -> str:
    """Escapa um link público sem reconstruir ou alterar parâmetros do pipeline."""
    if not isinstance(value, str) or any(char.isspace() or ord(char) < 32 for char in value) or "\\" in value:
        return ""
    try:
        link = urlsplit(value)
        if (link.scheme not in {"https", "http"} or not link.hostname
                or link.username is not None or link.password is not None):
            return ""
        # Também rejeita portas inválidas, sem restringir a origem do link.
        link.port
    except ValueError:
        return ""
    return escape(value, quote=True)


def _has_reference_price(product: Product) -> bool:
    # Product normalizado já valida a referência; não usar histórico como "De:".
    return type(product.original_price) is int and 0 < product.price < product.original_price < 10_000_000_000


def format_price(cents: int) -> str:
    whole, fraction = divmod(cents, 100)
    return f"R$ {whole:,}".replace(",", ".") + f",{fraction:02d}"


def format_offer(product: Product, result: ScoreResult, *, public_channel_url: str = "") -> str:
    category = canonical_category(product.category)
    title = escape(clean_text(product.title, 180), quote=True)
    heading = f"<b>{category_icon(category)} {title}</b>"
    if product.source == "fixture":
        heading += "\n<i>SIMULAÇÃO — dados fictícios</i>"
    blocks = [heading, CATEGORY_DESCRIPTIONS[category]]
    prices = []
    if _has_reference_price(product):
        prices.append(f"De: <s>{format_price(product.original_price)}</s>")
    prices.append(f"{PRICE_ICON} <b>Por: {format_price(product.price)}</b>")
    discount = product.discount_percentage if _has_reference_price(product) else None
    if discount is not None and 0 < discount < 100:
        percent = f"{discount:.2f}".rstrip("0").rstrip(".").replace(".", ",")
        prices.append(f"{DISCOUNT_ICON} {percent}% OFF")
    blocks.append("\n".join(prices))
    details = [f"{SCORE_ICON} Radar Score: {result.score}/100 — {escape(result.band)}"]
    if product.free_shipping is True:
        details.append("🚚 Frete grátis")
    if type(product.rating) in {int, float} and 0 <= product.rating <= 5:
        details.append(f"Avaliação: {product.rating:g}")
    # Mantém as afirmações de histórico já sustentadas pelo pipeline, sem usá-las
    # para inventar preço anterior, desconto ou atributos na descrição curta.
    if result.history_claim_days is not None and result.history_claim_days >= 3:
        details.append(f"Menor preço observado pelo Radar nos últimos {result.history_claim_days} dias do histórico local.")
    elif result.criteria.get("history", 0) > 0:
        details.append("Preço abaixo das observações anteriores do histórico local.")
    blocks.append("\n".join(details))
    url = _public_url(product.url)
    if url:
        blocks.append(f"Compre aqui 👉 {url}")
    channel_url = _public_url(public_channel_url)
    if channel_url:
        blocks.append(f"Envie o Radar Tech pra galera 👇\n{channel_url}")
    blocks.append(PROMOTION_NOTICE)
    return "\n\n".join(blocks)


def format_dry_run_report(report: SearchReport, threshold: int, *, public_channel_url: str = "") -> str:
    offers = sorted(report.offers, key=lambda entry: (-entry.result.score, entry.product.price))
    passing = sum(entry.result.score >= threshold for entry in offers)
    lines = ["RADAR TECH — DRY RUN", "", f"Termos pesquisados: {report.terms_searched}",
             f"Itens encontrados: {len(report.found_ids)}",
             f"Itens válidos: {len(report.valid_ids)}",
             f"Itens relevantes: {len(report.relevant_ids)}",
             f"Ofertas acima do threshold: {passing} (score >= {threshold})",
             f"Erros: {len(report.errors)}", f"Simulações registradas: {report.simulated}",
             ""]
    if report.discovery:
        lines.extend([report.discovery, ""])
    lines.append("TODAS AS OFERTAS ELEGÍVEIS")
    if not offers:
        lines.append("Nenhuma oferta elegível nesta busca. Confira diagnóstico e erros abaixo.")
    for index, entry in enumerate(offers, 1):
        product = entry.product
        lines.extend(["", f"{index}. {clean_text(product.title, 180)}",
                      f"Item ID: {product.item_id or product.external_id}", f"Preço: {format_price(product.price)}"])
        if product.discount_percentage is not None:
            lines.append(f"Preço anterior: {format_price(product.original_price)}")
        if product.discount_percentage is not None:
            percent = f"{product.discount_percentage:.2f}".rstrip("0").rstrip(".").replace(".", ",")
            lines.append(f"Desconto: {percent}%")
        lines.append(f"Radar Score: {entry.result.score}/100 — {entry.result.band}")
        if product.sold_quantity is not None:
            lines.append(f"Vendas: {product.sold_quantity}")
        if product.free_shipping is not None:
            lines.append(f"Frete grátis: {'Sim' if product.free_shipping else 'Não'}")
        passed = "passa" if entry.result.score >= threshold else "não passa"
        lines.extend([f"Status: {'APROVADO' if entry.result.approved(threshold) else 'REPROVADO'} | threshold: {passed}",
                      entry.decision, SOURCE_NAMES.get(product.source, "Fonte personalizada"), "URL: " + product.url])
    lines.extend(["", "TOP OFERTAS:", "Ranking para análise; aprovação e antispam indicados acima."])
    for index, entry in enumerate(offers[:10], 1):
        lines.append(f"{index}. {clean_text(entry.product.title, 180)} | {format_price(entry.product.price)} | {entry.result.score}/100 — {entry.result.band}")
    fixtures = [entry for entry in offers if entry.product.source == "fixture"]
    if fixtures:
        # Mostra cenários distintos mesmo quando o antispam retém as simulações.
        examples = []
        scenarios = (
            lambda p: _has_reference_price(p) and p.free_shipping is True,
            lambda p: not _has_reference_price(p) and p.free_shipping is None,
            lambda p: _has_reference_price(p) and p.free_shipping is None,
        )
        for matches in scenarios:
            example = next((entry for entry in fixtures if matches(entry.product) and entry not in examples), None)
            if example is not None:
                examples.append(example)
        examples.extend(entry for entry in fixtures if entry not in examples)
        lines.extend(["", "PRÉVIAS DAS MENSAGENS — HTML do Telegram (sem publicação)"])
        for index, entry in enumerate(examples[:3], 1):
            lines.extend(["", f"Prévia {index} — {entry.decision}",
                          format_offer(entry.product, entry.result, public_channel_url=public_channel_url)])
    if report.diagnostics:
        lines.extend(["", "DIAGNÓSTICO SEGURO (primeiros 10 registros):"])
        for diagnostic in report.diagnostics[:10]:
            status = str(diagnostic.http_status) if diagnostic.http_status is not None else "não disponível"
            lines.append(f"{diagnostic.item_id or 'Consulta'}: {diagnostic.reason}")
            lines.append(f"Endpoint: {diagnostic.endpoint} | HTTP: {status}")
            if diagnostic.fields:
                lines.append("Campos/tipos: " + ", ".join(diagnostic.fields))
    if report.errors:
        lines.extend(["", "Erros seguros:", *(clean_text(error) for error in report.errors)])
    lines.extend(["", "Nenhuma oferta enviada ao canal. Histórico local em construção."])
    return "\n".join(lines)


def format_search_summary(report: SearchReport, threshold: int, *, auto_publish: bool = False) -> str:
    approved = sum(entry.result.approved(threshold) for entry in report.offers)
    lines = ["Radar Tech — busca concluída", f"Resultado: {report.status}",
             f"Itens encontrados: {len(report.found_ids)}", f"Itens válidos: {len(report.valid_ids)}",
             f"Itens relevantes: {len(report.relevant_ids)}", f"Ofertas aprovadas: {approved}",
             f"Erros: {len(report.errors)}"]
    if report.discovery:
        lines.append(report.discovery)
    if report.errors:
        lines.append(clean_text(report.errors[0], 350))
    if not auto_publish:
        lines.append("Modo seguro: nenhuma oferta enviada ao canal.")
    return "\n".join(lines)
