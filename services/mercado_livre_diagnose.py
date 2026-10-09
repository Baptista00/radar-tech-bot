"""Diagnóstico isolado e somente leitura. Não importa fonte, engine ou Telegram."""

import json
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from urllib.parse import quote, quote_plus

import requests

from services.mercado_livre_http import API_TIMEOUT, create_meli_session

API = "https://api.mercadolibre.com"
ITEM_ID = "MLB7390921738"
ERROR_FIELDS = ("error", "code", "message", "blocked_by", "path", "cause", "request_id")
CAUSE_FIELDS = (*ERROR_FIELDS, "type", "description", "department_id", "cause_id")
SAFE_HEADERS = ("x-request-id", "retry-after", "x-content-missing")
SCOPES = ("read", "offline_access", "write")
CATEGORIES = {
    "A": "GRANT / SCOPES", "B": "APLICAÇÃO INATIVA OU BLOQUEADA", "C": "USUÁRIO / VALIDAÇÃO",
    "D": "IP / INFRAESTRUTURA", "E": "RESTRIÇÃO A ITEM DE TERCEIRO",
    "F": "POLICYAGENT / POLÍTICA DO MERCADO LIVRE", "G": "INCONCLUSIVO",
}


class Redactor:
    """Preserva texto oficial seguro; nunca usa repr de valores desconhecidos."""

    def __init__(self, protected=()):
        self._protected = set()
        for value in protected:
            if isinstance(value, str) and value:
                self._protected.update((value, quote(value, safe=""), quote_plus(value)))

    def __repr__(self):
        return "<Redactor>"

    def text(self, value: str) -> str:
        for secret in sorted(self._protected, key=len, reverse=True):
            value = value.replace(secret, "[oculto]")
        # Também cobre credenciais inesperadas ecoadas pelo provedor.
        value = re.sub(r"(?i)\b(?:Bearer|Basic)\s+[^\s,;\"'<>]+", "[oculto]", value)
        value = re.sub(
            r"(?i)\b(?:authorization|access[_ -]?token|refresh[_ -]?token|client[_ -]?secret|"
            r"meli_client_secret|telegram_bot_token|telegram_admin_id|set-cookie|cookies?|password)"
            r"[\"']?\s*[:=]\s*(?:\"[^\"]*\"|'[^']*'|[^\s,;&}]+)", "[oculto]", value,
        )
        value = re.sub(r"\bAPP_USR-[A-Za-z0-9_-]+", "[oculto]", value)
        value = re.sub(r"\b\d{6,}:[A-Za-z0-9_-]{20,}\b", "[oculto]", value)
        value = re.sub(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}", "[email oculto]", value)
        # Impede comandos de terminal e falsificação visual de linhas do relatório.
        value = re.sub(r"[\x00-\x1f\x7f-\x9f\u202a-\u202e\u2066-\u2069]",
                       lambda match: "\\u%04x" % ord(match[0]), value)
        return value

    def value(self, value, *, depth=0):
        if depth > 5:
            return "[estrutura omitida]"
        if isinstance(value, str):
            return self.text(value)
        if type(value) is int:
            text = self.text(str(value))
            return value if text == str(value) else text
        if value is None or type(value) is bool:
            return value
        if isinstance(value, list):
            return [self.value(entry, depth=depth + 1) for entry in value[:30]]
        if isinstance(value, dict):
            return {key: self.value(value[key], depth=depth + 1) for key in CAUSE_FIELDS if key in value}
        return "[tipo omitido]"


def _identifier(value) -> str | None:
    if type(value) is int and value > 0:
        return str(value)
    if isinstance(value, str) and re.fullmatch(r"[1-9][0-9]{0,63}", value):
        return value
    return None


def _names(value, redact: Redactor) -> list[str] | None:
    if not isinstance(value, list):
        return None
    # Dado inválido não pode virar uma falsa conclusão de scope ausente.
    if any(not isinstance(name, str) or not re.fullmatch(r"[A-Za-z][A-Za-z0-9_:.*\-/]{0,199}", name)
           or redact.text(name) != name for name in value):
        return None
    return list(dict.fromkeys(value))


