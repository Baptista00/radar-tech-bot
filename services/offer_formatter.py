"""Mensagem em texto simples; linhas ausentes nunca recebem dados inventados."""

from models import Product, ScoreResult, SearchReport
from safety import clean_text


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
        description = "Preço abaixo das observações anteriores do histórico local."
    else:
        description = "Confira as características e condições no anúncio."
    lines.extend(["", description, "", "Ver oferta:", product.url, "",
                  "Radar Tech — a gente encontra, você economiza."])
    return "\n".join(lines)


def format_dry_run_report(report: SearchReport, threshold: int) -> str:
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
        lines.append(f"Radar Score: {entry.result.score}/100")
        if product.sold_quantity is not None:
            lines.append(f"Vendas: {product.sold_quantity}")
        if product.free_shipping is not None:
            lines.append(f"Frete grátis: {'Sim' if product.free_shipping else 'Não'}")
        passed = "passa" if entry.result.score >= threshold else "não passa"
        lines.extend([f"Status: {'APROVADO' if entry.result.approved(threshold) else 'REPROVADO'} | threshold: {passed}",
                      entry.decision, "Mercado Livre", "URL: " + product.url])
    lines.extend(["", "TOP OFERTAS:", "Ranking para análise; aprovação e antispam indicados acima."])
    for index, entry in enumerate(offers[:10], 1):
        lines.append(f"{index}. {clean_text(entry.product.title, 180)} | {format_price(entry.product.price)} | {entry.result.score}/100")
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
