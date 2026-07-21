from dataclasses import dataclass


@dataclass(slots=True)
class BotSettings:
    entry_value: float = 2.0
    stop_win: float = 20.0
    stop_loss: float = 20.0
    timeframe: str = "M1"
    payout_min: int = 80
    martingale_enabled: bool = True
    max_martingale: int = 1
    martingale_multiplier: float = 2.0
    asset_limit: int = 10
    scan_seconds: int = 3600
