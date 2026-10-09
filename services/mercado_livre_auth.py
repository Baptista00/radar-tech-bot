"""OAuth oficial com PKCE. Importar o modulo nao le .env nem inicia rede."""

import argparse
import base64
import getpass
import hashlib
import http.client
import json
import logging
import math
import os
import re
import secrets
import sys
import time
import warnings
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlsplit

import requests
from dotenv import load_dotenv

from safety import SafeError, configure_logging
from services.meli_oauth_store import LocalOAuthStore
from services.mercado_livre_http import API_TIMEOUT, create_meli_session

AUTHORIZATION_URL = "https://auth.mercadolivre.com.br/authorization"
TOKEN_URL = "https://api.mercadolibre.com/oauth/token"
USERS_ME_URL = "https://api.mercadolibre.com/users/me"
BASE_DIR = Path(__file__).resolve().parents[1]
PENDING_TTL_SECONDS = 600
EXPIRY_MARGIN_SECONDS = 60
logger = logging.getLogger(__name__)


@dataclass(frozen=True, repr=False)
class OAuthConfig:
    client_id: str
    client_secret: str = field(repr=False)
    redirect_uri: str


def _https_url(value: str):
    try:
        if not value.isascii() or any(ord(char) <= 32 for char in value) or "\\" in value:
            raise ValueError
        parts = urlsplit(value)
        if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
                or parts.fragment or "#" in value):
            raise ValueError
        _ = parts.port
        return parts
    except (ValueError, TypeError):
        raise SafeError("OAuth: URL invalida; use HTTPS, sem credenciais ou fragmento.") from None


def load_oauth_config(environ: Mapping[str, str] | None = None) -> OAuthConfig:
    # Somente a chamada explicita pelo usuario carrega o .env; testes usam mapping.
    if environ is None:
        load_dotenv(BASE_DIR / ".env", override=False)
        environ = os.environ
    values = []
    for key in ("MELI_CLIENT_ID", "MELI_CLIENT_SECRET", "MELI_REDIRECT_URI"):
        value = environ.get(key, "").strip()
        if not value or value.startswith("seu_") or len(value) > 4096 or any(ord(c) < 32 for c in value):
            raise SafeError(f"OAuth: configure {key}; valor omitido.")
        values.append(value)
    config = OAuthConfig(*values)
    if not re.fullmatch(r"[0-9]{1,64}", config.client_id):
        raise SafeError("OAuth: MELI_CLIENT_ID precisa ser o identificador numerico da aplicacao.")
    redirect = _https_url(config.redirect_uri)
    if redirect.query or "?" in config.redirect_uri or redirect.hostname.endswith(".example"):
        raise SafeError("OAuth: configure uma MELI_REDIRECT_URI fixa, sem query, igual a cadastrada no DevCenter.")
    return config


def generate_code_verifier() -> str:
    return secrets.token_urlsafe(64)


def create_code_challenge(verifier: str) -> str:
    if not re.fullmatch(r"[A-Za-z0-9._~-]{43,128}", verifier):
        raise SafeError("OAuth: code_verifier invalido; gere uma nova autorizacao.")
    digest = hashlib.sha256(verifier.encode("ascii")).digest()
    return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


def validate_state(received: str, expected: str) -> None:
    if (not received or not expected or not received.isascii() or not expected.isascii()
            or not secrets.compare_digest(received, expected)):
        raise SafeError("OAuth: state ausente ou divergente; callback recusada.")


@dataclass(frozen=True, repr=False)
class Callback:
    code: str | None
    state: str
    denied: bool = False


