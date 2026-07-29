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


class Strategy01Ma21ReversalTests(unittest.TestCase):
    def test_real_ma21_uses_only_closed_candles(self) -> None:
        asset = strategy_asset(previous_close=1.21, current_close=5.0)

        self.assertAlmostEqual(moving_average(asset.candles), (20.0 + 1.21) / 21)

    def test_above_previous_and_ma21_after_33_sells_immediately(self) -> None:
        signal = generate_signal(strategy_asset(previous_close=1.0, current_close=1.10))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertTrue(signal.enter_on_signal)
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("venda PUT", signal.pattern)
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_below_previous_but_above_ma21_after_33_buys_immediately(self) -> None:
        signal = generate_signal(strategy_asset(previous_close=1.21, current_close=1.10))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertTrue(signal.enter_on_signal)
        self.assertIn("compra CALL", signal.pattern)

    def test_does_not_operate_before_second_33(self) -> None:
        self.assertIsNone(generate_signal(strategy_asset(1.0, 1.10, second=32)))

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
