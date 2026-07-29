from datetime import datetime

from models.asset import Asset
from models.candle import Candle
from models.trade import Signal


MOVING_AVERAGE_PERIOD = 21
CANDLE_LOOKBACK = 30
REVERSAL_WINDOW_SECONDS = 300
STRATEGY_PATTERN_MARKERS = ("estrategia 01", "estrategia 02")
STRATEGY_01_WATCH_TEXT = (
    "Estratégia 01 — Venda abaixo da MA21: no segundo 33, o último candle em tempo real deve "
    "estar negativo e com o preço abaixo do fechamento do candle anterior. Após essa validação, "
    "o robô aguarda o encerramento do mesmo candle e confirma se ele fechou abaixo da média móvel "
    "de 21 períodos. A entrada PUT é realizada na abertura do candle seguinte, com no máximo uma "
    "reentrada. Nenhuma operação é permitida se o candle fechar sobre ou acima da MA21."
)


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


def detect_strategy_01_ma21_reversal_after_33(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < MOVING_AVERAGE_PERIOD:
        return None, f"Aguardando {MOVING_AVERAGE_PERIOD} candles fechados para calcular a MA21", None

    current = asset.current_candle
    if current is None or current.closed:
        return None, "Estrategia 01 aguardando o ultimo candle em tempo real", None

    setup = closed[-1]
    if int(current.timestamp) <= int(setup.timestamp):
        return None, "Estrategia 01 aguardando a abertura do proximo candle", candle_color(setup)

    ma21 = moving_average_at(closed, len(closed) - 1)
    if ma21 is None:
        return None, "Aguardando calculo da MA21", candle_color(setup)

    if not setup.negative_at_33:
        return None, "Estrategia 01: no segundo 33, candle nao estava negativo e abaixo do fechamento anterior", candle_color(setup)
    if setup.close >= ma21:
        return None, "Estratégia 01 bloqueada: o candle não fechou abaixo da MA21", candle_color(setup)

    return (
        "PUT",
        "Estrategia 01: candle negativo e abaixo do anterior aos 33s, com fechamento abaixo da MA21; PUT na abertura do proximo candle com uma reentrada se necessario",
        candle_color(setup),
    )


def describe_strategy_watch(asset: Asset) -> str:
    _direction_01, reason_01, _color_01 = detect_strategy_01_ma21_reversal_after_33(asset)
    _direction_02, reason_02, _color_02 = detect_strategy_02_above_ma21_after_33(asset)
    return f"Estratégia 01: {reason_01} | Estratégia 02: {reason_02}"


def detect_strategy_02_above_ma21_after_33(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < MOVING_AVERAGE_PERIOD:
        return None, f"Aguardando {MOVING_AVERAGE_PERIOD} candles fechados para calcular a MA21", None

    current = asset.current_candle
    if current is None or current.closed:
        return None, "Estrategia 02 aguardando o ultimo candle em tempo real", None

    setup = closed[-1]
    if int(current.timestamp) <= int(setup.timestamp):
        return None, "Estrategia 02 aguardando a abertura do proximo candle", candle_color(setup)

    previous = closed[-2] if len(closed) >= 2 else None
    ma21 = moving_average_at(closed, len(closed) - 1)
    if previous is None or ma21 is None:
        return None, "Estrategia 02 aguardando historico para validacao", candle_color(setup)
    if setup.price_at_33 is None:
        return None, "Estrategia 02: preco do segundo 33 nao foi confirmado", candle_color(setup)
    if setup.price_at_33 <= previous.close:
        return None, "Estrategia 02: no segundo 33, preco nao estava acima do candle anterior", candle_color(setup)
    if candle_color(setup) != "GREEN":
        return None, "Estrategia 02: candle nao fechou verde", candle_color(setup)
    if setup.close <= ma21:
        return None, "Estrategia 02 bloqueada: candle nao fechou acima da MA21", candle_color(setup)
    if setup.close <= previous.close:
        return None, "Estrategia 02: candle nao fechou acima do candle anterior", candle_color(setup)
    if setup.close <= setup.price_at_33:
        return None, "Estrategia 02: fechamento nao ficou acima do preco registrado no segundo 33", candle_color(setup)

    return (
        "CALL",
        "Estrategia 02: acima do candle anterior aos 33s; fechamento verde acima da MA21, do candle anterior e do preco dos 33s; CALL no proximo candle com G1 automatico em caso de LOSS",
        candle_color(setup),
    )


def collect_strategy_signals(asset: Asset) -> list[Signal]:
    signals: list[Signal] = []
    detections = (
        (detect_strategy_01_ma21_reversal_after_33, True),
        (detect_strategy_02_above_ma21_after_33, True),
    )
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
