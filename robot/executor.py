from datetime import datetime
import threading
import time

from bullex.client import BullExClient
from models.settings import BotSettings
from models.trade import Signal, TradeResult
from robot.martingale import attempt_name, get_next_value
from robot.risk import RiskManager
from robot.strategy import is_allowed_strategy_signal
from storage.history import HistoryStore


DEFAULT_STRATEGY_WINDOW_SECONDS = 300


class TradeExecutor:
    def __init__(self, client: BullExClient, risk: RiskManager, history: HistoryStore, logger) -> None:
        self.client = client
        self.risk = risk
        self.history = history
        self.logger = logger
        self.current_trade = "Nenhuma"
        self.last_cycle_trades: list[TradeResult] = []
        self._operation_lock = threading.Lock()

    def buy_call(self, signal: Signal, value: float, duration: int):
        return self.client.buy(signal.asset, "call", value, duration)

    def buy_put(self, signal: Signal, value: float, duration: int):
        return self.client.buy(signal.asset, "put", value, duration)

    def wait_result(self, order_id):
        return self.client.get_result(order_id)

    def apply_martingale(self, settings: BotSettings, step: int) -> float:
        return get_next_value(settings.entry_value, step, settings.martingale_multiplier)

    def update_history(self, trade: TradeResult) -> None:
        self.history.add(trade)

    @staticmethod
    def direction_for_step(signal: Signal, step: int) -> str:
        return signal.direction

    @staticmethod
    def max_steps_for_signal(signal: Signal, settings: BotSettings) -> int:
        max_entries = getattr(signal, "max_entries", 0)
        if max_entries:
            return max(0, int(max_entries) - 1)
        return settings.max_martingale if settings.martingale_enabled else 0

    def execute_cycle(self, signal: Signal, settings: BotSettings, account_mode: str) -> TradeResult | None:
        if not self._operation_lock.acquire(blocking=False):
            self.logger.info("[TRADE] entrada ignorada: ja existe operacao em andamento")
            self.current_trade = "Operacao em andamento; aguardando finalizar"
            return None
        try:
            return self._execute_cycle(signal, settings, account_mode)
        finally:
            self._operation_lock.release()

    def _execute_cycle(self, signal: Signal, settings: BotSettings, account_mode: str) -> TradeResult | None:
        if not is_allowed_strategy_signal(signal):
            self.logger.info("[TRADE] sinal bloqueado fora das estrategias: %s", signal.pattern)
            self.current_trade = f"Bloqueado: estrategia nao permitida ({signal.pattern})"
            return None

        duration = {"M1": 1, "M5": 5, "M15": 15}[settings.timeframe]
        step = 0
        last_trade: TradeResult | None = None
        self.last_cycle_trades = []
        balance_before = self.client.get_balance()
        detected_mode = self.client.get_balance_mode()
        while True:
            elapsed = (datetime.now() - signal.timestamp).total_seconds()
            window_seconds = getattr(signal, "strategy_window_seconds", DEFAULT_STRATEGY_WINDOW_SECONDS)
            if elapsed >= window_seconds:
                window_minutes = max(1, int(window_seconds // 60))
                self.logger.info("[TRADE] janela da estrategia encerrada apos %s minutos", window_minutes)
                self.current_trade = f"Estrategia encerrada: {window_minutes} minutos"
                return last_trade

            value = self.apply_martingale(settings, step)
            allowed, reason = self.risk.can_trade(
                settings=settings,
                account_mode=account_mode,
                detected_mode=detected_mode,
                balance=balance_before,
                value=value,
            )
            if not allowed:
                self.logger.info("[RISK] %s, robô parado", reason)
                self.current_trade = f"Bloqueado: {reason}"
                return last_trade

            direction = self.direction_for_step(signal, step)
            platform_direction = "call" if direction == "CALL" else "put"
            self.current_trade = f"ENTRADA {direction} {signal.asset} {signal.pattern} {attempt_name(step)} R$ {value:.2f}"
            self.logger.info("[TRADE] sinal=%s plataforma=%s ativo=%s valor=%.2f", direction, platform_direction, signal.asset, value)
            self.wait_entry_second(signal)
            elapsed = (datetime.now() - signal.timestamp).total_seconds()
            if elapsed >= window_seconds:
                window_minutes = max(1, int(window_seconds // 60))
                self.logger.info("[TRADE] janela da estrategia encerrada antes da entrada apos %s minutos", window_minutes)
                self.current_trade = f"Estrategia encerrada: {window_minutes} minutos"
                return last_trade

            ok, order_id = self.client.buy(signal.asset, platform_direction, value, duration)
            if not ok:
                self.logger.info("[TRADE] falha ao abrir ordem: %s", order_id)
                self.current_trade = "Aguardando outro sinal"
                return last_trade

            result, profit = self.wait_result(order_id)
            balance_after = self.client.get_balance()
            result, profit = self.resolve_robot_order_result(
                broker_result=result,
                broker_profit=profit,
                order_id=order_id,
            )
            self.risk.add_profit(profit)
            trade = TradeResult(
                timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                asset=signal.asset,
                direction=direction,
                payout=signal.payout,
                value=value,
                attempt=attempt_name(step),
                result=result,
                profit=profit,
                balance_before=balance_before,
                balance_after=balance_after,
                account_mode=account_mode,
            )
            self.update_history(trade)
            self.last_cycle_trades.append(trade)
            last_trade = trade
            self.logger.info("[RESULT] %s lucro %.2f", result, profit)

            if result == "WIN":
                self.current_trade = f"WIN {signal.asset} {attempt_name(step)} lucro {profit:.2f}"
                return last_trade

            max_steps = self.max_steps_for_signal(signal, settings)
            if step >= max_steps:
                label = "RED" if result == "LOSS" else "DOJI"
                self.current_trade = f"{label} final {signal.asset} {attempt_name(step)} lucro {profit:.2f}"
                return last_trade

            step += 1
            next_value = self.apply_martingale(settings, step)
            balance_before = balance_after
            self.current_trade = f"Parcial {signal.asset}; fazendo G{step} R$ {next_value:.2f}"
            self.logger.info("[MARTINGALE] G%s valor %.2f", step, next_value)

    def execute_single(self, signal: Signal, settings: BotSettings, account_mode: str, note: str) -> TradeResult | None:
        if not self._operation_lock.acquire(blocking=False):
            self.logger.info("[TRADE] entrada unica ignorada: ja existe operacao em andamento")
            self.current_trade = "Operacao em andamento; aguardando finalizar"
            return None
        try:
            return self._execute_single(signal, settings, account_mode, note)
        finally:
            self._operation_lock.release()

    def _execute_single(self, signal: Signal, settings: BotSettings, account_mode: str, note: str) -> TradeResult | None:
        duration = {"M1": 1, "M5": 5, "M15": 15}[settings.timeframe]
        value = settings.entry_value
        balance_before = self.client.get_balance()
        allowed, reason = self.risk.can_trade(
            settings=settings,
            account_mode=account_mode,
            detected_mode=self.client.get_balance_mode(),
            balance=balance_before,
            value=value,
        )
        if not allowed:
            self.logger.info("[RISK] %s, entrada única bloqueada", reason)
            self.current_trade = f"Bloqueado: {reason}"
            return None

        platform_direction = "call" if signal.direction == "CALL" else "put"
        self.current_trade = f"{note}: {signal.direction} {signal.asset} R$ {value:.2f}"
        self.logger.info("[TRADE] sinal=%s plataforma=%s ativo=%s valor=%.2f", signal.direction, platform_direction, signal.asset, value)
        ok, order_id = self.client.buy(signal.asset, platform_direction, value, duration)
        if not ok:
            self.logger.info("[TRADE] falha ao abrir entrada única: %s", order_id)
            self.current_trade = "Aguardando outro sinal"
            return None

        result, profit = self.wait_result(order_id)
        balance_after = self.client.get_balance()
        result, profit = self.resolve_robot_order_result(
            broker_result=result,
            broker_profit=profit,
            order_id=order_id,
        )
        self.risk.add_profit(profit)
        trade = TradeResult(
            timestamp=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            asset=signal.asset,
            direction=signal.direction,
            payout=signal.payout,
            value=value,
            attempt=note,
            result=result,
            profit=profit,
            balance_before=balance_before,
            balance_after=balance_after,
            account_mode=account_mode,
        )
        self.update_history(trade)
        label = "GREEN" if result == "WIN" else "RED" if result == "LOSS" else "DOJI"
        self.current_trade = f"{label} {note} {signal.asset} lucro {profit:.2f}"
        return trade

    @staticmethod
    def _wait_next_candle(timeframe: str) -> None:
        seconds = {"M1": 60, "M5": 300, "M15": 900}[timeframe]
        now = int(time.time())
        wait = seconds - (now % seconds)
        if wait > 2:
            time.sleep(wait)

    def wait_entry_second(self, signal: Signal) -> None:
        entry_second = getattr(signal, "entry_second", None)
        if entry_second is None:
            return
        current_second = int(time.time()) % 60
        wait = (int(entry_second) - current_second) % 60
        if wait > 0:
            self.current_trade = f"Aguardando segundo {entry_second} para entrada"
            time.sleep(wait)

    def resolve_robot_order_result(
        self,
        broker_result: str,
        broker_profit: float,
        order_id,
    ) -> tuple[str, float]:
        broker_profit = round(float(broker_profit), 2)
        raw_result = str(broker_result or "").strip().upper()
        if raw_result in {"WIN", "WON", "PROFIT"}:
            profit = abs(broker_profit)
            self.logger.info(
                "[RESULT_ROBOT_ORDER] order=%s result=%s profit=%.2f",
                order_id,
                "WIN",
                profit,
            )
            return "WIN", profit
        if raw_result in {"LOSS", "LOOSE", "LOSE", "LOST"}:
            profit = -abs(broker_profit)
            self.logger.info(
                "[RESULT_ROBOT_ORDER] order=%s result=%s profit=%.2f",
                order_id,
                "LOSS",
                profit,
            )
            return "LOSS", profit
        if raw_result in {"DOJI", "EQUAL", "DRAW"}:
            self.logger.info("[RESULT_ROBOT_ORDER] order=%s result=DOJI profit=0.00", order_id)
            return "DOJI", 0.0
        if abs(broker_profit) >= 0.01:
            result = "WIN" if broker_profit > 0 else "LOSS"
            profit = abs(broker_profit) if result == "WIN" else -abs(broker_profit)
            self.logger.info(
                "[RESULT_ROBOT_ORDER] order=%s result=%s profit=%.2f",
                order_id,
                result,
                profit,
            )
            return result, profit
        self.logger.info("[RESULT_ROBOT_ORDER] order=%s result=DOJI profit=0.00", order_id)
        return "DOJI", 0.0
