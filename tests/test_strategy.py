import unittest

from models.asset import Asset
from models.candle import Candle
from models.settings import BotSettings
from robot.executor import TradeExecutor
from robot.strategy import generate_signal


def flat_candle(timestamp: int) -> Candle:
    return Candle(open=1.0, close=1.0, high=1.01, low=0.99, timestamp=timestamp, update_timestamp=timestamp)


def low_flat_candle(timestamp: int) -> Candle:
    return Candle(open=0.6, close=0.6, high=0.61, low=0.59, timestamp=timestamp, update_timestamp=timestamp)


def red_candle(timestamp: int, close: float, update_second: int) -> Candle:
    return Candle(
        open=1.0,
        close=close,
        high=1.01,
        low=min(0.99, close - 0.01),
        timestamp=timestamp,
        update_timestamp=timestamp + update_second,
    )


def green_candle(timestamp: int, close: float, update_second: int) -> Candle:
    return Candle(
        open=1.0,
        close=close,
        high=max(1.01, close + 0.01),
        low=0.99,
        timestamp=timestamp,
        update_timestamp=timestamp + update_second,
    )


def live_green_candle(timestamp: int, open_price: float, close: float, update_second: int) -> Candle:
    return Candle(
        open=open_price,
        close=close,
        high=max(open_price, close),
        low=min(open_price, close),
        timestamp=timestamp,
        update_timestamp=timestamp + update_second,
        closed=False,
    )


class Strategy01RedBelowMa21Tests(unittest.TestCase):
    def make_asset(self, last: Candle, current: Candle | None = None) -> Asset:
        candles = [flat_candle(index * 60) for index in range(21)]
        candles.append(last)
        if current:
            candles.append(current)
        return Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

    def test_red_below_real_ma21_before_33_seconds_sells_after_pullback_with_one_reentry(self) -> None:
        signal = generate_signal(
            self.make_asset(
                red_candle(21 * 60, close=0.70, update_second=33),
                live_green_candle(22 * 60, open_price=0.70, close=0.76, update_second=8),
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")
        self.assertEqual(signal.sequence_color, "RED")
        self.assertEqual(signal.max_entries, 2)
        self.assertIsNone(signal.entry_second)
        self.assertTrue(signal.enter_on_signal)
        self.assertIn("Estrategia 01", signal.pattern)
        self.assertIn("media movel real de 21", signal.pattern)
        self.assertIn("aguardou repique", signal.pattern)
        self.assertEqual(TradeExecutor.direction_for_step(signal, 0), "PUT")
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_no_signal_until_next_candle_pulls_back(self) -> None:
        signal = generate_signal(
            self.make_asset(
                red_candle(21 * 60, close=0.70, update_second=33),
                live_green_candle(22 * 60, open_price=0.70, close=0.72, update_second=8),
            )
        )

        self.assertIsNone(signal)

    def test_operation_candle_uses_pullback_without_ma21_filter(self) -> None:
        signal = generate_signal(
            self.make_asset(
                red_candle(21 * 60, close=0.70, update_second=33),
                live_green_candle(22 * 60, open_price=0.70, close=1.02, update_second=8),
            )
        )

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "PUT")

    def test_no_signal_when_red_closes_above_ma21(self) -> None:
        candles = [low_flat_candle(index * 60) for index in range(21)]
        candles.append(red_candle(21 * 60, close=0.90, update_second=33))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNone(signal)

    def test_no_signal_when_ma21_points_up(self) -> None:
        candles = [low_flat_candle(index * 60) for index in range(20)]
        candles.append(flat_candle(20 * 60))
        candles.append(red_candle(21 * 60, close=0.61, update_second=33))
        candles.append(live_green_candle(22 * 60, open_price=0.61, close=0.70, update_second=8))
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

        signal = generate_signal(asset)

        self.assertIsNone(signal)

    def test_no_signal_when_red_below_ma21_closes_after_33_seconds(self) -> None:
        signal = generate_signal(self.make_asset(red_candle(21 * 60, close=0.70, update_second=34)))

        self.assertIsNone(signal)

    def test_no_signal_when_last_candle_is_green(self) -> None:
        signal = generate_signal(self.make_asset(green_candle(21 * 60, close=1.20, update_second=20)))

        self.assertIsNone(signal)

    def test_no_signal_without_real_ma21(self) -> None:
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=[flat_candle(index * 60) for index in range(20)])

        self.assertIsNone(generate_signal(asset))


class Strategy05GreenAbovePreviousMa21Tests(unittest.TestCase):
    def make_asset(self, previous: Candle, current: Candle) -> Asset:
        candles = [flat_candle(index * 60) for index in range(20)]
        candles.extend([previous, current])
        return Asset(name="EURUSD", active_id=1, payout=90, candles=candles)

    def test_green_above_previous_and_ma21_after_33_calls_with_one_reentry(self) -> None:
        previous = Candle(open=1.0, close=1.01, high=1.02, low=0.99, timestamp=20 * 60)
        current = live_green_candle(21 * 60, open_price=1.01, close=1.20, update_second=34)

        signal = generate_signal(self.make_asset(previous, current))

        self.assertIsNotNone(signal)
        self.assertEqual(signal.direction, "CALL")
        self.assertEqual(signal.max_entries, 2)
        self.assertTrue(signal.enter_on_signal)
        self.assertIn("Estrategia 05", signal.pattern)
        self.assertIn("acima do fechamento anterior", signal.pattern)
        self.assertIn("media movel real de 21", signal.pattern)
        self.assertEqual(TradeExecutor.max_steps_for_signal(signal, BotSettings()), 1)

    def test_no_signal_when_green_does_not_close_above_previous(self) -> None:
        previous = Candle(open=1.0, close=1.25, high=1.26, low=0.99, timestamp=20 * 60)
        current = live_green_candle(21 * 60, open_price=1.01, close=1.20, update_second=34)

        self.assertIsNone(generate_signal(self.make_asset(previous, current)))

    def test_no_signal_when_green_is_not_above_ma21(self) -> None:
        candles = [
            Candle(open=2.0, close=2.0, high=2.01, low=1.99, timestamp=index * 60)
            for index in range(20)
        ]
        previous = Candle(open=0.8, close=0.9, high=0.91, low=0.79, timestamp=20 * 60)
        current = Candle(
            open=0.9,
            close=1.0,
            high=1.01,
            low=0.89,
            timestamp=21 * 60,
            update_timestamp=21 * 60 + 34,
            closed=False,
        )
        asset = Asset(name="EURUSD", active_id=1, payout=90, candles=candles + [previous, current])

        self.assertIsNone(generate_signal(asset))

    def test_no_signal_when_green_closes_at_33_seconds(self) -> None:
        previous = Candle(open=1.0, close=1.01, high=1.02, low=0.99, timestamp=20 * 60)
        current = live_green_candle(21 * 60, open_price=1.01, close=1.20, update_second=33)

        self.assertIsNone(generate_signal(self.make_asset(previous, current)))


if __name__ == "__main__":
    unittest.main()