@dataclass(repr=False)
class Reply:
    status: int | None = None
    payload: object = None
    headers: dict = field(default_factory=dict)
    problem: str | None = None

    def safe(self, redact: Redactor, *, public=False) -> dict:
        result = {"http_status": self.status}
        if self.problem:
            result["note"] = self.problem
        if isinstance(self.payload, dict) and self.status not in {200, 206}:
            allowed = ("error", "code", "message") if public else ERROR_FIELDS
            result.update({key: redact.value(self.payload[key]) for key in allowed if key in self.payload})
        if not public:
            headers = {key: redact.text(self.headers[key]) for key in SAFE_HEADERS
                       if isinstance(self.headers.get(key), str)}
            if headers:
                result["headers"] = headers
        return result


class Reader:
    def __init__(self, token: str):
        self._token = token
        self._called = False
        self._throttled = False

    def __repr__(self):
        return "<DiagnosticReader>"

    def get(self, path: str, *, anonymous=False, params=None) -> Reply:
        if self._throttled:
            return Reply(problem="Consulta não executada após HTTP 429; respeite retry-after.")
        if self._called:
            time.sleep(1)
        self._called = True
        headers = {"Accept": "application/json"}
        if not anonymous:
            headers["Authorization"] = "Bearer " + self._token
        try:
            # Sessão nova impede cookies/autorização residual na comparação pública.
            with create_meli_session() as session:
                response = session.get(API + path, headers=headers, params=params,
                                       timeout=API_TIMEOUT, allow_redirects=False)
                try:
                    result = Reply(status=response.status_code,
                                   headers={key: response.headers[key] for key in SAFE_HEADERS if key in response.headers})
                    self._throttled = result.status == 429
                    try:
                        result.payload = response.json()
                    except ValueError:
                        result.problem = "Resposta não é JSON válido; corpo omitido."
                    return result
                finally:
                    response.close()
        except requests.exceptions.SSLError:
            return Reply(problem="Falha de TLS; certificado continuou sendo validado. Detalhes omitidos.")
        except requests.RequestException:
            return Reply(problem="Falha de conexão ou timeout; detalhes omitidos.")


def _account(reply: Reply, redact: Redactor) -> tuple[dict, str | None]:
    result = {"http_status": reply.status, "user_id_present": False}
    if reply.problem:
        result["note"] = reply.problem
    payload = reply.payload if reply.status == 200 and isinstance(reply.payload, dict) else {}
    identity = _identifier(payload.get("id"))
    result["user_id_present"] = identity is not None
    if isinstance(payload.get("site_id"), str):
        result["site_id"] = redact.text(payload["site_id"])
    state = payload.get("status")
    if isinstance(state, dict):
        safe_state = {}
        for key in ("site_status", "required_action"):
            if isinstance(state.get(key), str):
                safe_state[key] = redact.text(state[key])
        if type(state.get("confirmed_email")) is bool:
            safe_state["confirmed_email"] = state["confirmed_email"]
        for key in ("list", "sell", "buy", "billing"):
            section = state.get(key)
            if isinstance(section, dict):
                safe_state[key] = {"allow": section.get("allow") if type(section.get("allow")) is bool else None,
                                   "codes": _names(section.get("codes"), redact)}
        result["account_status"] = safe_state
    return result, identity


def _application(reply: Reply, app_id: str, redact: Redactor) -> dict:
    result = reply.safe(redact)
    result.update(active=None, sandbox_mode=None, blocked=None, scopes=None)
    if reply.status != 200 or not isinstance(reply.payload, dict):
        result["note"] = result.get("note", "Dados da aplicação indisponíveis; não significa aplicação inativa.")
        return result
    payload = reply.payload
    identity = _identifier(payload.get("id"))
    if identity is not None and identity != app_id:
        result["note"] = "Resposta pertence a outra aplicação; metadados não usados."
        return result
    for key in ("active", "sandbox_mode", "blocked"):
        if type(payload.get(key)) is bool:
            result[key] = payload[key]
    for key in ("site_id", "certification_status", "status"):
        if isinstance(payload.get(key), str):
            result[key] = redact.text(payload[key])
    result["scopes"] = _names(payload.get("scopes"), redact)
    result["mercado_pago_scopes_present"] = (
        any(scope.startswith("urn:mp:") for scope in result["scopes"]) if result["scopes"] is not None else None
    )
    return result


