"""Painel privado autenticado pelo ID numérico de message.from (não username)."""

from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from config import Config
from database import Database
from engine import OfferEngine
from models import utcnow
from safety import clean_text
from services.offer_formatter import format_price
from telegram_service import TelegramService


class AdminBot:
    def __init__(self, config: Config, database: Database, engine: OfferEngine,
                 telegram: TelegramService, *, search_enabled: bool = True):
        self.config = config
        self.database = database
        self.engine = engine
        self.telegram = telegram
        self.search_enabled = search_enabled

    def handle_update(self, update: dict[str, Any]) -> None:
        message = update.get("message")
        if not isinstance(message, dict):
            return
        sender, chat, text = message.get("from"), message.get("chat"), message.get("text")
        if not isinstance(sender, dict) or not isinstance(chat, dict) or not isinstance(text, str):
            return
        user_id, chat_id = sender.get("id"), chat.get("id")
        if type(user_id) is not int or type(chat_id) is not int or not text.startswith("/"):
            return
        command = text.split()[0].split("@")[0].lower()
        if chat.get("type") != "private":
            self.telegram.reply(chat_id, "Não autorizado.")
            return
        if command == "/whoami":
            self.telegram.reply(chat_id, str(user_id))
            return
        if self.config.telegram_admin_id is None or user_id != self.config.telegram_admin_id:
            self.telegram.reply(chat_id, "Não autorizado.")
            return
        handlers = {
            "/status": self.status, "/ultimas": self.latest, "/stats": self.stats,
            "/pause": self.pause, "/resume": self.resume, "/buscar": self.search,
        }
        handler = handlers.get(command)
        answer = handler() if handler else "Comandos: /status /ultimas /stats /pause /resume /buscar /whoami"
        self.telegram.reply(chat_id, answer)

    def _date(self, value: str | None) -> str:
        if not value:
            return "ainda não registrada"
        return datetime.fromisoformat(value).astimezone(ZoneInfo(self.config.post_timezone)).strftime("%d/%m %H:%M")

    def status(self) -> str:
        stats = self.database.stats(utcnow(), self.config.post_timezone)
        paused = self.database.get_state("paused", False)
        last = stats["last_run"]
        errors = self.database.recent_errors()
        lines = [
            "Radar Tech — status",
            f"Sistema: {'online' if self.database.get_state('online', False) else 'offline'}",
            f"Buscador: {'online' if self.engine.online else 'offline'}",
            f"Automação: {'pausada' if paused else 'ativa'}",
            f"Modo: {'publicação real' if self.config.auto_publish else 'simulação (AUTO_PUBLISH=false)'}",
            f"Última busca: {self._date(self.database.get_state('last_search'))}",
            f"Próxima busca: {self._date(self.database.get_state('next_search'))}",
            f"Produtos analisados (última busca): {last['analyzed'] if last else 0}",
            f"Ofertas encontradas (última busca): {last['approved'] if last else 0}",
            f"Publicações hoje: {stats['publications_today']}/{self.config.max_posts_per_day}",
            f"Simulações hoje: {stats['simulations_today']}",
            f"Envios sem confirmação: {stats['uncertain']}",
            "Erros recentes:",
        ]
        lines.extend(f"- {self._date(error['occurred_at'])}: {error['description']}" for error in errors)
        if not errors:
            lines.append("nenhum")
        return "\n".join(lines)

    def latest(self) -> str:
        publications = self.database.recent_publications()
        if not publications:
            return "Nenhuma oferta publicada. Simulações não contam como publicação."
        lines = ["Últimas 5 ofertas publicadas:"]
        for item in publications:
            lines.append(f"\n{clean_text(item['title'], 160)}\n{format_price(item['price_when_published'])} | Score {item['score']}\n"
                         f"{self._date(item['published_at'])}\n{item['url']}")
        return "\n".join(lines)

    def stats(self) -> str:
        stats = self.database.stats(utcnow(), self.config.post_timezone)
        average = f"{stats['average_score']:.1f}" if stats["average_score"] is not None else "sem publicações"
        top = ", ".join(f"{clean_text(row['search_term'], 80)} ({row['count']})" for row in stats["top_terms"])
        return (f"Produtos analisados (observações): {stats['analyzed']}\n"
                f"Produtos únicos: {stats['products']}\nTotal de publicações: {stats['publications']}\n"
                f"Média de score publicado: {average}\nTermos mais publicados: {top or 'dados insuficientes'}")

    def pause(self) -> str:
        self.database.set_state("paused", True)
        return "Novas publicações pausadas. Buscas e histórico continuam; envio já em andamento pode concluir."

    def resume(self) -> str:
        self.database.set_state("paused", False)
        mode = "simulação" if not self.config.auto_publish else "publicação real"
        return f"Automação retomada no modo {mode}."

    def search(self) -> str:
        if not self.search_enabled:
            return "Busca desativada nesta sessão de validação do painel."
        if self.engine.request_search():
            return "Busca solicitada. Consulte /status para acompanhar o resultado."
        return "Já existe busca em andamento ou na fila."