def parse_callback(callback_url: str, redirect_uri: str) -> Callback:
    if len(callback_url) > 16384:
        raise SafeError("OAuth: callback invalida; conteudo omitido.")
    actual, expected = _https_url(callback_url), _https_url(redirect_uri)
    endpoint = lambda parts: (parts.scheme, parts.hostname, parts.port or 443, parts.path)
    if endpoint(actual) != endpoint(expected):
        raise SafeError("OAuth: callback nao corresponde a MELI_REDIRECT_URI cadastrada.")
    if re.search(r"%(?![0-9a-fA-F]{2})", actual.query):
        raise SafeError("OAuth: codificacao da callback invalida.")
    try:
        pairs = parse_qsl(actual.query, keep_blank_values=True, strict_parsing=True,
                          max_num_fields=20, errors="strict")
    except (ValueError, UnicodeError):
        raise SafeError("OAuth: parametros da callback invalidos.") from None
    params = dict(pairs)
    if len(params) != len(pairs):
        raise SafeError("OAuth: callback possui parametros duplicados.")
    state, code, error = params.get("state", ""), params.get("code"), params.get("error")
    if not state or not state.isascii() or len(state) > 256 or any(c.isspace() for c in state):
        raise SafeError("OAuth: state ausente ou invalido; callback recusada.")
    if (code is None) == (error is None):
        raise SafeError("OAuth: callback precisa conter um code ou um erro de autorizacao.")
    if error is not None:
        if not error:
            raise SafeError("OAuth: callback de erro invalida.")
        return Callback(None, state, denied=True)
    if not code or len(code) > 4096 or not code.isascii() or any(ord(c) <= 32 for c in code):
        raise SafeError("OAuth: authorization code ausente ou invalido.")
    return Callback(code, state)


def _number(value: Any) -> bool:
    return type(value) in (int, float) and math.isfinite(value)


def _token(value: Any) -> bool:
    return isinstance(value, str) and 0 < len(value) <= 8192 and all(32 < ord(c) < 127 for c in value)


