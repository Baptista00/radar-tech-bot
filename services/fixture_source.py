"""Fonte exclusivamente local. Dados fictícios, sem sessão HTTP ou credenciais."""

import json
import re
from dataclasses import fields
from pathlib import Path

from models import OfferCandidate
from safety import SafeError
from services.offer_source import OfferBatch

FIXTURE_PATH = Path(__file__).resolve().parents[1] / "data" / "fixtures" / "offers.json"
FIXTURE_TERMS = ("mouse gamer", "headset gamer", "ssd nvme", "notebook", "monitor",
                 "teclado mecânico", "memória ram", "webcam", "microfone", "carregador",
                 "power bank", "hub usb", "placa de vídeo", "processador", "controle xbox", "setup")


class FixtureSource:
    source_id = "fixture"
    simulation_only = True

    def __init__(self, path: Path = FIXTURE_PATH):
        try:
            payload = json.loads(Path(path).read_text(encoding="utf-8"))
            if not isinstance(payload, dict) or payload.get("schema_version") != 1 or not isinstance(payload.get("offers"), list):
                raise ValueError()
            self._rows = []
            allowed = {field.name for field in fields(OfferCandidate)} - {"source", "external_id"}
            for row in payload["offers"]:
                if not isinstance(row, dict) or not re.fullmatch(r"FIXTURE-[A-Z0-9-]{1,80}", str(row.get("id", ""))):
                    raise ValueError()
                terms, history = row.get("terms"), row.get("history_prices", [])
                if (not isinstance(terms, list) or not terms or any(term not in FIXTURE_TERMS for term in terms)
                        or not isinstance(history, list) or len(history) > 365
                        or any(type(price) is not int or price <= 0 for price in history)):
                    raise ValueError()
                values = {key: value for key, value in row.items() if key in allowed}
                values.setdefault("price", None)
                values.setdefault("currency_id", "BRL")
                values["price_source"] = "fixture"
                values["item_id"] = row["id"]
                candidate = OfferCandidate(source="fixture", external_id=row["id"], **values)
                self._rows.append((candidate, tuple(terms), tuple(history)))
        except (OSError, ValueError, TypeError):
            raise SafeError("Fixture: arquivo local indisponível ou schema inválido; conteúdo omitido.") from None
        self.begin_cycle()

    def begin_cycle(self, stop_event=None):
        self._stop_event = stop_event

    def search(self, term: str, limit: int) -> OfferBatch:
        if type(limit) is not int or not 1 <= limit <= 100:
            raise SafeError("Fixture: limite deve ficar entre 1 e 100.")
        batch = OfferBatch(discovery="FIXTURE — dados e histórico simulados; links example.com; nenhuma oferta real.")
        if self._stop_event is not None and self._stop_event.is_set():
            batch.stop = True
            return batch
        batch.candidates = [candidate for candidate, terms, _ in self._rows if term in terms][:limit]
        batch.found_ids = {candidate.external_id for candidate in batch.candidates}
        return batch

    def history_samples(self):
        """Preços sintéticos anteriores, em ordem cronológica; persistência externa."""
        return [(candidate, terms[0], prices) for candidate, terms, prices in self._rows if prices]
