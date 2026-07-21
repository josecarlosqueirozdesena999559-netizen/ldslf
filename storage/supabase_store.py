from __future__ import annotations

import json
import os
from dataclasses import asdict
from typing import Any
from urllib.parse import urlparse

import requests
from dotenv import load_dotenv

from models.trade import TradeResult


class SupabaseStore:
    def __init__(self) -> None:
        load_dotenv()
        self.url = os.getenv("SUPABASE_URL", "").strip().rstrip("/")
        self.project_ref = os.getenv("SUPABASE_PROJECT_REF", "").strip() or self._project_ref_from_url(self.url)
        self.access_token = os.getenv("SUPABASE_ACCESS_TOKEN", "").strip()
        self.timeout = 15

    @property
    def enabled(self) -> bool:
        return bool(self.project_ref and self.access_token)

    @staticmethod
    def _project_ref_from_url(value: str) -> str:
        if not value:
            return ""
        hostname = urlparse(value).hostname or ""
        return hostname.split(".", 1)[0] if hostname.endswith(".supabase.co") else ""

    def query(self, sql: str) -> list[dict[str, Any]]:
        if not self.enabled:
            return []
        response = requests.post(
            f"https://api.supabase.com/v1/projects/{self.project_ref}/database/query",
            headers={
                "Authorization": f"Bearer {self.access_token}",
                "Content-Type": "application/json",
            },
            json={"query": sql},
            timeout=self.timeout,
        )
        response.raise_for_status()
        data = response.json()
        if isinstance(data, list):
            return data
        value = data.get("value", [])
        return value if isinstance(value, list) else []

    @staticmethod
    def sql_json(value: Any) -> str:
        text = json.dumps(value, ensure_ascii=False)
        return "'" + text.replace("'", "''") + "'::jsonb"

    @staticmethod
    def sql_text(value: Any) -> str:
        return "'" + str(value).replace("'", "''") + "'"

    def load_settings(self) -> dict[str, Any] | None:
        rows = self.query("select data from public.bot_settings where id = 'default' limit 1;")
        return rows[0].get("data") if rows and isinstance(rows[0].get("data"), dict) else None

    def save_settings(self, data: dict[str, Any]) -> None:
        self.query(
            "insert into public.bot_settings (id, data, updated_at) "
            f"values ('default', {self.sql_json(data)}, now()) "
            "on conflict (id) do update set data = excluded.data, updated_at = now();"
        )

    def load_manual_entries(self) -> list[dict[str, Any]] | None:
        rows = self.query("select data from public.manual_entries order by created_at asc;")
        entries = [row.get("data") for row in rows if isinstance(row.get("data"), dict)]
        return entries

    def save_manual_entries(self, entries: list[dict[str, Any]]) -> None:
        self.query("delete from public.manual_entries;")
        for entry in entries:
            entry_id = str(entry.get("id") or "")
            if not entry_id:
                continue
            status = str(entry.get("status") or "")
            self.query(
                "insert into public.manual_entries (id, status, data, updated_at) "
                f"values ({self.sql_text(entry_id)}, {self.sql_text(status)}, {self.sql_json(entry)}, now()) "
                "on conflict (id) do update set status = excluded.status, data = excluded.data, updated_at = now();"
            )

    def load_session_score(self) -> dict[str, Any] | None:
        rows = self.query("select wins, losses, profit, results, last_green_time from public.session_score where id = 'default' limit 1;")
        return rows[0] if rows else None

    def save_session_score(self, data: dict[str, Any]) -> None:
        wins = int(data.get("wins", 0) or 0)
        losses = int(data.get("losses", 0) or 0)
        profit = float(data.get("profit", 0) or 0)
        last_green_time = str(data.get("last_green_time") or "-")
        results = data.get("results", [])
        self.query(
            "insert into public.session_score (id, wins, losses, profit, results, last_green_time, updated_at) "
            f"values ('default', {wins}, {losses}, {profit}, {self.sql_json(results)}, {self.sql_text(last_green_time)}, now()) "
            "on conflict (id) do update set wins = excluded.wins, losses = excluded.losses, profit = excluded.profit, "
            "results = excluded.results, last_green_time = excluded.last_green_time, updated_at = now();"
        )

    def add_trade(self, trade: TradeResult) -> None:
        data = asdict(trade)
        self.query(
            "insert into public.trade_history (asset, direction, result, profit, data) "
            f"values ({self.sql_text(trade.asset)}, {self.sql_text(trade.direction)}, {self.sql_text(trade.result)}, "
            f"{float(trade.profit or 0)}, {self.sql_json(data)});"
        )

    def add_trades_bulk(self, trades: list[TradeResult], chunk_size: int = 100) -> None:
        for index in range(0, len(trades), chunk_size):
            values = []
            for trade in trades[index : index + chunk_size]:
                data = asdict(trade)
                values.append(
                    "("
                    f"{self.sql_text(trade.asset)}, "
                    f"{self.sql_text(trade.direction)}, "
                    f"{self.sql_text(trade.result)}, "
                    f"{float(trade.profit or 0)}, "
                    f"{self.sql_json(data)}"
                    ")"
                )
            if values:
                self.query(
                    "insert into public.trade_history (asset, direction, result, profit, data) values "
                    + ", ".join(values)
                    + ";"
                )

    def all_trades(self) -> list[dict[str, Any]]:
        rows = self.query("select data from public.trade_history order by created_at asc, id asc;")
        return [row.get("data") for row in rows if isinstance(row.get("data"), dict)]