class MercadoLivreAuth:
    def __init__(self, config: OAuthConfig, store: LocalOAuthStore | None = None,
                 *, clock: Callable[[], float] = time.time):
        self.config = config
        self.store = store if store is not None else LocalOAuthStore()
        self.clock = clock

    def authorize(self) -> str:
        verifier = generate_code_verifier()
        state = secrets.token_urlsafe(32)
        challenge = create_code_challenge(verifier)
        with self.store.locked():
            saved = self.store.load()
            saved["pending"] = {
                "client_id": self.config.client_id, "redirect_uri": self.config.redirect_uri,
                "code_verifier": verifier, "state": state, "created_at": self.clock(),
            }
            self.store.save(saved)
        return AUTHORIZATION_URL + "?" + urlencode({
            "response_type": "code", "client_id": self.config.client_id,
            "redirect_uri": self.config.redirect_uri, "code_challenge": challenge,
            "code_challenge_method": "S256", "state": state,
        })

    def _pending(self, saved: dict[str, Any]) -> dict[str, Any]:
        pending = saved.get("pending")
        if not isinstance(pending, dict):
            raise SafeError("OAuth: nenhuma autorizacao pendente. Execute authorize primeiro.")
        created = pending.get("created_at")
        if not _number(created) or not 0 <= self.clock() - created < PENDING_TTL_SECONDS:
            saved.pop("pending", None)
            self.store.save(saved)
            raise SafeError("OAuth: autorizacao local expirou. Execute authorize novamente.")
        if (pending.get("client_id") != self.config.client_id
                or pending.get("redirect_uri") != self.config.redirect_uri):
            raise SafeError("OAuth: configuracao mudou desde authorize; inicie uma nova autorizacao.")
        verifier, state = pending.get("code_verifier"), pending.get("state")
        if not isinstance(verifier, str) or not isinstance(state, str):
            raise SafeError("OAuth: autorizacao local invalida. Execute authorize novamente.")
        create_code_challenge(verifier)
        return pending

    def _request_tokens(self, grant: dict[str, str], *, previous_refresh: str | None = None) -> dict[str, Any]:
        started = self.clock()
        body = {"client_id": self.config.client_id, "client_secret": self.config.client_secret, **grant}
        try:
            with create_meli_session() as session:
                response = session.post(TOKEN_URL, data=body, headers={"Accept": "application/json"},
                                        timeout=API_TIMEOUT, allow_redirects=False)
        except requests.Timeout:
            raise SafeError("OAuth: timeout na troca; resultado incerto. Execute authorize novamente.", uncertain=True) from None
        except requests.RequestException:
            raise SafeError("OAuth: falha de conexao; resultado incerto. Execute authorize novamente.", uncertain=True) from None
        status = response.status_code
        if status != 200:
            descriptions = {
                400: "concessao ou parametros recusados; confira o cadastro e autorize novamente",
                401: "aplicacao nao autenticada; confira a configuracao local",
                403: "acesso nao permitido para esta aplicacao",
                429: "limite atingido; aguarde antes de iniciar nova autorizacao",
            }
            description = descriptions.get(status, "servico indisponivel ou resposta recusada; autorize novamente")
            raise SafeError(f"OAuth HTTP {status}: {description}.", status=status)
        try:
            payload = response.json()
        except ValueError:
            raise SafeError("OAuth: resposta invalida; execute authorize novamente.", uncertain=True) from None
        if not isinstance(payload, dict):
            raise SafeError("OAuth: resposta invalida; execute authorize novamente.", uncertain=True)
        access, refresh, ttl = payload.get("access_token"), payload.get("refresh_token"), payload.get("expires_in")
        if (not _token(access) or not _token(refresh) or type(ttl) is not int or not 0 < ttl <= 31536000
                or str(payload.get("token_type", "")).lower() != "bearer"):
            raise SafeError("OAuth: resposta de tokens incompleta; execute authorize novamente.", uncertain=True)
        if previous_refresh is not None and secrets.compare_digest(refresh, previous_refresh):
            raise SafeError("OAuth: refresh token nao foi rotacionado; execute authorize novamente.", uncertain=True)
        if started + ttl <= self.clock():
            raise SafeError("OAuth: token ja expirou durante a troca; execute authorize novamente.", uncertain=True)
        return {"client_id": self.config.client_id, "access_token": access, "refresh_token": refresh,
                "expires_at": started + ttl, "refresh_after": started + max(1, ttl - EXPIRY_MARGIN_SECONDS)}

    def exchange(self, callback_url: str) -> None:
        callback = parse_callback(callback_url, self.config.redirect_uri)
        with self.store.locked():
            saved = self.store.load()
            pending = self._pending(saved)
            validate_state(callback.state, pending["state"])
            # Persistir consumo ANTES do POST impede replay apos timeout/crash.
            saved.pop("pending", None)
            self.store.save(saved)
            if callback.denied:
                raise SafeError("OAuth: autorizacao recusada no provedor. Execute authorize se quiser tentar novamente.")
            # Uma nova concessao pode invalidar tokens anteriores. Nao restaurar backups.
            saved.pop("tokens", None)
            saved["reauthorize_required"] = True
            self.store.save(saved)
            tokens = self._request_tokens({
                "grant_type": "authorization_code", "code": callback.code,
                "redirect_uri": self.config.redirect_uri, "code_verifier": pending["code_verifier"],
            })
            saved["tokens"] = tokens
            saved["reauthorize_required"] = False
            self.store.save(saved)
        logger.info("OAuth: autorizacao concluida; credenciais guardadas no cofre local.")

    def get_valid_access_token(self, *, force_refresh: bool = False) -> str:
        with self.store.locked():
            saved = self.store.load()
            tokens = saved.get("tokens")
            if saved.get("reauthorize_required") or not isinstance(tokens, dict):
                raise SafeError("OAuth: autorizacao necessaria. Execute authorize e depois exchange.")
            if tokens.get("client_id") != self.config.client_id:
                raise SafeError("OAuth: tokens pertencem a outra aplicacao; execute authorize novamente.")
            if (not _token(tokens.get("access_token")) or not _token(tokens.get("refresh_token"))
                    or not _number(tokens.get("expires_at")) or not _number(tokens.get("refresh_after"))
                    or tokens["refresh_after"] > tokens["expires_at"]):
                raise SafeError("OAuth: credenciais locais invalidas; execute authorize novamente.")
            if not force_refresh and self.clock() < min(tokens["refresh_after"], tokens["expires_at"]):
                return tokens["access_token"]
            old_refresh = tokens["refresh_token"]
            saved.pop("tokens", None)
            saved["reauthorize_required"] = True
            self.store.save(saved)
            # Nunca repetir refresh apos erro, timeout ou falha ao salvar a resposta.
            replacement = self._request_tokens({"grant_type": "refresh_token", "refresh_token": old_refresh},
                                               previous_refresh=old_refresh)
            saved["tokens"] = replacement
            saved["reauthorize_required"] = False
            self.store.save(saved)
        logger.info("OAuth: access token e refresh token renovados no cofre local.")
        return replacement["access_token"]