def _grant(reader: Reader, app_id: str, user_id: str | None, redact: Redactor) -> dict:
    result = {"found": None, "user_matches": None, "scopes": None,
              "scope_presence": dict.fromkeys(SCOPES), "queries": []}
    if user_id is None:
        result["note"] = "Sem user_id validado por /users/me; grant não pode ser comparado."
        return result

    def select(rows):
        return [row for row in rows if isinstance(row, dict)
                and _identifier(row.get("user_id")) == user_id and _identifier(row.get("app_id")) == app_id]

    reply = reader.get("/applications/" + app_id + "/grants")
    result["queries"].append({"endpoint": "/applications/{APP_ID}/grants", **reply.safe(redact)})
    rows = reply.payload.get("grants") if reply.status == 200 and isinstance(reply.payload, dict) else None
    matches = select(rows) if isinstance(rows, list) else []
    if len(matches) != 1:
        # A primeira página não prova ausência. Consulta documentada, restrita à conta.
        reply = reader.get("/users/" + user_id + "/applications")
        result["queries"].append({"endpoint": "/users/{USER_ID}/applications", **reply.safe(redact)})
        rows = reply.payload if reply.status == 200 and isinstance(reply.payload, list) else None
        matches = select(rows) if isinstance(rows, list) else []
        if rows is None:
            result["note"] = "Grant não verificável pela API nesta execução. DevCenter > Meus aplicativos > Administrar permissões."
            return result
        if any(not isinstance(row, dict) or _identifier(row.get("user_id")) != user_id
               or _identifier(row.get("app_id")) is None for row in rows):
            result["user_matches"] = False
            result["note"] = "Resposta de grants divergente ou incompleta; scopes não atribuídos ao usuário."
            return result
        if not matches:
            result.update(found=False, user_matches=False)
            result["note"] = "Aplicação não encontrada na lista de autorizações da conta autenticada."
            return result
    if len(matches) != 1:
        result["note"] = "Mais de um grant correspondente; resposta ambígua."
        return result
    selected = matches[0]
    scopes = _names(selected.get("scopes"), redact)
    result.update(found=True, user_matches=True, scopes=scopes,
                  scope_presence={scope: scope in scopes if scopes is not None else None for scope in SCOPES})
    for key in ("date_created", "date_updated", "status"):
        if isinstance(selected.get(key), str):
            result[key] = redact.text(selected[key])
    return result


def _own_item(reader: Reader, user_id: str | None, third: Reply, redact: Redactor) -> dict:
    if user_id is None:
        return {"available": None, "note": "Comparação não executada: user_id não validado."}
    search = reader.get("/users/" + user_id + "/items/search", params={"limit": 1})
    result = {"search": search.safe(redact), "available": None}
    payload = search.payload if isinstance(search.payload, dict) else {}
    rows = payload.get("results")
    if search.status != 200 or not isinstance(rows, list):
        result["note"] = "Não foi possível verificar se há item próprio; consulta indisponível ou inválida."
        return result
    if not rows:
        result.update(available=False, note="Não há item próprio disponível para comparação.")
        return result
    identity = rows[0]
    if not isinstance(identity, str) or not re.fullmatch(r"ML[A-Z][0-9]{1,20}", identity):
        result["note"] = "ID de item próprio inválido; comparação não executada."
        return result
    # Se o ID informado for da própria conta, não repete o GET desse recurso.
    detail = third if identity == ITEM_ID else reader.get("/items/" + identity)
    result.update(available=True, same_as_target=identity == ITEM_ID, detail=detail.safe(redact))
    if detail.status in {200, 206} and isinstance(detail.payload, dict):
        seller_id = _identifier(detail.payload.get("seller_id"))
        result["owner_confirmed"] = (detail.payload.get("id") == identity and seller_id == user_id
                                     if seller_id is not None else None)
    return result


