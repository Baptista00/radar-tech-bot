"""SQLite: histórico, avaliações, estado e reserva atômica de publicações."""

import json
import sqlite3
from contextlib import contextmanager
from datetime import datetime, time, timedelta
from pathlib import Path
from typing import Any, Iterator
from zoneinfo import ZoneInfo

from config import Config
from models import Product, ScoreResult, timestamp
from safety import SafeError, clean_text

SCHEMA = """
CREATE TABLE IF NOT EXISTS products (
    id INTEGER PRIMARY KEY,
    external_id TEXT NOT NULL,
    title TEXT NOT NULL,
    price INTEGER NOT NULL CHECK(price > 0),
    original_price INTEGER,
    discount_percentage REAL,
    rating REAL,
    sold_quantity INTEGER,
    url TEXT NOT NULL,
    image_url TEXT,
    source TEXT NOT NULL,
    brand TEXT,
    category TEXT,
    search_term TEXT,
    first_seen TEXT NOT NULL,
    last_seen TEXT NOT NULL,
    UNIQUE(source, external_id)
);
CREATE TABLE IF NOT EXISTS price_history (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price INTEGER NOT NULL,
    checked_at TEXT NOT NULL,
    UNIQUE(product_id, checked_at)
);
CREATE INDEX IF NOT EXISTS history_by_product ON price_history(product_id, checked_at);
CREATE TABLE IF NOT EXISTS search_runs (
    id INTEGER PRIMARY KEY,
    started_at TEXT NOT NULL,
    finished_at TEXT,
    status TEXT NOT NULL DEFAULT 'running',
    analyzed INTEGER NOT NULL DEFAULT 0,
    approved INTEGER NOT NULL DEFAULT 0,
    published INTEGER NOT NULL DEFAULT 0,
    simulated INTEGER NOT NULL DEFAULT 0
);
CREATE TABLE IF NOT EXISTS offer_evaluations (
    search_run_id INTEGER NOT NULL REFERENCES search_runs(id),
    product_id INTEGER NOT NULL REFERENCES products(id),
    score INTEGER NOT NULL,
    approved INTEGER NOT NULL,
    criteria TEXT NOT NULL,
    PRIMARY KEY(search_run_id, product_id)
);
CREATE TABLE IF NOT EXISTS publications (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price_when_published INTEGER NOT NULL,
    score INTEGER NOT NULL,
    published_at TEXT,
    telegram_message_id INTEGER,
    attempted_at TEXT NOT NULL,
    status TEXT NOT NULL CHECK(status IN ('pending', 'sent', 'failed', 'unknown')),
    title TEXT NOT NULL,
    url TEXT NOT NULL,
    search_term TEXT,
    error TEXT
);
CREATE INDEX IF NOT EXISTS publications_by_product ON publications(product_id, attempted_at);
CREATE UNIQUE INDEX IF NOT EXISTS message_id_unique ON publications(telegram_message_id)
    WHERE telegram_message_id IS NOT NULL;
CREATE TABLE IF NOT EXISTS simulations (
    id INTEGER PRIMARY KEY,
    product_id INTEGER NOT NULL REFERENCES products(id),
    price INTEGER NOT NULL,
    score INTEGER NOT NULL,
    simulated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS system_state (key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE IF NOT EXISTS recent_errors (
    id INTEGER PRIMARY KEY,
    occurred_at TEXT NOT NULL,
    description TEXT NOT NULL
);
"""


def day_bounds(now: datetime, timezone_name: str) -> tuple[str, str]:
    zone = ZoneInfo(timezone_name)
    today = now.astimezone(zone).date()
    start = datetime.combine(today, time.min, tzinfo=zone)
    end = datetime.combine(today + timedelta(days=1), time.min, tzinfo=zone)
    return timestamp(start), timestamp(end)