def get_valid_access_token(*, force_refresh: bool = False) -> str:
    """Para uso futuro pela fonte; nao inicia buscas nem publica mensagens."""
    return MercadoLivreAuth(load_oauth_config()).get_valid_access_token(force_refresh=force_refresh)


@dataclass(frozen=True)
class VerificationResult:
    http_status: int | None = None
    authenticated: bool = False


def verify_authentication() -> VerificationResult:
    """Somente /users/me. Mantem apenas status e confirmacao, sem dados da conta."""
    status = None
    try:
        token = get_valid_access_token()
        for attempt in range(2):
            with create_meli_session() as session:
                response = session.get(
                    USERS_ME_URL, headers={"Authorization": "Bearer " + token, "Accept": "application/json"},
                    timeout=API_TIMEOUT, allow_redirects=False,
                )
            try:
                status = response.status_code
                if status == 200:
                    account = response.json()
                    recognized = isinstance(account, dict) and type(account.get("id")) is int and account["id"] > 0
                    return VerificationResult(status, recognized)
                if status != 401 or attempt == 1:
                    return VerificationResult(status)
            finally:
                response.close()
            # Uma unica renovacao forcada para token revogado/expirado antecipadamente.
            # O cofre existente rotaciona o par e impede reutilizacao do refresh antigo.
            token = get_valid_access_token(force_refresh=True)
    except (SafeError, requests.RequestException, ValueError):
        pass
    return VerificationResult(status)


def _verify_command() -> int:
    previous_logging = logging.root.manager.disable
    previous_http_debug = http.client.HTTPConnection.debuglevel
    previous_https_debug = http.client.HTTPSConnection.debuglevel
    result = VerificationResult()
    try:
        # Nem logs de bibliotecas nem o debug do http.client podem imprimir headers.
        logging.disable(logging.CRITICAL)
        http.client.HTTPConnection.debuglevel = 0
        http.client.HTTPSConnection.debuglevel = 0
        result = verify_authentication()
    except Exception:
        # Excecoes inesperadas tambem nao podem expor argumentos ou respostas.
        pass
    finally:
        logging.disable(previous_logging)
        http.client.HTTPConnection.debuglevel = previous_http_debug
        http.client.HTTPSConnection.debuglevel = previous_https_debug
    print(json.dumps({"http_status": result.http_status, "authenticated": result.authenticated}))
    return 0 if result.authenticated else 1


