from datetime import datetime

from models.asset import Asset
from models.candle import Candle
from models.trade import Signal


MOVING_AVERAGE_PERIOD = 21
CANDLE_LOOKBACK = 30
REVERSAL_WINDOW_SECONDS = 300
STRATEGY_PATTERN_MARKERS = ("estrategia 01",)


def make_signal(
    asset: Asset,
    direction: str,
    pattern: str,
    sequence_color: str | None,
    window_seconds: int,
    max_entries: int = 2,
    enter_on_signal: bool = False,
) -> Signal:
    return Signal(
        asset=asset.name,
        active_id=asset.active_id,
        payout=asset.payout,
        pattern=pattern,
        direction=direction,
        sequence_color=sequence_color or "-",
        timestamp=datetime.now(),
        strategy_window_seconds=window_seconds,
        max_entries=max_entries,
        enter_on_signal=enter_on_signal,
    )


def candle_color(candle: Candle) -> str:
    if candle.close > candle.open:
        return "GREEN"
    if candle.close < candle.open:
        return "RED"
    return "DOJI"


def moving_average(candles: list[Candle], period: int = MOVING_AVERAGE_PERIOD) -> float | None:
    closed = [candle for candle in candles if candle.closed]
    if len(closed) < period:
        return None
    return sum(candle.close for candle in closed[-period:]) / period


def moving_average_at(candles: list[Candle], index: int, period: int = MOVING_AVERAGE_PERIOD) -> float | None:
    if index < period - 1:
        return None
    return sum(candle.close for candle in candles[index - period + 1 : index + 1]) / period


def moving_average_slope_at(candles: list[Candle], index: int, period: int = MOVING_AVERAGE_PERIOD) -> float | None:
    current = moving_average_at(candles, index, period)
    previous = moving_average_at(candles, index - 1, period)
    if current is None or previous is None:
        return None
    return current - previous


def candle_close_second(candle: Candle) -> int:
    timestamp = candle.update_timestamp or candle.timestamp
    try:
        elapsed = int(timestamp) - int(candle.timestamp)
        if 0 <= elapsed < 60:
            return elapsed
        return int(timestamp) % 60
    except (TypeError, ValueError):
        return candle.update_time.second


def moving_average_snapshot(asset: Asset, period: int = MOVING_AVERAGE_PERIOD) -> dict:
    closed = [candle for candle in asset.candles if candle.closed]
    current = moving_average(asset.candles, period)
    previous = None
    if len(closed) > period:
        previous = sum(candle.close for candle in closed[-period - 1 : -1]) / period

    last = closed[-1] if closed else asset.current_candle
    close = last.close if last else None
    distance = close - current if close is not None and current is not None else None
    slope = current - previous if current is not None and previous is not None else None
    trend = "AGUARDANDO" if slope is None else "SUBINDO" if slope > 0 else "DESCENDO" if slope < 0 else "LATERAL"
    position = "AGUARDANDO"
    if distance is not None:
        position = "ACIMA" if distance > 0 else "ABAIXO" if distance < 0 else "NA MEDIA"
    return {
        "period": period,
        "ready": current is not None,
        "value": current,
        "previous": previous,
        "slope": slope,
        "trend": trend,
        "close": close,
        "distance": distance,
        "position": position,
        "candles": len(closed),
    }


def describe_latest_sequence(asset: Asset) -> tuple[str | None, int, str]:
    candles = [candle for candle in asset.candles if candle.closed]
    if not candles:
        return None, 0, "Aguardando"
    last_color = candle_color(candles[-1])
    if last_color == "DOJI":
        return "DOJI", 1, "DOJI"
    count = 0
    for candle in reversed(candles):
        if candle_color(candle) != last_color:
            break
        count += 1
    label = "verdes" if last_color == "GREEN" else "vermelhos"
    return last_color, count, f"{count} {label}"


def detect_strategy_01_ma21_reversal_after_33(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < MOVING_AVERAGE_PERIOD:
        return None, f"Aguardando {MOVING_AVERAGE_PERIOD} candles fechados para calcular a MA21", None

    current = asset.current_candle
    if current is None or current.closed:
        return None, "Estratégia 01 aguardando o candle atual em tempo real", None
    previous = closed[-1]
    if int(current.timestamp) <= int(previous.timestamp):
        return None, "Estratégia 01 aguardando o candle seguinte", candle_color(current)
    ma21 = moving_average_at(closed, len(closed) - 1)
    if ma21 is None:
        return None, "Aguardando o cálculo da MA21", candle_color(current)
    if candle_close_second(current) < 33:
        return None, "Estratégia 01 aguardando o segundo 33", candle_color(current)
    if current.close <= ma21:
        return None, "Estratégia 01 sem entrada: preço atual abaixo da MA21", candle_color(current)
    if current.close == previous.close:
        return None, "Estratégia 01 sem entrada: preço igual ao fechamento anterior", candle_color(current)

    direction = "PUT" if current.close > previous.close else "CALL"
    movement = "acima" if direction == "PUT" else "abaixo"
    return (
        direction,
        f"Estrategia 01: após 33s, preço acima da MA21 e {movement} do fechamento "
        f"anterior; {'venda PUT' if direction == 'PUT' else 'compra CALL'} imediata "
        "com apenas um G1 se necessário",
        candle_color(current),
    )


def describe_strategy_watch(asset: Asset) -> str:
    _direction, reason, _color = detect_strategy_01_ma21_reversal_after_33(asset)
    return reason


def collect_strategy_signals(asset: Asset) -> list[Signal]:
    signals: list[Signal] = []
    detections = ((detect_strategy_01_ma21_reversal_after_33, True),)
    for detector, enter_on_signal in detections:
        direction, pattern, sequence_color = detector(asset)
        if direction:
            signals.append(
                make_signal(
                    asset,
                    direction,
                    pattern,
                    sequence_color,
                    REVERSAL_WINDOW_SECONDS,
                    max_entries=2,
                    enter_on_signal=enter_on_signal,
                )
            )
    return signals


def is_allowed_strategy_signal(signal: Signal) -> bool:
    pattern = (signal.pattern or "").lower()
    return any(marker in pattern for marker in STRATEGY_PATTERN_MARKERS)


def generate_signal(asset: Asset) -> Signal | None:
    _latest_color, latest_count, latest_sequence = describe_latest_sequence(asset)
    asset.sequence = latest_sequence if latest_count else "Aguardando"
    signals = collect_strategy_signals(asset)
    if not signals:
        asset.signal = describe_strategy_watch(asset)
        return None
    asset.signal = " | ".join(f"{signal.direction}: {signal.pattern}" for signal in signals)
    return signals[0]