def classify(report: dict) -> dict:
    item, account, app, grant = (report[key] for key in ("item", "users_me", "application", "grant"))
    evidence = []
    if item.get("http_status") != 403:
        return {"category": "G) " + CATEGORIES["G"], "evidence": ["HTTP 403 do item não foi reproduzido nesta execução."]}
    # Só mensagem do item com credencial pode explicar o 403 desse item.
    def signals(value):
        if isinstance(value, str):
            yield re.sub(r"[_-]+", " ", value.casefold()).strip(" .")
        elif isinstance(value, list):
            for entry in value:
                yield from signals(entry)
        elif isinstance(value, dict):
            for key in ("error", "code", "message", "description", "cause"):
                if key in value:
                    yield from signals(value[key])

    observed = set(signals(item))
    candidates = {}
    if observed & {"invalid scopes", "invalid scope", "insufficient scope", "insufficient scopes"}:
        candidates["A"] = "Erro oficial do item informa scopes inválidos ou insuficientes."
    if grant.get("found") is False or grant.get("scope_presence", {}).get("read") is False:
        candidates["A"] = "Consulta oficial não encontrou o grant da conta/aplicação ou confirmou ausência de read."
    if app.get("active") is False or app.get("blocked") is True or app.get("status") in {"blocked", "disabled", "inactive"} or observed & {
            "application blocked", "application disabled", "application inactive"}:
        candidates["B"] = "Aplicação oficialmente inativa/bloqueada ou erro do item declara esse bloqueio."
    state = account.get("account_status", {}).get("site_status")
    if state in {"inactive", "blocked", "suspended", "deactivated"} or observed & {
            "user validation required", "user suspended", "user inactive"}:
        candidates["C"] = "Estado oficial da conta ou erro do item identifica inatividade/validação pendente."
    if observed & {"ip not allowed", "ip blocked", "ip restriction", "ip address is not allowed"}:
        candidates["D"] = "Erro oficial do item menciona restrição de IP explicitamente."
    if observed & {"third party item restricted", "third party items not allowed",
                   "caller is not the owner", "caller is not owner"}:
        candidates["E"] = "Erro oficial do item identifica restrição por titularidade/terceiros."
    policy = item.get("blocked_by", "").casefold() == "policyagent" if isinstance(item.get("blocked_by"), str) else False
    policy = policy or "pa unauthorized result from policies" in observed
    if len(candidates) == 1:
        category = next(iter(candidates))
        evidence.append(candidates[category])
    elif len(candidates) > 1:
        category = "G"
        evidence.extend(candidates.values())
        evidence.append("Há múltiplos impedimentos; não é possível atribuir uma causa única.")
    elif policy:
        category = "F"
        evidence.append("Resposta autenticada do item identifica PolicyAgent; política interna específica não informada.")
    else:
        category = "G"
        evidence.append("403/access_denied genérico não distingue scopes, IP, usuário ou restrição de recurso.")
    own_status = report.get("own_item", {}).get("detail", {}).get("http_status")
    if own_status in {200, 206}:
        evidence.append("Item próprio acessível; diferença entre recursos não prova restrição geral a itens de terceiros.")
    result = {"category": category + ") " + CATEGORIES[category], "evidence": evidence}
    if policy and account.get("http_status") == 200 and account.get("user_id_present") and app.get("active") is True \
            and grant.get("user_matches") is True and grant.get("scope_presence", {}).get("read") is True:
        result["next_step"] = ("OAuth válido, aplicação ativa e grant correspondente com read. Acionar suporte oficial do Mercado Livre; "
                               "anexar data UTC, endpoint, HTTP, message/code/blocked_by e request_id seguros deste relatório. "
                               "Não continuar alterando o pipeline para contornar PolicyAgent.")
    else:
        result["next_step"] = "Conferir evidências e limitações abaixo; não alterar o pipeline com base em 403 genérico."
    return result


