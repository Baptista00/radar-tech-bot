"""Telegram via requests. Nenhuma exceção ou resposta bruta sai desta camada."""

from typing import Any

import requests

from safety import SafeError


class TelegramService:
    def __init__(self, token: str, channel_id: str, *, auto_publish: bool = False):
        self._token = token
        self._channel_id = channel_id
        self._auto_publish = auto_publish

    def _call(self, method: str, data: dict[str, Any], *, timeout: int = 20,
              sending: bool = False) -> Any:
        try:
            response = requests.post(
                f"https://api.telegram.org/bot{self._token}/{method}",
                json=data, timeout=(5, timeout), allow_redirects=False,
            )
        except requests.Timeout:
            raise SafeError("Telegram: timeout; entrega não confirmada.", uncertain=sending) from None
        except requests.RequestException:
            raise SafeError("Telegram: falha de conexão; entrega não confirmada.", uncertain=sending) from None
        code = response.status_code
        try:
            payload = response.json()
        except ValueError:
            raise SafeError(f"Telegram HTTP {code}: resposta inválida.", status=code, uncertain=sending) from None
        if 200 <= code < 300 and isinstance(payload, dict) and payload.get("ok") is True:
            return payload.get("result")
        descriptions = {
            400: "requisição inválida; confira canal e permissões do bot",
            401: "bot não autorizado; confira o token",
            403: "acesso negado; confira permissões ou bloqueio do bot",
            409: "conflito de polling; outro processo ou webhook pode estar ativo",
            429: "limite de requisições atingido",
        }
        error_code = payload.get("error_code") if isinstance(payload, dict) else None
        if type(error_code) is not int:
            error_code = code
        retry_after = 0
        if isinstance(payload, dict) and isinstance(payload.get("parameters"), dict):
            value = payload["parameters"].get("retry_after")
            if type(value) is int:
                retry_after = max(0, min(value, 86400))
        description = descriptions.get(error_code, "resposta sem confirmação de sucesso")
        uncertain = sending and (code >= 500 or code < 400 or not isinstance(payload, dict))
        raise SafeError(f"Telegram HTTP {code}: {description}.", status=code,
                        uncertain=uncertain, retry_after=retry_after)

    def _send(self, chat_id: str | int, text: str, *, parse_mode: str | None = None,
              link_preview: bool = False) -> int:
        data = {"chat_id": chat_id, "text": text,
                "link_preview_options": {"is_disabled": not link_preview}}
        if parse_mode is not None:
            data["parse_mode"] = parse_mode
        result = self._call("sendMessage", data, sending=True)
        message_id = result.get("message_id") if isinstance(result, dict) else None
        if type(message_id) is not int or message_id <= 0:
            raise SafeError("Telegram: resposta sem ID de mensagem; entrega não confirmada.", uncertain=True)
        return message_id

    def publish_offer(self, text: str) -> int:
        if not self._auto_publish:
            raise SafeError("Publicação bloqueada: AUTO_PUBLISH=false.")
        return self._send(self._channel_id, text, parse_mode="HTML", link_preview=True)

    def reply(self, chat_id: int, text: str) -> int:
        return self._send(chat_id, text)

    def get_updates(self, offset: int | None) -> list[dict[str, Any]]:
        data: dict[str, Any] = {"timeout": 20, "limit": 50, "allowed_updates": ["message"]}
        if offset is not None:
            data["offset"] = offset
        result = self._call("getUpdates", data, timeout=30)
        if not isinstance(result, list):
            raise SafeError("Telegram: lista de atualizações inválida.")
        return [update for update in result if isinstance(update, dict)]
