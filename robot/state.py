from dataclasses import dataclass, field

from models.asset import Asset
from models.trade import Signal


@dataclass(slots=True)
class RobotState:
    running: bool = False
    assets: list[Asset] = field(default_factory=list)
    last_signal: Signal | None = None
    focused_asset: str | None = None
    status: str = "Parado"
