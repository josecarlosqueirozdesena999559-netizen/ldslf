from dataclasses import dataclass, field

from models.candle import Candle


@dataclass(slots=True)
class Asset:
    name: str
    active_id: int
    payout: int
    open: bool = True
    candles: list[Candle] = field(default_factory=list)
    sequence: str = "-"
    signal: str = "-"

    @property
    def current_candle(self) -> Candle | None:
        return self.candles[-1] if self.candles else None
