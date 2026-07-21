from datetime import datetime

from models.asset import Asset
from models.candle import Candle
from models.trade import Signal

MOVING_AVERAGE_PERIOD = 21
CANDLE_LOOKBACK = 30
TREND_SEQUENCE_MIN = 8
REVERSAL_CONFIRMATION_CANDLES = 2
CONTINUATION_CONFIRMATION_CANDLES = 3
REVERSAL_WINDOW_SECONDS = 300
CONTINUATION_WINDOW_SECONDS = 600
MA21_WICKLESS_WINDOW_SECONDS = 600
MA21_GREEN_BUY_WINDOW_SECONDS = 300
NEGATIVE_33_GREEN_CLOSE_WINDOW_SECONDS = 300
STRATEGY_PATTERN_MARKERS = (
    "operar contra nas velas 3, 4 e 5",
    "operar contra nas velas 4, 5 e 6",
    "operar contra tendencia nas velas 5, 6 e 7",
    "comprar no segundo 33",
    "negativo aos 33s e fechou verde positivo",
    "positivo aos 33s e fechou vermelho negativo",
)


def make_signal(
    asset: Asset,
    direction: str,
    pattern: str,
    sequence_color: str | None,
    window_seconds: int,
    max_entries: int = 3,
    entry_second: int | None = None,
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
        entry_second=entry_second,
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
    closes = [candle.close for candle in closed[-period:]]
    return sum(closes) / period


def moving_average_at(candles: list[Candle], index: int, period: int = MOVING_AVERAGE_PERIOD) -> float | None:
    if index < period - 1:
        return None
    closes = [candle.close for candle in candles[index - period + 1 : index + 1]]
    return sum(closes) / period


def candle_close_second(candle: Candle) -> int:
    timestamp = candle.update_timestamp or candle.timestamp
    try:
        return int(timestamp) % 60
    except (TypeError, ValueError):
        return candle.time.second


def is_wickless(candle: Candle) -> bool:
    body = abs(candle.close - candle.open)
    if body <= 0:
        return False
    tolerance = max(body * 0.02, 0.0000001)
    upper_wick = abs(candle.high - max(candle.open, candle.close))
    lower_wick = abs(min(candle.open, candle.close) - candle.low)
    return upper_wick <= tolerance and lower_wick <= tolerance


def moving_average_snapshot(asset: Asset, period: int = MOVING_AVERAGE_PERIOD) -> dict:
    closed = [candle for candle in asset.candles if candle.closed]
    current = moving_average(asset.candles, period)
    previous = None
    if len(closed) > period:
        previous_closes = [candle.close for candle in closed[-period - 1 : -1]]
        previous = sum(previous_closes) / period

    last = closed[-1] if closed else asset.current_candle
    close = last.close if last else None
    distance = close - current if close is not None and current is not None else None
    slope = current - previous if current is not None and previous is not None else None
    if slope is None:
        trend = "AGUARDANDO"
    elif slope > 0:
        trend = "SUBINDO"
    elif slope < 0:
        trend = "DESCENDO"
    else:
        trend = "LATERAL"
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


def describe_strategy_watch(asset: Asset) -> str:
    closed = [candle for candle in asset.candles if candle.closed]
    if not closed:
        return "Aguardando candles"

    color, count, sequence = describe_latest_sequence(asset)
    if count >= TREND_SEQUENCE_MIN:
        return f"Padrao 8 ativo: {sequence}"
    if count >= TREND_SEQUENCE_MIN - 2:
        return f"Perto dos 8: {sequence}"

    colors = [candle_color(candle) for candle in closed]
    if len(colors) >= TREND_SEQUENCE_MIN + 1:
        reversal_color = colors[-1]
        if reversal_color != "DOJI":
            reversal_count = 0
            for item in reversed(colors):
                if item != reversal_color:
                    break
                reversal_count += 1
            previous_color = "RED" if reversal_color == "GREEN" else "GREEN"
            previous_count = 0
            for item in reversed(colors[:-reversal_count]):
                if item != previous_color:
                    break
                previous_count += 1
            if previous_count >= TREND_SEQUENCE_MIN and reversal_count == 1:
                return "Reversao 1/2 - perto da entrada 3/4/5"

    ma21_status = describe_ma21_watch(closed)
    if ma21_status:
        return ma21_status
    negative_33_status = describe_negative_33_green_close_watch(asset)
    if negative_33_status:
        return negative_33_status
    return "Analisando"


def describe_ma21_watch(closed: list[Candle]) -> str | None:
    if len(closed) < MOVING_AVERAGE_PERIOD:
        return None

    last = closed[-1]
    last_index = len(closed) - 1
    ma21 = moving_average_at(closed, last_index)
    if ma21 is None:
        return None

    if candle_color(last) == "GREEN" and last.close > ma21:
        previous_three = [candle_color(candle) for candle in closed[-4:-1]]
        if previous_three != ["GREEN", "GREEN", "GREEN"]:
            if candle_close_second(last) > 33:
                return "Compra no 33 armada"
            return "Verde acima MA21 - aguardando fechar apos 33s"

    if len(closed) >= MOVING_AVERAGE_PERIOD + 1:
        for green_count in range(1, 4):
            size = green_count + 1
            if len(closed) < size:
                continue
            tail = closed[-size:]
            colors = [candle_color(candle) for candle in tail]
            if colors == ["RED"] + ["GREEN"] * green_count:
                anchor_index = len(closed) - size
                anchor = closed[anchor_index]
                anchor_ma21 = moving_average_at(closed, anchor_index)
                if (
                    anchor_ma21 is not None
                    and is_wickless(anchor)
                    and candle_close_second(anchor) <= 33
                    and anchor.close < anchor_ma21
                ):
                    return f"MA21 contra: {green_count}/4 verdes"
    return None


def previous_same_color_count(candles: list[Candle], end_index: int) -> tuple[str | None, int]:
    if end_index <= 0:
        return None, 0
    color = candle_color(candles[end_index - 1])
    if color == "DOJI":
        return color, 1
    count = 0
    for candle in reversed(candles[:end_index]):
        if candle_color(candle) != color:
            break
        count += 1
    return color, count


def describe_negative_33_green_close_watch(asset: Asset) -> str | None:
    closed = [candle for candle in asset.candles if candle.closed]
    current = asset.current_candle
    if current and not current.closed:
        color, count = previous_same_color_count(closed, len(closed))
        if count >= 3:
            if getattr(current, "negative_at_33", False):
                return f"33 negativo marcado apos {count} candles da mesma cor"
            if getattr(current, "positive_at_33", False):
                return f"33 positivo marcado apos {count} candles da mesma cor"
            current_second = int(current.update_timestamp or current.timestamp) - int(current.timestamp)
            if current_second < 33:
                return f"Aguardando 33s apos {count} candles da mesma cor"
            if current.close < current.open:
                return f"Candle negativo aos 33s apos {count} candles da mesma cor"
            if current.close > current.open:
                return f"Candle positivo aos 33s apos {count} candles da mesma cor"
    return None


def detect_eight_candle_reversal(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    minimum = TREND_SEQUENCE_MIN + REVERSAL_CONFIRMATION_CANDLES
    if len(closed) < minimum:
        return None, f"Aguardando {minimum} candles", None

    colors = [candle_color(candle) for candle in closed]
    if colors[-1] == "DOJI":
        return None, "Aguardando candle sem DOJI", "DOJI"

    reversal_color = colors[-1]
    reversal_count = 0
    for color in reversed(colors):
        if color != reversal_color:
            break
        reversal_count += 1

    if reversal_count != REVERSAL_CONFIRMATION_CANDLES:
        return None, "Aguardando segunda vela da reversao", reversal_color

    previous_color = "RED" if reversal_color == "GREEN" else "GREEN"
    previous_count = 0
    for color in reversed(colors[:-reversal_count]):
        if color != previous_color:
            break
        previous_count += 1

    if previous_count < TREND_SEQUENCE_MIN:
        return None, "Aguardando 8 candles antes da reversao", reversal_color

    direction = "PUT" if reversal_color == "GREEN" else "CALL"
    sequence_label = "verdes" if previous_color == "GREEN" else "vermelhos"
    reversal_label = "verdes" if reversal_color == "GREEN" else "vermelhos"
    pattern = (
        f"{previous_count} candles {sequence_label}; reversao com "
        f"{REVERSAL_CONFIRMATION_CANDLES} {reversal_label}; operar contra nas velas 3, 4 e 5"
    )
    return direction, pattern, reversal_color


def detect_eight_candle_continuation(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    target_count = TREND_SEQUENCE_MIN + CONTINUATION_CONFIRMATION_CANDLES
    if len(closed) < target_count:
        return None, f"Aguardando {target_count} candles", None

    colors = [candle_color(candle) for candle in closed]
    if colors[-1] == "DOJI":
        return None, "Aguardando candle sem DOJI", "DOJI"

    sequence_color = colors[-1]
    sequence_count = 0
    for color in reversed(colors):
        if color != sequence_color:
            break
        sequence_count += 1

    if sequence_count != target_count:
        return None, "Aguardando terceiro candle igual depois dos 8", sequence_color

    direction = "PUT" if sequence_color == "GREEN" else "CALL"
    sequence_label = "verdes" if sequence_color == "GREEN" else "vermelhos"
    pattern = (
        f"{TREND_SEQUENCE_MIN} candles {sequence_label} + "
        f"{CONTINUATION_CONFIRMATION_CANDLES} iguais; operar contra nas velas 4, 5 e 6"
    )
    return direction, pattern, sequence_color


def detect_ma21_red_wickless_green_continuation(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    pattern_size = 5
    if len(closed) < MOVING_AVERAGE_PERIOD + 4:
        return None, f"Aguardando {MOVING_AVERAGE_PERIOD + 4} candles para MA21", None

    last_five = closed[-pattern_size:]
    colors = [candle_color(candle) for candle in last_five]
    if colors != ["RED", "GREEN", "GREEN", "GREEN", "GREEN"]:
        return None, "Aguardando vermelho sem pavio + 4 verdes", colors[-1] if colors else None

    anchor_index = len(closed) - pattern_size
    anchor = closed[anchor_index]
    ma21 = moving_average_at(closed, anchor_index)
    if ma21 is None:
        return None, "Aguardando MA21 real no candle sem pavio", "RED"

    if not is_wickless(anchor):
        return None, "Aguardando vermelho sem pavio", "RED"
    if candle_close_second(anchor) > 33:
        return None, "Vermelho sem pavio fechou depois de 33s", "RED"
    if anchor.close >= ma21:
        return None, "Vermelho sem pavio nao fechou abaixo da MA21", "RED"

    pattern = (
        "Vermelho sem pavio abaixo da MA21 fechado ate 33s + "
        "4 verdes; operar contra tendencia nas velas 5, 6 e 7"
    )
    return "PUT", pattern, "GREEN"


def detect_ma21_green_buy_at_33(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < MOVING_AVERAGE_PERIOD:
        return None, f"Aguardando {MOVING_AVERAGE_PERIOD} candles para MA21", None

    anchor_index = len(closed) - 1
    anchor = closed[anchor_index]
    if candle_color(anchor) != "GREEN":
        return None, "Aguardando candle verde acima da MA21", candle_color(anchor)

    ma21 = moving_average_at(closed, anchor_index)
    if ma21 is None:
        return None, "Aguardando MA21 real no candle verde", "GREEN"
    if anchor.close <= ma21:
        return None, "Candle verde nao fechou acima da MA21", "GREEN"
    if candle_close_second(anchor) <= 33:
        return None, "Candle verde fechou antes dos 33s", "GREEN"

    previous_three = [candle_color(candle) for candle in closed[-4:-1]]
    if len(previous_three) == 3 and previous_three == ["GREEN", "GREEN", "GREEN"]:
        return None, "Antes do verde houve 3 candles verdes seguidos", "GREEN"

    pattern = (
        "Verde acima da MA21 fechado apos 33s sem 3 verdes antes; "
        "comprar no segundo 33 com 2 entradas"
    )
    return "CALL", pattern, "GREEN"


def detect_negative_33_green_close_call(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < 4:
        return None, "Aguardando 3 candles iguais e candle de virada", None

    anchor_index = len(closed) - 1
    anchor = closed[anchor_index]
    if candle_color(anchor) != "GREEN":
        return None, "Aguardando candle fechar verde positivo", candle_color(anchor)
    if not getattr(anchor, "negative_at_33", False):
        return None, "Aguardando candle que estava negativo aos 33s", "GREEN"

    previous_color, previous_count = previous_same_color_count(closed, anchor_index)
    if previous_count < 3 or previous_color == "DOJI":
        return None, "Aguardando 3 candles ou mais da mesma cor antes da virada", previous_color

    label = "verdes" if previous_color == "GREEN" else "vermelhos"
    pattern = (
        f"{previous_count} candles {label}; candle estava negativo aos 33s "
        "e fechou verde positivo; CALL com duas reentradas se necessario"
    )
    return "CALL", pattern, "GREEN"


def detect_positive_33_red_close_put(asset: Asset) -> tuple[str | None, str, str | None]:
    closed = [candle for candle in asset.candles if candle.closed]
    if len(closed) < 4:
        return None, "Aguardando 3 candles iguais e candle de virada", None

    anchor_index = len(closed) - 1
    anchor = closed[anchor_index]
    if candle_color(anchor) != "RED":
        return None, "Aguardando candle fechar vermelho negativo", candle_color(anchor)
    if not getattr(anchor, "positive_at_33", False):
        return None, "Aguardando candle que estava positivo aos 33s", "RED"

    previous_color, previous_count = previous_same_color_count(closed, anchor_index)
    if previous_count < 3 or previous_color == "DOJI":
        return None, "Aguardando 3 candles ou mais da mesma cor antes da virada", previous_color

    label = "verdes" if previous_color == "GREEN" else "vermelhos"
    pattern = (
        f"{previous_count} candles {label}; candle estava positivo aos 33s "
        "e fechou vermelho negativo; PUT com duas reentradas se necessario"
    )
    return "PUT", pattern, "RED"


def collect_strategy_signals(asset: Asset) -> list[Signal]:
    signals: list[Signal] = []

    direction, pattern, sequence_color = detect_eight_candle_reversal(asset)
    if direction:
        signals.append(make_signal(asset, direction, pattern, sequence_color, REVERSAL_WINDOW_SECONDS))

    direction, pattern, sequence_color = detect_eight_candle_continuation(asset)
    if direction:
        signals.append(make_signal(asset, direction, pattern, sequence_color, CONTINUATION_WINDOW_SECONDS))

    direction, pattern, sequence_color = detect_ma21_red_wickless_green_continuation(asset)
    if direction:
        signals.append(make_signal(asset, direction, pattern, sequence_color, MA21_WICKLESS_WINDOW_SECONDS))

    direction, pattern, sequence_color = detect_ma21_green_buy_at_33(asset)
    if direction:
        signals.append(
            make_signal(
                asset,
                direction,
                pattern,
                sequence_color,
                MA21_GREEN_BUY_WINDOW_SECONDS,
                max_entries=2,
                entry_second=33,
            )
        )

    direction, pattern, sequence_color = detect_negative_33_green_close_call(asset)
    if direction:
        signals.append(
            make_signal(
                asset,
                direction,
                pattern,
                sequence_color,
                NEGATIVE_33_GREEN_CLOSE_WINDOW_SECONDS,
                max_entries=3,
            )
        )

    direction, pattern, sequence_color = detect_positive_33_red_close_put(asset)
    if direction:
        signals.append(
            make_signal(
                asset,
                direction,
                pattern,
                sequence_color,
                NEGATIVE_33_GREEN_CLOSE_WINDOW_SECONDS,
                max_entries=3,
            )
        )

    return signals


def is_allowed_strategy_signal(signal: Signal) -> bool:
    pattern = (signal.pattern or "").lower()
    return any(marker in pattern for marker in STRATEGY_PATTERN_MARKERS)


def generate_signal(asset: Asset) -> Signal | None:
    latest_color, latest_count, latest_sequence = describe_latest_sequence(asset)
    asset.sequence = latest_sequence if latest_count else "Aguardando"
    asset.signal = describe_strategy_watch(asset)
    signals = collect_strategy_signals(asset)
    if not signals:
        return None
    asset.signal = " | ".join(f"{signal.direction}: {signal.pattern}" for signal in signals)
    return signals[0]
