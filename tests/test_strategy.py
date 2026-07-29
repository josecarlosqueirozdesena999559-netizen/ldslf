import unittest

from models.asset import Asset
from models.candle import Candle
from models.settings import BotSettings
from robot.executor import TradeExecutor
from robot.strategy import MOVING_AVERAGE_PERIOD, generate_signal, moving_average


def closed_candle(timestamp: int, close: float = 1.0) -> Candle:
    return Candle(
        open=close,
        close=close,
        high=close,
        low=close,
        timestamp=timestamp,
        update_timestamp=timestamp + 59,
        closed=True,
    )


def live_candle(timestamp: int, close: float, second: int = 33) -> Candle:
    return Candle(
        open=close,
        close=close,
        high=close,
        low=close,
        timestamp=timestamp,
        update_timestamp=timestamp + second,
        closed=False,
    )


def strategy_asset(previous_close: float, current_close: float, second: int = 33) -> Asset:
    candles = [closed_candle(index * 60) for index in range(MOVING_AVERAGE_PERIOD - 1)]
    previous_timestamp = (MOVING_AVERAGE_PERIOD - 1) * 60
    candles.append(closed_candle(previous_timestamp, previous_close))
    candles.append(live_candle(previous_timestamp + 60, current_close, second))
    return Asset(name="EURUSD", active_id=1, payout=90, candles=candles)


def strategy_01_asset(direction: str, mark_33: bool = True) -> Asset:
    base = 1.0 if direction == "CALL" else 1.1
    candles = [closed_candle(index * 60, base) for index in range(MOVING_AVERAGE_PERIOD)]
    breaker_timestamp = MOVING_AVERAGE_PERIOD * 60
    confirmation_timestamp = breaker_timestamp + 60
    current_timestamp = confirmation_timestamp + 60
    if direction == "CALL":
        candles.append(
            Candle(
                open=1.0,
                close=1.10,
                high=1.10,
                low=1.0,
                timestamp=breaker_timestamp,
                update_timestamp=breaker_timestamp + 59,
                closed=True,
            )
        )
        candles.append(
            Candle(
                open=1.10,
                close=1.20,
                high=1.20,
                low=1.08,
                timestamp=confirmation_timestamp,
                update_timestamp=confirmation_timestamp + 59,
                closed=True,
                negative_at_33=mark_33,
            )
        )
    else:
        candles.append(
            Candle(
                open=1.10,
                close=1.0,
                high=1.10,
                low=1.0,
                timestamp=breaker_timestamp,
                update_timestamp=breaker_timestamp + 59,
                closed=True,
            )
        )
        candles.append(
            Candle(
                open=1.0,
                close=0.90,
                high=1.02,
                low=0.90,
                timestamp=confirmation_timestamp,
                update_timestamp=confirmation_timestamp + 59,
                closed=True,
                positive_at_33=mark_33,
            )
        )
    candles.append(live_candle(current_timestamp, candles[-1].close, 1))
    return Asset(name="EURUSD-OTC", active_id=1, payout=90, candles=candles)


class Strategy01Ma21ReversalTests(unittest.TestCase):
    def test_real_ma21_uses_only_closed_candles(self) -> None:
        asset = strategy_asset(previous_close=1.21, current_close=5.0)

        self.assertAlmostEqual(moving_average(asset.candles), (20.0 + 1.21) / 21)

    def test_green_breaks_ma21_and_next_turns_positive_buys_next_candle(self) -> None:
        signal = generate_signal(strategy_01_asset("CALL"))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertTrue(signal.enter_on_signal)
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("CALL no inicio do proximo candle", signal.pattern)
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_red_breaks_ma21_and_next_turns_negative_sells_next_candle(self) -> None:
        signal = generate_signal(strategy_01_asset("PUT"))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertTrue(signal.enter_on_signal)
        self.assertIn("PUT no inicio do proximo candle", signal.pattern)

    def test_does_not_operate_without_second_33_mark(self) -> None:
        self.assertIsNone(generate_signal(strategy_01_asset("CALL", mark_33=False)))

    def test_does_not_operate_at_or_below_ma21(self) -> None:
        self.assertIsNone(generate_signal(strategy_asset(1.21, 1.0)))

    def test_does_not_operate_when_equal_to_previous_close(self) -> None:
        self.assertIsNone(generate_signal(strategy_asset(1.10, 1.10)))

    def test_does_not_operate_without_21_closed_candles(self) -> None:
        candles = [closed_candle(index * 60) for index in range(20)]
        candles.append(live_candle(20 * 60, 1.10))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))


if __name__ == "__main__":
    unittest.main()