def _diagnose_command() -> int:
    from services.mercado_livre_diagnose import diagnose, format_report
    previous_logging = logging.root.manager.disable
    previous_http_debug = http.client.HTTPConnection.debuglevel
    previous_https_debug = http.client.HTTPSConnection.debuglevel
    report = None
    try:
        # Inclui carregamento/renovação OAuth: bibliotecas nunca imprimem headers.
        logging.disable(logging.CRITICAL)
        http.client.HTTPConnection.debuglevel = 0
        http.client.HTTPSConnection.debuglevel = 0
        config = load_oauth_config()
        token = get_valid_access_token()
        protected = [os.environ.get(key, "") for key in (
            "TELEGRAM_BOT_TOKEN", "TELEGRAM_ADMIN_ID", "MELI_CLIENT_SECRET",
            "MELI_ACCESS_TOKEN", "MELI_REFRESH_TOKEN",
        )]
        # Somente leitura do cofre já usado pelo OAuth; valores servem só à redação.
        store = LocalOAuthStore()
        with store.locked():
            tokens = store.load().get("tokens", {})
            if isinstance(tokens, dict):
                protected.extend(tokens.get(key, "") for key in ("access_token", "refresh_token"))
        report = diagnose(config, token, protected=protected)
    except Exception:
        # Nunca exibir exceções que possam conter requisições/credenciais.
        pass
    finally:
        logging.disable(previous_logging)
        http.client.HTTPConnection.debuglevel = previous_http_debug
        http.client.HTTPSConnection.debuglevel = previous_https_debug
    if report is None:
        print("AUTO_PUBLISH=false\nTelegram: 0 mensagens\nClassificação: G) INCONCLUSIVO\n"
              "Diagnóstico não concluído. Falha ao preparar OAuth/cofre ou processar resposta; detalhes sensíveis omitidos.")
        return 1
    print(format_report(report))
    return 0 if report["item"].get("success") is True else 1


class _SafeParser(argparse.ArgumentParser):
    def error(self, message: str) -> None:
        # argparse normalmente inclui o argumento recebido, que pode conter code/state.
        raise SafeError("OAuth: argumentos invalidos; use --help. Valores omitidos.")


def _callback_from_prompt() -> str:
    # O modo interativo evita URL sensivel no historico do shell e na lista de processos.
    with warnings.catch_warnings():
        warnings.simplefilter("error", getpass.GetPassWarning)
        try:
            return getpass.getpass("Cole a URL completa da callback (entrada oculta): ")
        except (getpass.GetPassWarning, EOFError, OSError):
            raise SafeError("OAuth: entrada oculta indisponivel. Use um terminal interativo local.") from None


def main(argv: list[str] | None = None) -> int:
    parser = _SafeParser(description="OAuth Mercado Livre: authorize, exchange, refresh, verify e diagnose, sem exibir tokens.")
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("authorize", help="gera somente a URL oficial com PKCE e state")
    exchange = commands.add_parser("exchange", help="finaliza a autorizacao usando a callback")
    exchange.add_argument("--callback-url", help="callback completa; omita para entrada oculta recomendada")
    commands.add_parser("refresh", help="garante token valido, renovando somente se necessario")
    commands.add_parser("verify", help="consulta /users/me; exibe somente status HTTP e confirmacao de autenticacao")
    commands.add_parser("diagnose", help="diagnostica conta, aplicação, grant e HTTP 403 de items; sem Telegram")
    try:
        args = parser.parse_args(argv)
        if args.command == "verify":
            return _verify_command()
        if args.command == "diagnose":
            return _diagnose_command()
        config = load_oauth_config()
        configure_logging((config.client_secret,))
        auth = MercadoLivreAuth(config)
        if args.command == "authorize":
            print(auth.authorize())
        elif args.command == "exchange":
            auth.exchange(args.callback_url if args.callback_url is not None else _callback_from_prompt())
            print("Autorizacao concluida. Tokens guardados com seguranca.")
        else:
            auth.get_valid_access_token()
            print("Access token valido disponivel no cofre local. Nenhum token exibido.")
        return 0
    except SafeError as error:
        print(str(error), file=sys.stderr)
        return 1
    except KeyboardInterrupt:
        print("OAuth cancelado.", file=sys.stderr)
        return 130
    except Exception:
        print("OAuth: operacao interrompida; detalhes sensiveis omitidos. Confira o cofre e a configuracao local.", file=sys.stderr)
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