def diagnose(config, token: str, *, protected=()) -> dict:
    redact = Redactor((token, config.client_secret, *protected))
    reader = Reader(token)
    account, user_id = _account(reader.get("/users/me"), redact)
    app = _application(reader.get("/applications/" + config.client_id), config.client_id, redact)
    grant = _grant(reader, config.client_id, user_id, redact)
    third = reader.get("/items/" + ITEM_ID)
    anonymous = reader.get("/items/" + ITEM_ID, anonymous=True)
    item = third.safe(redact)
    if third.status in {200, 206}:
        item["success"] = isinstance(third.payload, dict) and third.payload.get("id") == ITEM_ID
        if not item["success"]:
            item["note"] = "HTTP de sucesso, mas detalhe não confirma o ID solicitado."
    report = {
        "checked_at_utc": datetime.now(timezone.utc).isoformat(), "auto_publish": False, "telegram_messages": 0,
        "users_me": account, "application": app, "grant": grant, "item": item,
        "without_token": anonymous.safe(redact, public=True),
        "own_item": _own_item(reader, user_id, third, redact),
    }
    if anonymous.status in {200, 206} and isinstance(anonymous.payload, dict) and anonymous.payload.get("id") == ITEM_ID:
        report["public_comparison"] = "Recurso consultado respondeu sem credencial nesta execução; não é solução de produção."
    elif anonymous.status in {401, 403}:
        report["public_comparison"] = "Consulta sem credencial recusada; isso, isoladamente, não prova exigência universal de autenticação."
    else:
        report["public_comparison"] = "Consulta pública inconclusiva nesta execução."
    report["classification"] = classify(report)
    report["manual_checks"] = [
        "DevCenter > Meus aplicativos > aplicação > Administrar permissões: conferir/exportar grant da conta.",
        "DevCenter > Meus aplicativos > aplicação > Editar: conferir unidades e permissões funcionais. "
        "Se scopes urn:mp: aparecerem, verificar separação Mercado Livre/Mercado Pago conforme documentação oficial.",
        "DevCenter > menu da aplicação > Gerenciar intervalos de IP: somente conferir se habilitado; não alterar. "
        "Ausência dessa opção não prova bloqueio de IP.",
        "Dados privados da aplicação podem exigir token de quem a criou; campo ausente não significa false.",
    ]
    return report


def format_report(report: dict) -> str:
    labels = {"http_status": "HTTP", "user_id_present": "user_id presente", "active": "Aplicação ativa",
              "found": "Grant encontrado", "user_matches": "Usuário do grant corresponde ao /users/me",
              "scopes": "Scopes concedidos", "scope_presence": "Presença dos scopes", "note": "Observação"}
    lines = ["RADAR TECH — DIAGNÓSTICO DE ACESSO", "AUTO_PUBLISH=false", "Telegram: 0 mensagens",
             "Data UTC: " + report["checked_at_utc"]]
    for title, key in (("Conta /users/me", "users_me"), ("Aplicação", "application"), ("Grant real", "grant"),
                       ("Item terceiro /items/" + ITEM_ID, "item"), ("Mesmo item sem token", "without_token"),
                       ("Item próprio", "own_item"), ("Classificação", "classification")):
        lines.extend(("", title + ":"))
        for name, value in report[key].items():
            if name == "scope_presence":
                for scope, present in value.items():
                    lines.append(scope + ": " + ("Sim" if present is True else "Não" if present is False else "Não informado"))
                continue
            rendered = "Sim" if value is True else "Não" if value is False else "Não informado" if value is None \
                else json.dumps(value, ensure_ascii=False)
            label = "Scopes da aplicação" if key == "application" and name == "scopes" else labels.get(name, name)
            lines.append(label + ": " + rendered)
    own_status = report["own_item"].get("detail", {}).get("http_status")
    lines.extend(("", f"Item próprio: HTTP {own_status or 'não disponível'}",
                  f"Item terceiro: HTTP {report['item'].get('http_status') or 'não disponível'}",
                  report["public_comparison"], "", "Verificações manuais:"))
    lines.extend("- " + check for check in report["manual_checks"])
    return "\n".join(lines)
