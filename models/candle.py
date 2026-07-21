from dataclasses import dataclass
from datetime import datetime, timedelta, timezone


@dataclass(slots=True)
class Candle:
    open: float
    close: float
    high: float
    low: float
    timestamp: int
    closed: bool = True
    update_timestamp: int | None = None
    asset: str = ""
    negative_at_33: bool = False
    positive_at_33: bool = False

    @property
    def time(self) -> datetime:
        return self._to_bullex_time(self.timestamp)

    @property
    def update_time(self) -> datetime:
        timestamp = self.update_timestamp or self.timestamp
        return self._to_bullex_time(timestamp)

    @staticmethod
    def _to_bullex_time(timestamp: int) -> datetime:
        try:
            value = int(timestamp)
            if value > 10_000_000_000:
                value = value // 1000
            return datetime.fromtimestamp(value, timezone.utc) - timedelta(hours=3)
        except (OSError, OverflowError, ValueError):
            return datetime.now(timezone.utc) - timedelta(hours=3)
