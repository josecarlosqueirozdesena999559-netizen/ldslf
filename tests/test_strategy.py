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


def strategy_01_asset(mark_33: bool = True, setup_close: float = 0.90) -> Asset:
    candles = [closed_candle(index * 60, 1.10) for index in range(MOVING_AVERAGE_PERIOD - 1)]
    setup_timestamp = (MOVING_AVERAGE_PERIOD - 1) * 60
    candles.append(
        Candle(
            open=1.05,
            close=setup_close,
            high=max(1.05, setup_close),
            low=min(1.05, setup_close),
            timestamp=setup_timestamp,
            update_timestamp=setup_timestamp + 59,
            closed=True,
            negative_at_33=mark_33,
        )
    )
    candles.append(live_candle(setup_timestamp + 60, setup_close, 1))
    return Asset(name="EURUSD-OTC", active_id=1, payout=90, candles=candles)


def strategy_02_asset(
    price_at_33: float | None = 1.10,
    setup_close: float = 1.20,
    setup_open: float = 1.00,
) -> Asset:
    candles = [closed_candle(index * 60, 1.00) for index in range(MOVING_AVERAGE_PERIOD - 1)]
    setup_timestamp = (MOVING_AVERAGE_PERIOD - 1) * 60
    candles.append(
        Candle(
            open=setup_open,
            close=setup_close,
            high=max(setup_open, setup_close),
            low=min(setup_open, setup_close),
            timestamp=setup_timestamp,
            update_timestamp=setup_timestamp + 59,
            closed=True,
            price_at_33=price_at_33,
        )
    )
    candles.append(live_candle(setup_timestamp + 60, setup_close, 1))
    return Asset(name="GBPUSD-OTC", active_id=2, payout=90, candles=candles)


class Strategy01Ma21ReversalTests(unittest.TestCase):
    def test_real_ma21_uses_only_closed_candles(self) -> None:
        asset = strategy_asset(previous_close=1.21, current_close=5.0)

        self.assertAlmostEqual(moving_average(asset.candles), (20.0 + 1.21) / 21)

    def test_negative_at_33_and_close_below_ma21_sells_next_candle(self) -> None:
        signal = generate_signal(strategy_01_asset())

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertTrue(signal.enter_on_signal)
        self.assertEqual(signal.max_entries, 2)
        self.assertIn("PUT na abertura do proximo candle", signal.pattern)
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_does_not_operate_without_second_33_mark(self) -> None:
        self.assertIsNone(generate_signal(strategy_01_asset(mark_33=False)))

    def test_does_not_operate_when_setup_closes_above_ma21(self) -> None:
        self.assertIsNone(generate_signal(strategy_01_asset(setup_close=1.20)))

    def test_does_not_operate_when_equal_to_previous_close(self) -> None:
        self.assertIsNone(generate_signal(strategy_asset(1.10, 1.10)))

    def test_does_not_operate_without_21_closed_candles(self) -> None:
        candles = [closed_candle(index * 60) for index in range(20)]
        candles.append(live_candle(20 * 60, 1.10))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        self.assertIsNone(generate_signal(asset))


class Strategy02AboveMa21Tests(unittest.TestCase):
    def test_confirms_all_conditions_and_buys_next_candle(self) -> None:
        signal = generate_signal(strategy_02_asset())

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertIn("Estrategia 02", signal.pattern)
        self.assertTrue(signal.enter_on_signal)
        self.assertEqual(signal.max_entries, 2)
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_rejects_without_exact_second_33_price(self) -> None:
        self.assertIsNone(generate_signal(strategy_02_asset(price_at_33=None)))

    def test_rejects_when_close_is_not_above_second_33_price(self) -> None:
        self.assertIsNone(generate_signal(strategy_02_asset(price_at_33=1.20)))

    def test_rejects_when_close_is_not_above_ma21(self) -> None:
        self.assertIsNone(generate_signal(strategy_02_asset(price_at_33=0.95, setup_close=1.00)))

    def test_rejects_red_candle(self) -> None:
        self.assertIsNone(generate_signal(strategy_02_asset(price_at_33=1.10, setup_close=1.20, setup_open=1.25)))


if __name__ == "__main__":
    unittest.main()
