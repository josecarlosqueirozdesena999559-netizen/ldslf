from dataclasses import dataclass
from datetime import datetime


@dataclass(slots=True)
class Signal:
    asset: str
    active_id: int
    payout: int
    pattern: str
    direction: str
    sequence_color: str
    timestamp: datetime
    strategy_window_seconds: int = 300
    max_entries: int = 0
    entry_second: int | None = None


@dataclass(slots=True)
class TradeResult:
    timestamp: str
    asset: str
    direction: str
    payout: int
    value: float
    attempt: str
    result: str
    profit: float
    balance_before: float
    balance_after: float
    account_mode: str
