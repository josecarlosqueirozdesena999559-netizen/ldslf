import json
from dataclasses import asdict

from config import DATA_DIR, HISTORY_FILE
from models.trade import TradeResult
from storage.supabase_store import SupabaseStore


class HistoryStore:
    def __init__(self) -> None:
        DATA_DIR.mkdir(parents=True, exist_ok=True)
        if not HISTORY_FILE.exists():
            HISTORY_FILE.write_text("[]", encoding="utf-8")
        self.supabase = SupabaseStore()

    def add(self, trade: TradeResult) -> None:
        if self.supabase.enabled:
            try:
                self.supabase.add_trade(trade)
            except Exception:
                pass
        data = self.all()
        data.append(asdict(trade))
        HISTORY_FILE.write_text(json.dumps(data, ensure_ascii=False, indent=2), encoding="utf-8")

    def all(self) -> list[dict]:
        if self.supabase.enabled:
            try:
                rows = self.supabase.all_trades()
                if rows:
                    return rows
            except Exception:
                pass
        try:
            return json.loads(HISTORY_FILE.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, FileNotFoundError):
            return []

    def summary(self) -> dict[str, float | int | dict | None]:
        rows = self.all()
        wins = sum(1 for row in rows if row.get("result") == "WIN")
        losses = sum(1 for row in rows if row.get("result") == "LOSS")
        profit = round(sum(float(row.get("profit", 0)) for row in rows), 2)
        return {"wins": wins, "losses": losses, "profit": profit, "last": rows[-1] if rows else None}
