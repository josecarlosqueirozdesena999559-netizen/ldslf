from __future__ import annotations

import time
import threading
from datetime import datetime, timedelta

from rich.live import Live

from app.terminal_ui import TerminalUI
from bullex.account import account_snapshot
from bullex.client import BullExClient
from models.candle import BULLEX_TIMEZONE
from models.settings import BotSettings
from models.trade import Signal
from robot.executor import TradeExecutor
from robot.risk import RiskManager
from robot.state import RobotState
from robot.strategy import CANDLE_LOOKBACK, candle_color, generate_signal, is_allowed_strategy_signal
from storage.history import HistoryStore


MONITOR_POLL_SECONDS = 0.5
ASSET_UPDATE_SECONDS = 1.0
CANDLE_LOG_SECONDS = 10.0


class RobotEngine:
    def __init__(
        self,
        client: BullExClient,
        settings: BotSettings,
        risk: RiskManager,
        history: HistoryStore,
        ui: TerminalUI,
        logger,
        account_mode: str,
        auto_trade: bool = True,
        display_mode: str = "dashboard",
    ) -> None:
        self.client = client
        self.settings = settings
        self.risk = risk
        self.history = history
        self.ui = ui
        self.logger = logger
        self.account_mode = account_mode
        self.auto_trade = auto_trade
        self.display_mode = display_mode
        self.state = RobotState()
        self.executor = TradeExecutor(client, risk, history, logger)
        self.operation_open = False
        self.operation_lock = threading.Lock()
        self.used_signal_keys: set[tuple] = set()
        self.negative_at_33_marks: set[tuple[str, int]] = set()
        self.positive_at_33_marks: set[tuple[str, int]] = set()
        self.trade_thread: threading.Thread | None = None
        self.scan_deadline = 0.0
        self.last_green_time = "-"
        self.last_payout_update = 0.0
        self.last_asset_update: dict[str, float] = {}
        self.last_candle_log: dict[str, float] = {}
        self.pair_watch_states: dict[str, dict] = {}

    def start(self) -> None:
        self.state.running = True
        self.state.status = "Carregando ativos"
        self.state.assets = self.client.get_assets(self.settings.payout_min, self.settings.asset_limit)
        self.logger.info("[ASSETS] %s ativos carregados", len(self.state.assets))
        if not self.state.assets:
            self.state.status = "Nenhum ativo aberto com payout mínimo"
            self.ui.console.print("[red]Nenhum ativo aberto com payout mínimo.[/red]")
            return
        self.state.status = "Iniciando streams"
        for asset in self.state.assets:
            started = self.client.start_candles_stream(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
            self.logger.info("[CANDLE] stream %s %s", asset.name, "iniciado" if started else "fallback polling")
        self.state.status = "Carregando candles atuais da BullEx"
        self.load_initial_candles()
        self.reset_scan_timer()
        self.state.status = "Monitorando e operando em DEMO" if self.auto_trade else "Monitorando candles"
        self.monitor_assets()

    def stop(self) -> None:
        self.state.running = False
        self.state.status = "Parado"
        for asset in self.state.assets:
            self.client.stop_candles_stream(asset.name, self.settings.timeframe)

    def monitor_assets(self) -> None:
        with Live(self.update_live_panels(), console=self.ui.console, refresh_per_second=2, screen=True) as live:
            while self.state.running:
                if self.display_mode == "pair_watch":
                    self.update_candles()
                    signal = self.update_pair_watch_and_find_signal()
                elif self.auto_trade and not self.operation_open:
                    signal = self.update_market_and_find_signal()
                    if not signal:
                        signal = self.update_pair_watch_and_find_signal()
                else:
                    self.update_candles()
                    self.update_focus_asset()
                    signal = self.find_best_signal(mark_used=False)
                self.state.last_signal = signal
                if signal and not self.operation_open and self.is_pair_watch_signal(signal):
                    self.start_pair_watch_trade(signal)
                elif self.auto_trade and not self.operation_open and self.display_mode != "pair_watch":
                    self.handle_auto_trade(signal)
                live.update(self.update_live_panels())
                time.sleep(MONITOR_POLL_SECONDS)

    def handle_auto_trade(self, signal: Signal | None) -> None:
        self.state.status = "Escaneando ativos em tempo real / aguardando sinal"
        if signal:
            self.start_trade(signal)

    def reset_scan_timer(self) -> None:
        self.scan_deadline = time.time() + self.settings.scan_seconds

    def update_candles(self) -> None:
        now = time.time()
        update_payout = now - self.last_payout_update >= 30
        if update_payout:
            self.last_payout_update = now
        for asset in self._ordered_assets_for_update():
            self.update_asset_candles(asset, update_payout)

    def update_market_and_find_signal(self) -> Signal | None:
        update_payout = time.time() - self.last_payout_update >= 30
        if update_payout:
            self.last_payout_update = time.time()
        for asset in self._ordered_assets_for_update():
            self.update_asset_candles(asset, update_payout)
            self.update_focus_asset()
            if not asset.open or asset.payout < self.settings.payout_min:
                asset.signal = "-"
                continue
            signal = generate_signal(asset)
            if not signal:
                continue
            key = self.signal_key(asset, signal)
            if key in self.used_signal_keys:
                continue
            self.used_signal_keys.add(key)
            self.logger.info("[SIGNAL] %s %s encontrado", signal.asset, signal.direction)
            return signal
        return None

    def update_asset_candles(self, asset, update_payout: bool) -> None:
        try:
            now = time.time()
            if now - self.last_asset_update.get(asset.name, 0.0) < ASSET_UPDATE_SECONDS:
                return
            self.last_asset_update[asset.name] = now
            if update_payout:
                asset.payout = self.client.get_payout(asset.name)
                asset.open = asset.payout >= self.settings.payout_min
            if not asset.open:
                return
            asset.candles = self.client.get_realtime_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
            self.mark_negative_at_33(asset)
            if now - self.last_candle_log.get(asset.name, 0.0) >= CANDLE_LOG_SECONDS:
                self.last_candle_log[asset.name] = now
                closed = [candle for candle in asset.candles if candle.closed]
                colors = " ".join(candle_color(candle) for candle in closed[-5:])
                self.logger.info("[CANDLE] %s ultimos 5: %s", asset.name, colors or "-")
        except Exception as exc:
            self.logger.info("[CANDLE] falha %s: %s", asset.name, exc)

    def _ordered_assets_for_update(self):
        if not self.state.focused_asset:
            return self.state.assets
        focused = []
        others = []
        for asset in self.state.assets:
            if asset.name == self.state.focused_asset:
                focused.append(asset)
            else:
                others.append(asset)
        return focused + others

    def load_initial_candles(self) -> None:
        for asset in self.state.assets:
            try:
                asset.candles = self.client.get_realtime_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                if not asset.candles:
                    asset.candles = self.client.get_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                self.mark_negative_at_33(asset)
            except Exception as exc:
                self.logger.info("[CANDLE] carga inicial falhou %s: %s", asset.name, exc)

    def mark_negative_at_33(self, asset) -> None:
        for candle in asset.candles:
            key = (asset.name, int(candle.timestamp))
            if key in self.negative_at_33_marks:
                candle.negative_at_33 = True
            if key in self.positive_at_33_marks:
                candle.positive_at_33 = True

        current = asset.current_candle
        if not current or current.closed:
            return
        elapsed = int(current.update_timestamp or time.time()) - int(current.timestamp)
        key = (asset.name, int(current.timestamp))
        if elapsed >= 33 and current.close < current.open:
            self.negative_at_33_marks.add(key)
            current.negative_at_33 = True
        if elapsed >= 33 and current.close > current.open:
            self.positive_at_33_marks.add(key)
            current.positive_at_33 = True

    def find_best_signal(self, mark_used: bool = True) -> Signal | None:
        signals: list[tuple[Signal, tuple]] = []
        for asset in self.state.assets:
            if not asset.open or asset.payout < self.settings.payout_min:
                asset.signal = "-"
                continue
            signal = generate_signal(asset)
            if signal:
                key = self.signal_key(asset, signal)
                if key in self.used_signal_keys:
                    continue
                signals.append((signal, key))
        if not signals:
            return None
        best, key = max(signals, key=lambda item: item[0].payout)
        if mark_used:
            self.used_signal_keys.add(key)
        self.logger.info("[SIGNAL] %s %s encontrado", best.asset, best.direction)
        return best

    def update_pair_watch_and_find_signal(self) -> Signal | None:
        self.state.status = (
            f"Monitorando pares de cores: limite {self.settings.pair_watch_minutes} minutos"
        )
        threshold_seconds = self.settings.pair_watch_minutes * 60
        now = time.time()
        signal_to_trade: Signal | None = None

        for asset in self.state.assets:
            if not asset.open or asset.payout < self.settings.payout_min:
                self.pair_watch_states[asset.name] = {
                    "status": "Ativo fechado ou payout baixo",
                    "alert": False,
                }
                continue

            state = self.update_pair_watch_asset(asset, now, threshold_seconds)
            if state.get("signal") and signal_to_trade is None:
                signal_to_trade = state["signal"]
                state["signal"] = None

        self.update_pair_watch_focus()
        return signal_to_trade

    def update_pair_watch_asset(self, asset, now: float, threshold_seconds: int) -> dict:
        closed = [candle for candle in asset.candles if candle.closed and candle_color(candle) != "DOJI"]
        state = self.pair_watch_states.get(asset.name, {})
        if len(closed) < 3:
            state.update(
                {
                    "status": "Aguardando tendencia",
                    "alert": False,
                    "elapsed_seconds": 0,
                    "last_colors": self._last_pair_watch_colors(closed),
                }
            )
            self.pair_watch_states[asset.name] = state
            return state

        last = closed[-1]
        last_color = candle_color(last)
        last_timestamp = int(last.timestamp)

        if (
            not state.get("watching")
            and (state.get("respected") or state.get("alert"))
            and last_timestamp == state.get("completed_timestamp")
        ):
            state["last_colors"] = self._last_pair_watch_colors(closed)
            self.pair_watch_states[asset.name] = state
            return state

        if state.get("watching"):
            state["elapsed_seconds"] = int(now - float(state.get("started_at", now)))
            state["last_colors"] = self._last_pair_watch_colors(closed)
            target_color = state.get("target_color")
            if last_color == target_color and last_timestamp != state.get("first_candle_timestamp"):
                state.update(
                    {
                        "watching": False,
                        "respected": True,
                        "alert": False,
                        "completed_timestamp": last_timestamp,
                        "status": f"Respeitou: 2 {self._pair_color_label(target_color)}",
                    }
                )
            elif state["elapsed_seconds"] >= threshold_seconds and not state.get("trade_sent"):
                direction = "CALL" if target_color == "GREEN" else "PUT"
                state.update(
                    {
                        "watching": False,
                        "respected": False,
                        "alert": True,
                        "trade_sent": True,
                        "completed_timestamp": last_timestamp,
                        "status": f"Nao respeitou: 1 {self._pair_color_label(target_color)}; entrada {direction}",
                        "signal": Signal(
                            asset=asset.name,
                            active_id=asset.active_id,
                            payout=asset.payout,
                            pattern=f"Par de cores atrasado: apenas 1 {self._pair_color_label(target_color)} em {self.settings.pair_watch_minutes} minutos",
                            direction=direction,
                            sequence_color=target_color,
                            timestamp=datetime.now(),
                            strategy_window_seconds=60,
                            max_entries=1,
                        ),
                    }
                )
            else:
                remaining = max(0, threshold_seconds - int(state.get("elapsed_seconds", 0) or 0))
                state["status"] = (
                    f"Nasceu 1 {self._pair_color_label(target_color)} as {state.get('first_candle_time', '-')}; "
                    f"aguardando 2 {self._pair_color_label(target_color)} ({remaining // 60:02d}:{remaining % 60:02d})"
                )
            self.pair_watch_states[asset.name] = state
            return state

        previous_color = candle_color(closed[-2])
        previous_count = 0
        for candle in reversed(closed[:-1]):
            if candle_color(candle) != previous_color:
                break
            previous_count += 1

        if previous_count >= 2 and last_color != previous_color and last_timestamp != state.get("completed_timestamp"):
            trend = "ALTA" if previous_color == "GREEN" else "BAIXA"
            first_time = datetime.fromtimestamp(last_timestamp, BULLEX_TIMEZONE)
            deadline_time = first_time + timedelta(seconds=threshold_seconds)
            state = {
                "watching": True,
                "respected": False,
                "alert": False,
                "trend": trend,
                "target_color": last_color,
                "first_candle_timestamp": last_timestamp,
                "first_candle_time": first_time.strftime("%H:%M:%S"),
                "deadline_time": deadline_time.strftime("%H:%M:%S"),
                "started_at": now,
                "elapsed_seconds": 0,
                "status": f"Nasceu 1 {self._pair_color_label(last_color)} as {first_time.strftime('%H:%M:%S')}; aguardando 2 ate {deadline_time.strftime('%H:%M:%S')}",
                "last_colors": self._last_pair_watch_colors(closed),
                "completed_timestamp": last_timestamp,
            }
        else:
            state.update(
                {
                    "watching": False,
                    "alert": False,
                    "trend": "ALTA" if last_color == "GREEN" else "BAIXA",
                    "target_color": "-",
                    "elapsed_seconds": 0,
                    "first_candle_time": "-",
                    "deadline_time": "-",
                    "status": "Aguardando primeira cor contraria",
                    "last_colors": self._last_pair_watch_colors(closed),
                }
            )
        self.pair_watch_states[asset.name] = state
        return state

    def update_pair_watch_focus(self) -> None:
        alert_assets = [
            asset for asset in self.state.assets if self.pair_watch_states.get(asset.name, {}).get("alert")
        ]
        if alert_assets:
            self.state.focused_asset = alert_assets[0].name
            return
        watching_assets = [
            asset for asset in self.state.assets if self.pair_watch_states.get(asset.name, {}).get("watching")
        ]
        self.state.focused_asset = watching_assets[0].name if watching_assets else None

    def update_focus_asset(self) -> None:
        if self.operation_open:
            return
        ready_assets = [asset for asset in self.state.assets if asset.candles]
        if not ready_assets:
            self.state.focused_asset = None
            return

        current = self._asset_by_name(self.state.focused_asset)
        if current and self._visual_sequence_count(current) >= 2:
            return

        best = max(ready_assets, key=lambda asset: (self._visual_sequence_count(asset), asset.payout))
        if self._visual_sequence_count(best) >= 2:
            self.state.focused_asset = best.name
        else:
            self.state.focused_asset = None

    def _asset_by_name(self, name: str | None):
        if not name:
            return None
        return next((asset for asset in self.state.assets if asset.name == name), None)

    @staticmethod
    def _visual_sequence_count(asset) -> int:
        if not asset.candles:
            return 0
        last_color = candle_color(asset.candles[-1])
        if last_color == "DOJI":
            return 0
        count = 0
        for candle in reversed(asset.candles):
            if candle_color(candle) != last_color:
                break
            count += 1
        return count

    def start_trade(self, signal: Signal) -> None:
        with self.operation_lock:
            if self.operation_open:
                return
            if not is_allowed_strategy_signal(signal):
                self.state.status = f"Bloqueado: estrategia nao permitida ({signal.pattern})"
                self.executor.current_trade = self.state.status
                return
            self.state.focused_asset = signal.asset
            self.operation_open = True
        self.state.status = f"Operando: {signal.pattern}"
        self.trade_thread = threading.Thread(target=self.execute_cycle, args=(signal,), daemon=True)
        self.trade_thread.start()

    def start_pair_watch_trade(self, signal: Signal) -> None:
        with self.operation_lock:
            if self.operation_open:
                return
            self.state.focused_asset = signal.asset
            self.operation_open = True
        self.state.status = f"Operando par atrasado: {signal.pattern}"
        self.trade_thread = threading.Thread(target=self.execute_pair_watch_trade, args=(signal,), daemon=True)
        self.trade_thread.start()

    def execute_pair_watch_trade(self, signal: Signal) -> None:
        try:
            self.executor.execute_single(signal, self.settings, self.account_mode, "PAR ATRASADO")
            self.state.status = (
                f"Monitorando pares de cores: limite {self.settings.pair_watch_minutes} minutos"
            )
        finally:
            with self.operation_lock:
                self.operation_open = False

    def execute_cycle(self, signal: Signal) -> None:
        try:
            trade = self.executor.execute_cycle(signal, self.settings, self.account_mode)
            if trade and trade.result == "WIN":
                self.last_green_time = time.strftime("%H:%M:%S")
            self.reset_scan_timer()
            self.state.status = "Escaneando ativos em tempo real / aguardando sinal"
        finally:
            with self.operation_lock:
                self.operation_open = False

    @staticmethod
    def is_reentry_signal(signal: Signal) -> bool:
        return False

    @staticmethod
    def is_pair_watch_signal(signal: Signal) -> bool:
        return (signal.pattern or "").lower().startswith("par de cores atrasado")

    @staticmethod
    def signal_key(asset, signal: Signal) -> tuple:
        closed = [candle for candle in asset.candles if candle.closed]
        last_timestamp = int(closed[-1].timestamp) if closed else 0
        return (asset.name, signal.direction, signal.pattern, last_timestamp)

    def update_live_panels(self):
        account = account_snapshot(self.client)
        if self.display_mode == "pair_watch":
            return self.ui.render_pair_watch_monitor(
                account=account,
                assets=self.state.assets,
                states=self.pair_watch_states,
                settings=self.settings,
                current_trade=self.executor.current_trade,
                status=self.state.status,
            )
        if self.display_mode == "individual":
            return self.ui.render_individual_monitor(
                account=account,
                assets=self.state.assets,
                focused_asset_name=self.state.focused_asset,
                settings=self.settings,
                signal=self.state.last_signal,
                current_trade=self.executor.current_trade,
                status=self.state.status,
                auto_trade=self.auto_trade,
            )
        return self.ui.render_dashboard(
            account=account,
            assets=self.state.assets,
            settings=self.settings,
            signal=self.state.last_signal,
            current_trade=self.executor.current_trade,
            history_summary=self.history.summary(),
            status=self.state.status,
            auto_trade=self.auto_trade,
        )

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        seconds = max(0, seconds)
        minutes, secs = divmod(seconds, 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _last_pair_watch_colors(candles) -> str:
        return " ".join(candle_color(candle) for candle in candles[-8:]) or "-"

    @staticmethod
    def _pair_color_label(color: str | None) -> str:
        if color == "GREEN":
            return "verde"
        if color == "RED":
            return "vermelho"
        return "-"
