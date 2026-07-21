from __future__ import annotations

import json
import sys
from pathlib import Path


BASE_DIR = Path(__file__).resolve().parents[1]
DATA_DIR = BASE_DIR / "data"
sys.path.insert(0, str(BASE_DIR))

from models.trade import TradeResult
from storage.supabase_store import SupabaseStore


def load_json(path: Path, fallback):
    try:
        return json.loads(path.read_text(encoding="utf-8-sig"))
    except Exception:
        return fallback


def main() -> None:
    store = SupabaseStore()
    if not store.enabled:
        raise SystemExit("SUPABASE_URL/SUPABASE_ACCESS_TOKEN nao configurados.")

    settings = load_json(DATA_DIR / "web_settings.json", {})
    if isinstance(settings, dict) and settings:
        store.save_settings(settings)

    score = load_json(DATA_DIR / "session_score.json", {})
    if isinstance(score, dict):
        store.save_session_score(score)

    manual_entries = load_json(DATA_DIR / "manual_entries.json", [])
    if isinstance(manual_entries, list):
        store.save_manual_entries([entry for entry in manual_entries if isinstance(entry, dict)])

    history = load_json(DATA_DIR / "history.json", [])
    if isinstance(history, list):
        store.query("truncate table public.trade_history restart identity;")
        trades: list[TradeResult] = []
        for row in history:
            if not isinstance(row, dict):
                continue
            try:
                trades.append(TradeResult(**row))
            except TypeError:
                continue
        store.add_trades_bulk(trades)

    print("Migracao Supabase concluida.")


if __name__ == "__main__":
    main()