class Database:
    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with self.connect() as connection:
            connection.execute("PRAGMA journal_mode=WAL")
            connection.executescript(SCHEMA)
            connection.execute("PRAGMA user_version=1")

    @contextmanager
    def connect(self) -> Iterator[sqlite3.Connection]:
        connection = sqlite3.connect(self.path, timeout=10)
        connection.row_factory = sqlite3.Row
        connection.execute("PRAGMA foreign_keys=ON")
        try:
            with connection:
                yield connection
        finally:
            connection.close()

    @staticmethod
    def _get(connection: sqlite3.Connection, key: str, default: Any = None) -> Any:
        row = connection.execute("SELECT value FROM system_state WHERE key=?", (key,)).fetchone()
        return json.loads(row[0]) if row else default

    @staticmethod
    def _set(connection: sqlite3.Connection, key: str, value: Any) -> None:
        connection.execute(
            "INSERT INTO system_state VALUES (?, ?) ON CONFLICT(key) DO UPDATE SET value=excluded.value",
            (key, json.dumps(value, ensure_ascii=False)),
        )

    def get_state(self, key: str, default: Any = None) -> Any:
        with self.connect() as connection:
            return self._get(connection, key, default)

    def set_state(self, key: str, value: Any) -> None:
        with self.connect() as connection:
            self._set(connection, key, value)

    def recover_interrupted(self) -> None:
        with self.connect() as connection:
            pending = connection.execute(
                "UPDATE publications SET status='unknown', error=? WHERE status='pending'",
                ("Processo interrompido durante envio; conferir canal antes de reenviar.",),
            ).rowcount
            if pending:
                self._set(connection, "paused", True)
            connection.execute("UPDATE search_runs SET status='interrupted' WHERE status='running'")

    def start_search(self, now: datetime) -> int:
        with self.connect() as connection:
            self._set(connection, "last_search", timestamp(now))
            cursor = connection.execute("INSERT INTO search_runs(started_at) VALUES (?)", (timestamp(now),))
            return int(cursor.lastrowid)

    def finish_search(self, run_id: int, now: datetime, status: str, analyzed: int,
                      approved: int, published: int, simulated: int) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE search_runs SET finished_at=?, status=?, analyzed=?, approved=?, published=?, simulated=? WHERE id=?",
                (timestamp(now), status, analyzed, approved, published, simulated, run_id),
            )

    def observe(self, product: Product, now: datetime, history_days: int) -> tuple[int, list[int]]:
        with self.connect() as connection:
            row = connection.execute(
                "SELECT id FROM products WHERE source=? AND external_id=?",
                (product.source, product.external_id),
            ).fetchone()
            history = []
            if row:
                history = [r[0] for r in connection.execute(
                    "SELECT price FROM price_history WHERE product_id=? AND checked_at>=? AND checked_at<?",
                    (row[0], timestamp(now - timedelta(days=history_days)), timestamp(now)),
                )]
            values = (
                product.external_id, product.title, product.price, product.original_price,
                product.discount_percentage, product.rating, product.sold_quantity,
                product.url, product.image_url, product.source, product.brand,
                product.category, product.search_term, timestamp(now), timestamp(now),
            )
            connection.execute("""
                INSERT INTO products(external_id,title,price,original_price,discount_percentage,
                    rating,sold_quantity,url,image_url,source,brand,category,search_term,first_seen,last_seen)
                VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
                ON CONFLICT(source,external_id) DO UPDATE SET
                    title=excluded.title, price=excluded.price, original_price=excluded.original_price,
                    discount_percentage=excluded.discount_percentage, rating=excluded.rating,
                    sold_quantity=excluded.sold_quantity, url=excluded.url, image_url=excluded.image_url,
                    brand=excluded.brand, category=excluded.category, search_term=excluded.search_term,
                    last_seen=excluded.last_seen
            """, values)
            product_id = int(connection.execute(
                "SELECT id FROM products WHERE source=? AND external_id=?",
                (product.source, product.external_id),
            ).fetchone()[0])
            connection.execute("INSERT OR IGNORE INTO price_history(product_id,price,checked_at) VALUES (?,?,?)",
                               (product_id, product.price, timestamp(now)))
            return product_id, history

    def save_evaluation(self, run_id: int, product_id: int, result: ScoreResult, approved: bool) -> None:
        with self.connect() as connection:
            connection.execute("INSERT INTO offer_evaluations VALUES (?,?,?,?,?)",
                               (run_id, product_id, result.score, int(approved), json.dumps(result.criteria)))

    def _delivery_history(self, connection: sqlite3.Connection, product_id: int,
                          simulate: bool) -> tuple[list[sqlite3.Row], list[sqlite3.Row]]:
        rows = list(connection.execute(
            "SELECT product_id, price_when_published AS price, attempted_at AS stamp FROM publications "
            "WHERE status IN ('pending','sent','unknown')"
        ))
        if simulate:
            rows.extend(connection.execute("SELECT product_id,price,simulated_at AS stamp FROM simulations"))
        rows.sort(key=lambda row: row["stamp"], reverse=True)
        return rows, [row for row in rows if row["product_id"] == product_id]

    def reserve(self, product_id: int, product: Product, score: int, now: datetime,
                config: Config, *, simulate: bool) -> tuple[int | None, str]:
        """Limites e reserva no mesmo write lock: comandos e ciclos não furam cota."""
        with self.connect() as connection:
            connection.execute("BEGIN IMMEDIATE")
            if self._get(connection, "paused", False):
                return None, "publicações pausadas"
            if score < config.min_offer_score:
                return None, "score abaixo do mínimo"
            if connection.execute(
                "SELECT 1 FROM publications WHERE product_id=? AND status IN ('pending','unknown')",
                (product_id,),
            ).fetchone():
                return None, "envio anterior sem confirmação"
            not_before = self._get(connection, "send_not_before")
            if not_before and now < datetime.fromisoformat(not_before):
                return None, "aguardando limite informado pelo Telegram"
            all_rows, product_rows = self._delivery_history(connection, product_id, simulate)
            start, end = day_bounds(now, config.post_timezone)
            if sum(start <= row["stamp"] < end for row in all_rows) >= config.max_posts_per_day:
                return None, "limite diário atingido"
            if all_rows and now - datetime.fromisoformat(all_rows[0]["stamp"]) < timedelta(minutes=config.min_minutes_between_posts):
                return None, "intervalo mínimo entre publicações"
            if product_rows:
                last = product_rows[0]
                recent = now - datetime.fromisoformat(last["stamp"]) < timedelta(hours=config.republish_after_hours)
                significant_drop = product.price * 100 <= last["price"] * (100 - config.significant_price_drop_percent)
                if recent and not significant_drop:
                    return None, "produto publicado recentemente"
            if simulate:
                cursor = connection.execute(
                    "INSERT INTO simulations(product_id,price,score,simulated_at) VALUES (?,?,?,?)",
                    (product_id, product.price, score, timestamp(now)),
                )
            else:
                cursor = connection.execute(
                    "INSERT INTO publications(product_id,price_when_published,score,attempted_at,status,title,url,search_term) "
                    "VALUES (?,?,?,?,'pending',?,?,?)",
                    (product_id, product.price, score, timestamp(now), product.title, product.url, product.search_term),
                )
            return int(cursor.lastrowid), "reservado"

    def confirm_publication(self, publication_id: int, message_id: int, now: datetime) -> None:
        with self.connect() as connection:
            connection.execute(
                "UPDATE publications SET status='sent',published_at=?,telegram_message_id=? WHERE id=? AND status='pending'",
                (timestamp(now), message_id, publication_id),
            )

    def fail_publication(self, publication_id: int, error: SafeError, now: datetime) -> None:
        with self.connect() as connection:
            connection.execute("UPDATE publications SET status=?,error=? WHERE id=? AND status='pending'",
                               ("unknown" if error.uncertain else "failed", clean_text(str(error)), publication_id))
            if error.uncertain:
                self._set(connection, "paused", True)
            if error.retry_after:
                self._set(connection, "send_not_before", timestamp(now + timedelta(seconds=error.retry_after)))

    def add_error(self, description: str, now: datetime) -> None:
        with self.connect() as connection:
            connection.execute("INSERT INTO recent_errors(occurred_at,description) VALUES (?,?)",
                               (timestamp(now), clean_text(description)))
            connection.execute("DELETE FROM recent_errors WHERE id NOT IN (SELECT id FROM recent_errors ORDER BY id DESC LIMIT 20)")

    def recent_errors(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute("SELECT * FROM recent_errors ORDER BY id DESC LIMIT 5")]

    def recent_publications(self) -> list[dict[str, Any]]:
        with self.connect() as connection:
            return [dict(row) for row in connection.execute(
                "SELECT * FROM publications WHERE status='sent' ORDER BY published_at DESC LIMIT 5"
            )]

    def stats(self, now: datetime, timezone_name: str) -> dict[str, Any]:
        start, end = day_bounds(now, timezone_name)
        with self.connect() as connection:
            totals = dict(connection.execute(
                "SELECT COALESCE(SUM(analyzed),0) AS analyzed, COALESCE(SUM(approved),0) AS approved FROM search_runs"
            ).fetchone())
            totals.update(dict(connection.execute(
                "SELECT COUNT(*) AS publications, AVG(score) AS average_score FROM publications WHERE status='sent'"
            ).fetchone()))
            totals["products"] = connection.execute("SELECT COUNT(*) FROM products").fetchone()[0]
            totals["publications_today"] = connection.execute(
                "SELECT COUNT(*) FROM publications WHERE status='sent' AND published_at>=? AND published_at<?", (start, end)
            ).fetchone()[0]
            totals["simulations_today"] = connection.execute(
                "SELECT COUNT(*) FROM simulations WHERE simulated_at>=? AND simulated_at<?", (start, end)
            ).fetchone()[0]
            totals["uncertain"] = connection.execute(
                "SELECT COUNT(*) FROM publications WHERE status IN ('pending','unknown')"
            ).fetchone()[0]
            last = connection.execute("SELECT * FROM search_runs ORDER BY id DESC LIMIT 1").fetchone()
            totals["last_run"] = dict(last) if last else None
            totals["top_terms"] = [dict(row) for row in connection.execute(
                "SELECT search_term,COUNT(*) AS count FROM publications WHERE status='sent' AND search_term IS NOT NULL "
                "GROUP BY search_term HAVING COUNT(*)>=2 ORDER BY count DESC, search_term LIMIT 3"
            )] if totals["publications"] >= 5 else []
            return totals
