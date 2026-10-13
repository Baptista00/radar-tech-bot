"""Contrato de saída e transporte offline, que não possui cliente HTTP."""

from typing import Protocol

from safety import SafeError


class OfferPublisher(Protocol):
    def publish_offer(self, text: str) -> int: ...

    def reply(self, chat_id: int, text: str) -> int: ...


class OfflinePublisher:
    def publish_offer(self, text: str) -> int:
        raise SafeError("Publicação bloqueada: modo fixture offline.")

    def reply(self, chat_id: int, text: str) -> int:
        raise SafeError("Telegram bloqueado: modo fixture offline.")
