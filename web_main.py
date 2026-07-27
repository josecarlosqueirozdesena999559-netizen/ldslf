from __future__ import annotations

import threading
import time
import json
import queue
import uuid
from dataclasses import replace
from datetime import datetime, timedelta
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from bullex.account import account_snapshot
from bullex.client import BullExClient
from config import ASSET_PRIORITY
from models.asset import Asset
from models.candle import BULLEX_TIMEZONE, Candle
from models.settings import BotSettings
from models.trade import Signal, TradeResult
from robot.executor import TradeExecutor
from robot.risk import RiskManager
from robot.strategy import (
    CANDLE_LOOKBACK,
    MOVING_AVERAGE_PERIOD,
    candle_color,
    generate_signal,
    is_allowed_strategy_signal,
    moving_average_snapshot,
)
from storage.history import HistoryStore
from storage.supabase_store import SupabaseStore


app = FastAPI(title="AndersonAnalisesTrader")
SETTINGS_FILE = Path("data/web_settings.json")
MANUAL_ENTRIES_FILE = Path("data/manual_entries.json")
SESSION_SCORE_FILE = Path("data/session_score.json")
LOGIN_TIMEOUT_SECONDS = 35
BOT_LOOP_IDLE_SECONDS = 0.20


def bullex_now() -> datetime:
    return datetime.now(BULLEX_TIMEZONE)


def today_key() -> str:
    return bullex_now().strftime("%Y-%m-%d")


STRATEGY_OPTIONS = (
    ("estrategia 02", "Estrategia 02 - 13min sem pares"),
)
STRATEGY_KEYS = {key for key, _label in STRATEGY_OPTIONS}


class LoginPayload(BaseModel):
    email: str
    password: str
    account_mode: str = "DEMO"
    real_confirmation: str | None = None


class SettingsPayload(BaseModel):
    entry_value: float | None = None
    stop_win: float | None = None
    stop_loss: float | None = None
    payout_min: int | None = None
    martingale_multiplier: float | None = None
    schedule_enabled: bool | None = None
    schedule_start: str | None = None
    schedule_stop: str | None = None
    enabled_strategies: list[str] | None = None
    real_confirmation: str | None = None


class ManualEntryPayload(BaseModel):
    asset: str
    time: str
    direction: str
    value: float | None = None
    market: str = "BINARIOS"


def analyze_hourly_sequences(candles: list[Candle]) -> list[dict]:
    hours: dict[str, list[Candle]] = {}
    for candle in sorted((item for item in candles if item.closed), key=lambda item: item.timestamp):
        hour_key = candle.time.strftime("%Y-%m-%d %H:00")
        hours.setdefault(hour_key, []).append(candle)

    rows: list[dict] = []
    for hour_key, hour_candles in hours.items():
        sequence_lengths: list[int] = []
        best_color = "DOJI"
        best_count = 0
        best_start = None
        best_end = None
        current_color = None
        current_count = 0
        current_start = None

        for candle in hour_candles:
            color = candle_color(candle)
            if color == "DOJI":
                if current_count:
                    sequence_lengths.append(current_count)
                current_color = None
                current_count = 0
                current_start = None
                continue
            if color == current_color:
                current_count += 1
            else:
                if current_count:
                    sequence_lengths.append(current_count)
                current_color = color
                current_count = 1
                current_start = candle.time
            if current_count > best_count:
                best_color = color
                best_count = current_count
                best_start = current_start
                best_end = candle.time

        if current_count:
            sequence_lengths.append(current_count)
        hour_time = hour_candles[0].time
        rows.append(
            {
                "key": hour_key,
                "date": hour_time.strftime("%d/%m"),
                "hour": hour_time.strftime("%H:00"),
                "sequence": best_count,
                "color": best_color,
                "start": best_start.strftime("%H:%M") if best_start else "-",
                "end": best_end.strftime("%H:%M") if best_end else "-",
                "candles": len(hour_candles),
                "average": round(sum(sequence_lengths) / len(sequence_lengths), 2) if sequence_lengths else 0,
                "sequence_count": len(sequence_lengths),
            }
        )
    return rows


LONG_SEQUENCE_LEVELS = (11, 12, 13)


def count_long_sequence_milestones(candles: list[Candle]) -> dict:
    counts = {str(level): 0 for level in LONG_SEQUENCE_LEVELS}
    longest = 0
    long_runs = 0
    current_color = None
    current_count = 0

    def finish_run(length: int) -> None:
        nonlocal longest, long_runs
        if not length:
            return
        longest = max(longest, length)
        if length >= LONG_SEQUENCE_LEVELS[0]:
            long_runs += 1
        for level in LONG_SEQUENCE_LEVELS:
            if length >= level:
                counts[str(level)] += 1

    for candle in sorted((item for item in candles if item.closed), key=lambda item: item.timestamp):
        color = candle_color(candle)
        if color == "DOJI":
            finish_run(current_count)
            current_color = None
            current_count = 0
            continue
        if color == current_color:
            current_count += 1
        else:
            finish_run(current_count)
            current_color = color
            current_count = 1

    finish_run(current_count)
    return {
        "counts": counts,
        "longest": longest,
        "runs": long_runs,
    }


class WebBot:
    def __init__(self) -> None:
        self.client: BullExClient | None = None
        self.settings = BotSettings()
        self.risk = RiskManager()
        self.history = HistoryStore()
        self.supabase = SupabaseStore()
        self.executor: TradeExecutor | None = None
        self.assets: list[Asset] = []
        self.focused_asset: str | None = None
        self.last_signal: Signal | None = None
        self.last_green_time = "-"
        self.status = "Aguardando login"
        self.stop_reason = ""
        self.connected = False
        self.running = False
        self.starting = False
        self.auto_trade = True
        self.manual_paused = False
        self.active_strategy = "Estrategia 02"
        self.next_strategy = "13 minutos sem 2 velas iguais; entrar somente em verde"
        self.schedule_enabled = False
        self.schedule_start = ""
        self.schedule_stop = ""
        self.settings_saved = False
        self.scheduler_thread: threading.Thread | None = None
        self.operation_open = False
        self.used_signal_keys: set[tuple] = set()
        self.asset_signal_cooldowns: dict[str, int] = {}
        self.asset_strategy_cooldowns: dict[str, set[str]] = {}
        self.negative_at_33_marks: set[tuple[str, int]] = set()
        self.positive_at_33_marks: set[tuple[str, int]] = set()
        self.last_payout_update = 0.0
        self.last_account_update = 0.0
        self.last_account = {"connected": False, "mode": "DEMO", "currency": "", "balance": 0.0}
        self.session_wins = 0
        self.session_losses = 0
        self.session_profit = 0.0
        self.session_results: list[dict] = []
        self.manual_entries: list[dict] = []
        self.lock = threading.Lock()
        self.thread: threading.Thread | None = None
        self.trade_thread: threading.Thread | None = None
        self.session_token = ""
        self.analysis_lock = threading.Lock()
        self.sequence_cache: dict[str, tuple[float, dict]] = {}
        self.monitored_sequence_cache: tuple[str, tuple[str, ...], dict] | None = None
        self.pair_watch_states: dict[str, dict] = {}
        self.pair_watch_respected = 0
        self.pair_watch_entries = 0
        self.strategy_02_next_trade_at = 0.0
        self.load_saved_settings()
        self.load_manual_entries()
        self.load_session_score()

    def login(self, email: str, password: str, account_mode: str, real_confirmation: str | None = None) -> tuple[bool, str | None]:
        account_mode = "REAL" if account_mode.upper() == "REAL" else "DEMO"
        with self.lock:
            self.status = f"Conectando em {account_mode}"
        client = BullExClient()
        ok, error = self.connect_with_timeout(client, email, password, account_mode)
        if not ok:
            with self.lock:
                self.connected = False
                self.status = f"Falha no login: {error}"
            return False, error
        with self.lock:
            self.session_token = uuid.uuid4().hex
            self.client = client
            self.connected = True
            if account_mode == "REAL":
                self.risk.confirm_real("CONFIRMO REAL")
            else:
                self.risk.real_confirmed = False
            self.executor = TradeExecutor(client, self.risk, self.history, NoneLogger())
            self.last_account = account_snapshot(client)
            self.status = "Login realizado"
            self.start_scheduler()
        return True, None

    def connect_with_timeout(self, client: BullExClient, email: str, password: str, account_mode: str) -> tuple[bool, str | None]:
        result_queue: queue.Queue[tuple[bool, str | None]] = queue.Queue(maxsize=1)

        def connect_worker() -> None:
            try:
                result_queue.put(client.connect(email, password, account_mode))
            except Exception as exc:
                result_queue.put((False, str(exc)))

        worker = threading.Thread(target=connect_worker, daemon=True)
        worker.start()
        try:
            return result_queue.get(timeout=LOGIN_TIMEOUT_SECONDS)
        except queue.Empty:
            return False, "Tempo limite ao conectar na BullEx. Verifique senha, bloqueio por IP/VPS ou sessÃƒÂ£o aberta em outro lugar."

    def start(self, auto_trade: bool = True, reset_stats: bool = True) -> tuple[bool, str | None]:
        with self.lock:
            if not self.client or not self.connected:
                return False, "FaÃƒÂ§a login primeiro."
            if auto_trade and not self.settings_saved:
                self.status = "Salve as configuracoes antes de iniciar"
                return False, "Salve as configuracoes antes de iniciar."
            if self.running or self.starting:
                if auto_trade and not self.auto_trade:
                    self.auto_trade = True
                    self.status = "Operando automaticamente / aguardando sinal"
                elif not auto_trade and self.auto_trade:
                    self.status = "Robo ja esta operando automaticamente"
                return True, None
            self.starting = True
            self.manual_paused = False
            self.auto_trade = auto_trade
            self.active_strategy = "Estrategia 02"
            self.next_strategy = "13 minutos sem 2 velas iguais; entrar somente em verde"
            self.status = "Carregando ativos"

        try:
            assets = self.client.get_priority_assets_fast(self.settings.payout_min, self.settings.asset_limit)
            if not assets:
                with self.lock:
                    self.starting = False
                    self.status = "Nenhum ativo aberto com payout mÃƒÂ­nimo"
                return False, "Nenhum ativo aberto com payout mÃƒÂ­nimo."
            for asset in assets:
                self.client.start_candles_stream(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
            with self.lock:
                self.assets = assets
                self.status = "Carregando candles para estrategias"
            self.load_initial_candles()
        except Exception as exc:
            with self.lock:
                self.starting = False
                self.running = False
                self.status = f"Erro ao iniciar: {exc}"
            return False, str(exc)

        with self.lock:
            self.running = True
            self.starting = False
            self.status = "Escaneando ativos em tempo real / aguardando sinal"
        self.thread = threading.Thread(target=self.loop, daemon=True)
        self.thread.start()
        return True, None

    def stop(self) -> None:
        with self.lock:
            self.running = False
            self.starting = False
            self.manual_paused = False
            self.status = "Parado"
        if self.client:
            for asset in self.assets:
                self.client.stop_candles_stream(asset.name, self.settings.timeframe)

    def logout(self) -> None:
        with self.lock:
            client = self.client
            assets = list(self.assets)
            timeframe = self.settings.timeframe
            self.running = False
            self.starting = False
            self.session_token = uuid.uuid4().hex
            self.client = None
            self.executor = None
            self.assets = []
            self.focused_asset = None
            self.last_signal = None
            self.connected = False
            self.manual_paused = False
            self.operation_open = False
            self.used_signal_keys = set()
            self.negative_at_33_marks = set()
            self.positive_at_33_marks = set()
            self.pair_watch_states = {}
            self.status = "Aguardando login"
            self.last_account = {"connected": False, "mode": "DEMO", "currency": "", "balance": 0.0}
            self.settings_saved = False
        if client:
            self.disconnect_in_background(client, assets, timeframe)

    @staticmethod
    def disconnect_in_background(client: BullExClient, assets: list[Asset], timeframe: str) -> None:
        def worker() -> None:
            for asset in assets:
                try:
                    client.stop_candles_stream(asset.name, timeframe)
                except Exception:
                    pass
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning("Falha ao deslogar da BullEx: %s", exc)

        threading.Thread(target=worker, daemon=True).start()

    def pause(self) -> None:
        with self.lock:
            self.running = False
            self.starting = False
            self.manual_paused = True
            self.status = "Pausado"

    def resume(self) -> tuple[bool, str | None]:
        with self.lock:
            auto_trade = self.auto_trade
        return self.start(auto_trade=auto_trade, reset_stats=False)

    def loop(self) -> None:
        with self.lock:
            session_token = self.session_token
        while True:
            with self.lock:
                if not self.running or session_token != self.session_token:
                    return

            if self.auto_trade and not self.operation_open:
                signal = self.update_pair_watch_and_find_signal()
                if not signal and any(key != "estrategia 02" for key in self.settings.enabled_strategies):
                    signal = self.update_market_and_find_signal()
            elif self.operation_open:
                self.update_candles()
                self.update_focus_asset()
                signal = self.last_signal
            else:
                self.update_candles()
                self.update_focus_asset()
                signal = self.find_best_signal()
            with self.lock:
                if not self.operation_open:
                    self.last_signal = signal
                    self.status = "Escaneando ativos em tempo real / aguardando sinal"
            if signal and self.auto_trade and not self.operation_open:
                if self.is_pair_watch_signal(signal):
                    self.start_pair_watch_trade(signal)
                else:
                    self.start_trade(signal)
            self.refresh_account_if_due()
            time.sleep(BOT_LOOP_IDLE_SECONDS)

    def load_initial_candles(self) -> None:
        for asset in self.assets:
            try:
                candles = self.client.get_realtime_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                if not candles:
                    candles = self.client.get_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                asset.candles = candles
            except Exception:
                asset.candles = []

    def update_candles(self) -> None:
        update_payout = time.time() - self.last_payout_update >= 30
        if update_payout:
            self.last_payout_update = time.time()
        for asset in self.ordered_assets():
            self.update_asset_candles(asset, update_payout)

    def update_market_and_find_signal(self) -> Signal | None:
        if not self.settings.enabled_strategies:
            self.status = "Nenhuma estrategia configurada"
            self.update_focus_asset()
            return None
        update_payout = time.time() - self.last_payout_update >= 30
        if update_payout:
            self.last_payout_update = time.time()
        signals: list[tuple[Signal, tuple]] = []
        for asset in self.ordered_assets():
            self.update_asset_candles(asset, update_payout)
            if not asset.open or asset.payout < self.settings.payout_min:
                asset.signal = "-"
                continue
            signal = generate_signal(asset)
            if not signal:
                self.clear_strategy_cooldown(asset)
                continue
            if not self.is_signal_strategy_enabled(signal):
                continue
            self.release_inactive_strategy_cooldowns(asset, signal)
            if self.is_strategy_in_cooldown(asset, signal):
                continue
            if self.is_asset_in_signal_cooldown(asset):
                continue
            key = self.signal_key(asset, signal)
            if key in self.used_signal_keys:
                continue
            signals.append((signal, key))
        if not signals:
            self.update_focus_asset()
            return None
        signal, _key = max(signals, key=lambda item: (self.strategy_priority(item[0]), item[0].payout))
        self.focused_asset = signal.asset
        return signal

    def update_asset_candles(self, asset: Asset, update_payout: bool) -> None:
        try:
            if update_payout:
                asset.payout = self.client.get_payout(asset.name)
                asset.open = asset.payout >= self.settings.payout_min
            if asset.open:
                candles = self.client.get_realtime_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                if not candles:
                    candles = self.client.get_candles(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
                if candles:
                    asset.candles = candles
                    self.mark_negative_at_33(asset)
        except Exception:
            pass

    def mark_negative_at_33(self, asset: Asset) -> None:
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

    def ordered_assets(self) -> list[Asset]:
        if not self.focused_asset:
            return self.assets
        return sorted(self.assets, key=lambda asset: 0 if asset.name == self.focused_asset else 1)

    def update_focus_asset(self) -> None:
        if self.operation_open:
            return
        ready = [asset for asset in self.assets if asset.candles]
        if not ready:
            self.focused_asset = None
            return
        best = max(ready, key=self.asset_radar_score)
        self.focused_asset = best.name

    @staticmethod
    def asset_recency_score(asset: Asset) -> int:
        current = asset.current_candle
        if not current:
            return 0
        updated_at = int(current.update_timestamp or current.timestamp)
        return max(0, 120 - (int(time.time()) - updated_at))

    def asset_radar_score(self, asset: Asset) -> tuple[int, int, int, str]:
        if not asset.open or asset.payout < self.settings.payout_min:
            return (0, 0, 0, asset.name)

        state = self.pair_watch_states.get(asset.name, {})
        if state.get("alert"):
            return (
                150 + int(state.get("elapsed_seconds", 0) or 0),
                asset.payout,
                self.asset_recency_score(asset),
                asset.name,
            )
        if state.get("watching"):
            remaining = max(0, self.settings.pair_watch_minutes * 60 - int(state.get("elapsed_seconds", 0) or 0))
            return (120 + max(0, 30 - remaining // 60), asset.payout, self.asset_recency_score(asset), asset.name)

        signal = generate_signal(asset)
        if signal and self.is_signal_strategy_enabled(signal):
            return (100 + self.strategy_priority(signal), asset.payout, self.asset_recency_score(asset), asset.name)
        if signal:
            asset.signal = "-"

        text = f"{asset.sequence} {asset.signal}".lower()
        sequence_count = self.visual_sequence_count(asset)
        score = sequence_count
        if "reversao 1/2" in text:
            score = max(score, 85)
        elif "ma21" in text:
            score = max(score, 70)
        elif "perto dos 8" in text:
            score = max(score, 60 + sequence_count)
        elif sequence_count >= 2:
            score = max(score, sequence_count * 6)
        return (score, asset.payout, self.asset_recency_score(asset), asset.name)

    @staticmethod
    def strategy_priority(signal: Signal) -> int:
        pattern = (signal.pattern or "").lower()
        if "par de cores atrasado" in pattern:
            return 95
        if "rompeu a ma21" in pattern:
            return 90
        if "comprar no segundo 33" in pattern:
            return 85
        if "velas 5, 6 e 7" in pattern:
            return 80
        if "estrategia 03" in pattern:
            return 75
        if "estrategia 04" in pattern or "estrategia 05" in pattern:
            return 82
        if "estrategia 01" in pattern:
            return 88
        if "velas 3, 4 e 5" in pattern:
            return 70
        return 50

    def find_best_signal(self) -> Signal | None:
        return self.find_signal_for_sequences(mark_used=False)

    def find_signal_for_sequences(self, mark_used: bool = True) -> Signal | None:
        if not self.settings.enabled_strategies:
            return None
        signals = []
        for asset in self.assets:
            if asset.open and asset.payout >= self.settings.payout_min:
                signal = generate_signal(asset)
                if signal:
                    if not self.is_signal_strategy_enabled(signal):
                        asset.signal = "-"
                        continue
                    self.release_inactive_strategy_cooldowns(asset, signal)
                    if self.is_strategy_in_cooldown(asset, signal):
                        continue
                    if self.is_asset_in_signal_cooldown(asset):
                        continue
                else:
                    self.clear_strategy_cooldown(asset)
                if signal:
                    key = self.signal_key(asset, signal)
                    if key in self.used_signal_keys:
                        continue
                    signals.append((signal, key))
        if not signals:
            return None
        signal, key = max(signals, key=lambda item: (self.strategy_priority(item[0]), item[0].payout))
        if mark_used:
            self.used_signal_keys.add(key)
        return signal

    def update_pair_watch_and_find_signal(self) -> Signal | None:
        if "estrategia 02" not in self.settings.enabled_strategies:
            self.pair_watch_states = {}
            return None
        threshold_seconds = self.settings.pair_watch_minutes * 60
        now = time.time()
        signals: list[tuple[Signal, int]] = []
        for asset in self.ordered_assets():
            if not asset.open or asset.payout < self.settings.payout_min:
                self.pair_watch_states[asset.name] = {
                    "status": "Ativo fechado ou payout baixo",
                    "alert": False,
                    "last_colors": "-",
                }
                continue
            state = self.update_pair_watch_asset(asset, now, threshold_seconds)
            if state.get("signal"):
                signals.append((state["signal"], int(state.get("elapsed_seconds", 0) or 0)))
                state["signal"] = None
        if not signals:
            return None
        signal_to_trade, _elapsed = max(signals, key=lambda item: (item[1], item[0].payout))
        self.focused_asset = signal_to_trade.asset
        return signal_to_trade

    def update_pair_watch_asset(self, asset: Asset, now: float, threshold_seconds: int) -> dict:
        closed = [candle for candle in asset.candles if candle.closed and candle_color(candle) != "DOJI"]
        state = self.pair_watch_states.get(asset.name, {})
        if len(closed) < 2:
            state.update(
                {
                    "status": "Aguardando candles para medir pares",
                    "alert": False,
                    "elapsed_seconds": 0,
                    "last_colors": self.last_pair_watch_colors(closed),
                    "equal_pairs_count": 0,
                    "last_equal_pair_time": "-",
                }
            )
            self.pair_watch_states[asset.name] = state
            return state

        last = closed[-1]
        last_color = candle_color(last)
        last_timestamp = int(last.timestamp)
        current = getattr(asset, "current_candle", asset.candles[-1] if asset.candles else None)

        latest_pair_timestamp = None
        latest_pair_color = None
        for index in range(len(closed) - 1, 0, -1):
            color = candle_color(closed[index])
            if color == candle_color(closed[index - 1]):
                latest_pair_timestamp = int(closed[index].timestamp)
                latest_pair_color = color
                break
        equal_pairs_count, last_equal_pair_time, window_colors = self.pair_watch_window_stats(
            closed,
            self.settings.pair_watch_minutes,
        )

        previous_pair_timestamp = state.get("last_pair_timestamp")
        if latest_pair_timestamp and latest_pair_timestamp != previous_pair_timestamp:
            pair_time = datetime.fromtimestamp(latest_pair_timestamp, BULLEX_TIMEZONE)
            state = {
                "watching": False,
                "respected": True,
                "alert": False,
                "trade_sent": False,
                "trend": "ALTA" if latest_pair_color == "GREEN" else "BAIXA",
                "target_color": latest_pair_color,
                "signal_color": "-",
                "signal_direction": "-",
                "first_candle_time": pair_time.strftime("%H:%M:%S"),
                "deadline_time": (pair_time + timedelta(seconds=threshold_seconds)).strftime("%H:%M:%S"),
                "last_pair_timestamp": latest_pair_timestamp,
                "elapsed_seconds": 0,
                "status": (
                    f"Estrategia 02: 2 {self.pair_color_label(latest_pair_color)} as "
                    f"{pair_time.strftime('%H:%M:%S')}; contador reiniciado"
                ),
                "last_colors": window_colors,
                "equal_pairs_count": equal_pairs_count,
                "last_equal_pair_time": last_equal_pair_time,
                "completed_timestamp": latest_pair_timestamp,
            }
            if previous_pair_timestamp:
                self.pair_watch_respected += 1
            self.pair_watch_states[asset.name] = state
            return state

        baseline_timestamp = int(previous_pair_timestamp or latest_pair_timestamp or closed[0].timestamp)
        baseline_time = datetime.fromtimestamp(baseline_timestamp, BULLEX_TIMEZONE)
        elapsed_seconds = max(0, last_timestamp - baseline_timestamp)
        deadline_time = baseline_time + timedelta(seconds=threshold_seconds)
        current_color = candle_color(current) if current and not current.closed else None
        entry_candle = current if current_color == "GREEN" else last if last_color == "GREEN" else None
        entry_timestamp = int(entry_candle.timestamp) if entry_candle else None
        is_candidate = elapsed_seconds >= threshold_seconds
        can_signal = (
            is_candidate
            and entry_candle is not None
            and state.get("last_signal_timestamp") != entry_timestamp
        )

        state.update(
            {
                "watching": True,
                "alert": is_candidate,
                "trend": "ALTA" if last_color == "GREEN" else "BAIXA",
                "target_color": last_color,
                "signal_color": "GREEN" if is_candidate else "-",
                "signal_direction": "CALL" if is_candidate else "-",
                "elapsed_seconds": elapsed_seconds,
                "first_candle_time": baseline_time.strftime("%H:%M:%S"),
                "deadline_time": deadline_time.strftime("%H:%M:%S"),
                "last_pair_timestamp": baseline_timestamp,
                "status": (
                    f"Estrategia 02: candidato ha {self.format_seconds(elapsed_seconds)} sem 2 iguais; aguardando verde"
                    if is_candidate and not can_signal
                    else "Estrategia 02: candidato atrasado; entrada verde"
                    if can_signal
                    else "Estrategia 02: aguardando completar 13 minutos sem 2 candles iguais"
                ),
                "last_colors": window_colors,
                "equal_pairs_count": equal_pairs_count,
                "last_equal_pair_time": last_equal_pair_time,
            }
        )
        asset.signal = state["status"]
        if can_signal:
            state["last_signal_timestamp"] = entry_timestamp
            state["signal"] = Signal(
                asset=asset.name,
                active_id=asset.active_id,
                payout=asset.payout,
                pattern=(
                    f"Estrategia 02: {self.settings.pair_watch_minutes} minutos sem 2 candles iguais; "
                    f"ativo mais atrasado; entrar verde"
                ),
                direction="CALL",
                sequence_color="GREEN",
                timestamp=datetime.now(),
                strategy_window_seconds=60,
                max_entries=1,
                enter_on_signal=True,
            )
            self.pair_watch_entries += 1
        self.pair_watch_states[asset.name] = state
        return state

    def start_trade(self, signal: Signal) -> None:
        with self.lock:
            if self.operation_open:
                return
            if not is_allowed_strategy_signal(signal):
                self.status = f"Bloqueado: estrategia nao permitida ({signal.pattern})"
                if self.executor:
                    self.executor.current_trade = self.status
                return
            self.used_signal_keys.add(self.signal_key_for_signal(signal))
            self.operation_open = True
            self.focused_asset = signal.asset
            self.status = f"Operando: {signal.pattern}"
            self.last_signal = signal
            if self.executor:
                self.executor.current_trade = f"SINAL ENCONTRADO {signal.direction} {signal.asset} - preparando entrada"
            session_token = self.session_token
        self.trade_thread = threading.Thread(target=self.execute_trade, args=(signal, session_token), daemon=True)
        self.trade_thread.start()

    def start_pair_watch_trade(self, signal: Signal) -> None:
        with self.lock:
            if self.operation_open:
                return
            self.used_signal_keys.add(self.signal_key_for_signal(signal))
            self.operation_open = True
            self.focused_asset = signal.asset
            self.status = f"Operando Estrategia 02: {signal.pattern}"
            self.last_signal = signal
            if self.executor:
                self.executor.current_trade = f"ESTRATEGIA 02 {signal.direction} {signal.asset} - enviando entrada"
            session_token = self.session_token
        self.trade_thread = threading.Thread(target=self.execute_pair_watch_trade, args=(signal, session_token), daemon=True)
        self.trade_thread.start()

    def execute_pair_watch_trade(self, signal: Signal, session_token: str) -> None:
        try:
            if session_token != self.session_token:
                return
            account_mode = str(self.last_account.get("mode") or "DEMO")
            trade = self.executor.execute_single(signal, self.settings, account_mode, "ESTRATEGIA 02") if self.executor else None
            if session_token != self.session_token:
                return
            if trade:
                self.strategy_02_next_trade_at = time.time() + self.settings.pair_watch_minutes * 60
                self.mark_asset_signal_cooldown(signal)
                self.mark_strategy_cooldown(signal)
                self.add_session_cycle([trade], pattern=signal.pattern)
                if trade.result == "WIN":
                    self.last_green_time = bullex_now().strftime("%H:%M:%S")
                    self.save_session_score()
                self.finish_cycle_after_trade()
            else:
                self.status = self.executor.current_trade if self.executor else "Falha ao executar par atrasado"
        finally:
            with self.lock:
                if session_token == self.session_token:
                    self.operation_open = False
            if session_token == self.session_token:
                self.refresh_account()

    def execute_trade(self, signal: Signal, session_token: str) -> None:
        try:
            if session_token != self.session_token:
                return
            account_mode = str(self.last_account.get("mode") or "DEMO")
            is_reentry = False
            trade = self.executor.execute_cycle(signal, self.settings, account_mode) if self.executor else None
            if session_token != self.session_token:
                return
            cycle_trades = self.executor.last_cycle_trades if self.executor else []
            if trade and trade.result == "WIN":
                self.mark_asset_signal_cooldown(signal)
                self.mark_strategy_cooldown(signal)
                self.add_session_cycle(cycle_trades or [trade], pattern=signal.pattern)
                self.last_green_time = bullex_now().strftime("%H:%M:%S")
                self.save_session_score()
                self.finish_cycle_after_trade()
            elif trade:
                self.mark_asset_signal_cooldown(signal)
                self.mark_strategy_cooldown(signal)
                self.add_session_cycle(cycle_trades or [trade], pattern=signal.pattern)
                self.finish_cycle_after_trade()
            else:
                if self.executor and self.executor.current_trade.startswith("Falha:"):
                    self.executor.current_trade = "Aguardando outro sinal"
                    self.update_focus_asset()
                    self.last_signal = None
                    self.used_signal_keys.discard(self.signal_key_for_signal(signal))
                elif self.executor and "stop win" in self.executor.current_trade.lower():
                    self.stop_reason = "STOP WIN atingido. RobÃƒÂ´ parado."
                    self.status = self.stop_reason
                    self.running = False
                elif self.executor and "stop loss" in self.executor.current_trade.lower():
                    self.stop_reason = "STOP LOSS atingido. RobÃƒÂ´ parado."
                    self.status = self.stop_reason
                    self.running = False
                else:
                    if self.executor and self.executor.current_trade == "Aguardando outro sinal":
                        self.used_signal_keys.discard(self.signal_key_for_signal(signal))
                    self.status = "Escaneando ativos em tempo real / aguardando sinal"
        finally:
            with self.lock:
                if session_token == self.session_token:
                    self.operation_open = False
            if session_token == self.session_token:
                self.refresh_account()

    def reset_session_stats(self) -> None:
        self.session_wins = 0
        self.session_losses = 0
        self.session_profit = 0.0
        self.session_results = []
        self.used_signal_keys = set()
        self.asset_signal_cooldowns = {}
        self.asset_strategy_cooldowns = {}
        self.pair_watch_states = {}
        self.pair_watch_respected = 0
        self.pair_watch_entries = 0
        self.strategy_02_next_trade_at = 0.0
        self.risk.daily_profit = 0.0
        self.last_green_time = "-"
        self.stop_reason = ""
        if self.executor:
            self.executor.current_trade = "Nenhuma"
        if not self.running:
            self.status = "Login realizado" if self.connected else "Aguardando login"
        self.save_session_score()

    def add_session_cycle(self, trades: list[TradeResult], pattern: str = "") -> None:
        if not trades:
            return
        win_trade = next((trade for trade in trades if trade.result == "WIN"), None)
        final_trade = win_trade or trades[-1]
        profit = round(sum(float(trade.profit or 0) for trade in trades), 2)
        cycle_result = "WIN" if win_trade else "LOSS"
        if win_trade:
            self.session_wins += 1
        else:
            self.session_losses += 1
        self.session_profit = round(self.session_profit + profit, 2)
        
        motivo = pattern or "EstratÃƒÂ©gia do RobÃƒÂ´"
            
        self.session_results.insert(
            0,
            {
                "time": final_trade.timestamp.split(" ", 1)[-1],
                "asset": final_trade.asset,
                "position": final_trade.direction,
                "gale": final_trade.attempt.upper() if final_trade.attempt != "normal" else "ENTRADA",
                "attempts": len(trades),
                "result": cycle_result,
                "profit": profit,
                "motivo": motivo,
            },
        )
        self.session_results = self.session_results[:50]
        self.save_session_score()

    def finish_cycle_after_trade(self) -> None:
        if self.risk.check_stop_win(self.settings):
            self.stop_reason = "STOP WIN atingido. RobÃƒÂ´ parado."
            self.status = self.stop_reason
            self.running = False
            return
        if self.risk.check_stop_loss(self.settings):
            self.stop_reason = "STOP LOSS atingido. RobÃƒÂ´ parado."
            self.status = self.stop_reason
            self.running = False
            return
        self.next_strategy = "13 minutos sem 2 velas iguais; entrar somente em verde"
        self.active_strategy = "Estrategia 02"
        self.status = "Escaneando ativos em tempo real / aguardando sinal"

    def start_scheduler(self) -> None:
        if self.scheduler_thread and self.scheduler_thread.is_alive():
            return
        self.scheduler_thread = threading.Thread(target=self.scheduler_loop, daemon=True)
        self.scheduler_thread.start()

    def scheduler_loop(self) -> None:
        while True:
            time.sleep(0.2)
            if not self.connected:
                return
            self.process_manual_entries()

    def can_start_now_by_schedule(self) -> bool:
        return True

    def schedule_wait_message(self) -> str:
        return "Agendamento desativado; o robo inicia imediatamente"

    @staticmethod
    def is_time_inside_window(now: str, start: str, stop: str) -> bool:
        if start <= stop:
            return start <= now < stop
        return now >= start or now < stop

    def update_settings(self, payload: SettingsPayload) -> None:
        with self.lock:
            self.settings.max_martingale = 1
            self.settings.martingale_enabled = True
            if payload.entry_value is not None:
                self.settings.entry_value = max(0.01, float(payload.entry_value))
            if payload.stop_win is not None:
                self.settings.stop_win = max(0.0, float(payload.stop_win))
            if payload.stop_loss is not None:
                self.settings.stop_loss = max(0.0, float(payload.stop_loss))
            if payload.payout_min is not None:
                self.settings.payout_min = max(1, min(100, int(payload.payout_min)))
            enabled = payload.enabled_strategies if payload.enabled_strategies is not None else ["estrategia 02"]
            self.settings.enabled_strategies = [key for key in enabled if key in STRATEGY_KEYS]
            if not self.settings.enabled_strategies:
                self.settings.enabled_strategies = ["estrategia 02"]
            self.settings.martingale_multiplier = 2.0
            if payload.schedule_enabled is not None:
                self.schedule_enabled = False
            if payload.schedule_start is not None:
                self.schedule_start = payload.schedule_start[:5]
            if payload.schedule_stop is not None:
                self.schedule_stop = payload.schedule_stop[:5]
            if self.last_account.get("mode") == "REAL":
                self.risk.confirm_real("CONFIRMO REAL")
            self.settings_saved = True
            self.save_settings()

    def load_saved_settings(self) -> None:
        if self.supabase.enabled:
            try:
                data = self.supabase.load_settings()
                if isinstance(data, dict):
                    self.apply_settings_data(data)
                    return
            except Exception:
                pass
        if not SETTINGS_FILE.exists():
            return
        try:
            data = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        self.apply_settings_data(data)

    def apply_settings_data(self, data: dict) -> None:
        self.settings.entry_value = float(data.get("entry_value", self.settings.entry_value))
        self.settings.stop_win = float(data.get("stop_win", self.settings.stop_win))
        self.settings.stop_loss = float(data.get("stop_loss", self.settings.stop_loss))
        self.settings.payout_min = int(data.get("payout_min", self.settings.payout_min))
        self.settings.martingale_multiplier = 2.0
        self.settings.max_martingale = 1
        self.settings.martingale_enabled = True
        self.settings.pair_watch_minutes = 13
        enabled = data.get("enabled_strategies", ["estrategia 02"])
        self.settings.enabled_strategies = [
            key for key in enabled if key in STRATEGY_KEYS
        ] if isinstance(enabled, list) else ["estrategia 02"]
        if not self.settings.enabled_strategies:
            self.settings.enabled_strategies = ["estrategia 02"]
        self.schedule_enabled = False
        self.schedule_start = str(data.get("schedule_start", self.schedule_start))
        self.schedule_stop = str(data.get("schedule_stop", self.schedule_stop))
        if data.get("real_confirmed"):
            self.risk.confirm_real("CONFIRMO REAL")
        self.settings_saved = True

    def save_settings(self) -> None:
        data = {
            "entry_value": self.settings.entry_value,
            "stop_win": self.settings.stop_win,
            "stop_loss": self.settings.stop_loss,
            "payout_min": self.settings.payout_min,
            "martingale_multiplier": self.settings.martingale_multiplier,
            "enabled_strategies": self.settings.enabled_strategies,
            "schedule_enabled": self.schedule_enabled,
            "schedule_start": self.schedule_start,
            "schedule_stop": self.schedule_stop,
            "real_confirmed": self.risk.real_confirmed,
        }
        if self.supabase.enabled:
            try:
                self.supabase.save_settings(data)
            except Exception:
                pass
        SETTINGS_FILE.parent.mkdir(parents=True, exist_ok=True)
        SETTINGS_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def load_manual_entries(self) -> None:
        if self.supabase.enabled:
            try:
                data = self.supabase.load_manual_entries()
                if isinstance(data, list):
                    self.manual_entries = [entry for entry in data if isinstance(entry, dict)]
                    return
            except Exception:
                pass
        if not MANUAL_ENTRIES_FILE.exists():
            return
        try:
            data = json.loads(MANUAL_ENTRIES_FILE.read_text(encoding="utf-8"))
        except Exception:
            return
        if isinstance(data, list):
            self.manual_entries = [entry for entry in data if isinstance(entry, dict)]

    def load_session_score(self) -> None:
        if self.supabase.enabled:
            try:
                data = self.supabase.load_session_score()
                if isinstance(data, dict):
                    self.apply_session_score_data(data)
                    return
            except Exception:
                pass
        if not SESSION_SCORE_FILE.exists():
            return
        try:
            data = json.loads(SESSION_SCORE_FILE.read_text(encoding="utf-8-sig"))
        except Exception:
            return
        self.apply_session_score_data(data)

    def apply_session_score_data(self, data: dict) -> None:
        if str(data.get("date") or "") != today_key():
            self.reset_session_stats()
            return
        self.session_wins = int(data.get("wins", self.session_wins))
        self.session_losses = int(data.get("losses", self.session_losses))
        self.session_profit = float(data.get("profit", self.session_profit))
        results = data.get("results", [])
        self.session_results = results if isinstance(results, list) else []
        self.last_green_time = str(data.get("last_green_time", self.last_green_time))
        self.risk.daily_profit = self.session_profit

    def save_session_score(self) -> None:
        data = {
            "date": today_key(),
            "wins": self.session_wins,
            "losses": self.session_losses,
            "profit": self.session_profit,
            "results": self.session_results,
            "last_green_time": self.last_green_time,
        }
        if self.supabase.enabled:
            try:
                self.supabase.save_session_score(data)
            except Exception:
                pass
        SESSION_SCORE_FILE.parent.mkdir(parents=True, exist_ok=True)
        SESSION_SCORE_FILE.write_text(
            json.dumps(data, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def save_manual_entries(self) -> None:
        if self.supabase.enabled:
            try:
                self.supabase.save_manual_entries(self.manual_entries)
            except Exception:
                pass
        MANUAL_ENTRIES_FILE.parent.mkdir(parents=True, exist_ok=True)
        MANUAL_ENTRIES_FILE.write_text(
            json.dumps(self.manual_entries, ensure_ascii=False, indent=2),
            encoding="utf-8",
        )

    def add_manual_entry(self, payload: ManualEntryPayload) -> tuple[bool, str | None]:
        asset = payload.asset.strip().upper()
        direction = self.normalize_manual_direction(payload.direction)
        entry_time = self.normalize_manual_time(payload.time)
        value = max(0.01, float(payload.value or self.settings.entry_value))
        market = "BINARIOS"
        if not asset:
            return False, "Informe o nome do ativo."
        if not direction:
            return False, "DireÃƒÂ§ÃƒÂ£o invÃƒÂ¡lida. Use COMPRA/CALL ou VENDA/PUT."
        if not entry_time:
            return False, "HorÃƒÂ¡rio invÃƒÂ¡lido. Use HH:MM ou HH:MM:SS."

        entry = {
            "id": uuid.uuid4().hex,
            "asset": asset,
            "time": entry_time,
            "direction": direction,
            "direction_label": "COMPRA" if direction == "CALL" else "VENDA",
            "value": value,
            "market": market,
            "status": "AGUARDANDO",
            "created_at": bullex_now().strftime("%Y-%m-%d %H:%M:%S"),
            "last_executed_date": "",
            "message": "",
        }
        with self.lock:
            self.manual_entries.append(entry)
            self.save_manual_entries()
        self.wake_manual_entries()
        return True, None

    def delete_manual_entry(self, entry_id: str) -> bool:
        with self.lock:
            before = len(self.manual_entries)
            self.manual_entries = [entry for entry in self.manual_entries if entry.get("id") != entry_id]
            changed = len(self.manual_entries) != before
            if changed:
                self.save_manual_entries()
            return changed

    def process_manual_entries(self) -> None:
        if not self.client or not self.executor or self.operation_open:
            return
        now = bullex_now()
        now_time = now.strftime("%H:%M:%S")
        today = now.strftime("%Y-%m-%d")
        due_entries = [
            entry
            for entry in self.manual_entries
            if entry.get("status") == "AGUARDANDO"
            and entry.get("last_executed_date") != today
            and str(entry.get("time", "")) <= now_time
        ]
        if not due_entries:
            return
        entry = sorted(due_entries, key=lambda item: str(item.get("time", "")))[0]
        self.start_manual_entry(entry)

    def wake_manual_entries(self) -> None:
        if self.connected:
            threading.Thread(target=self.process_manual_entries, daemon=True).start()

    def start_manual_entry(self, entry: dict) -> None:
        with self.lock:
            if self.operation_open:
                return
            self.operation_open = True
            entry["status"] = "EXECUTANDO"
            entry["message"] = "Enviando ordem"
            self.save_manual_entries()
            self.status = f"Entrada manual {entry['direction_label']} {entry['asset']}"
        self.trade_thread = threading.Thread(target=self.execute_manual_entry, args=(entry.get("id"),), daemon=True)
        self.trade_thread.start()

    def execute_manual_entry(self, entry_id: str | None) -> None:
        try:
            entry = self.manual_entry_by_id(entry_id)
            if not entry or not self.executor:
                return
            signal = self.manual_signal(entry)
            account_mode = str(self.last_account.get("mode") or "DEMO")
            trade = self.executor.execute_cycle(signal, self.manual_entry_settings(entry), account_mode)
            cycle_trades = self.executor.last_cycle_trades if self.executor else []
            if trade:
                self.add_session_cycle(cycle_trades or [trade], pattern="Entrada Manual")
                win_trade = next((item for item in cycle_trades if item.result == "WIN"), None)
                if win_trade or trade.result == "WIN":
                    self.last_green_time = bullex_now().strftime("%H:%M:%S")
                    self.save_session_score()
                entry["status"] = "WIN" if win_trade else trade.result
                entry["last_executed_date"] = bullex_now().strftime("%Y-%m-%d")
                entry["message"] = f"Resultado automÃƒÂ¡tico: {'WIN' if win_trade else trade.result} lucro {sum(float(item.profit or 0) for item in cycle_trades or [trade]):.2f}"
            else:
                entry["status"] = "FALHOU"
                entry["message"] = self.executor.current_trade if self.executor else "Falha ao executar"
            self.finish_cycle_after_trade()
        except Exception as exc:
            entry = self.manual_entry_by_id(entry_id)
            if entry:
                entry["status"] = "FALHOU"
                entry["message"] = str(exc)
            self.status = f"Falha entrada manual: {exc}"
        finally:
            with self.lock:
                self.operation_open = False
            self.save_manual_entries()
            self.refresh_account()

    def manual_entry_by_id(self, entry_id: str | None) -> dict | None:
        if not entry_id:
            return None
        return next((entry for entry in self.manual_entries if entry.get("id") == entry_id), None)

    def mark_manual_entry_win(self, entry_id: str) -> bool:
        with self.lock:
            entry = self.manual_entry_by_id(entry_id)
            if not entry:
                return False
            entry["status"] = "WIN"
            entry["last_executed_date"] = bullex_now().strftime("%Y-%m-%d")
            entry["message"] = "WIN marcado manualmente"
            self.save_manual_entries()
            return True

    def retry_manual_entry(self, entry_id: str) -> bool:
        with self.lock:
            entry = self.manual_entry_by_id(entry_id)
            if not entry:
                return False
            entry["status"] = "AGUARDANDO"
            entry["last_executed_date"] = ""
            entry["message"] = "Aguardando reenvio"
            self.save_manual_entries()
        self.wake_manual_entries()
        return True

    def manual_entry_settings(self, entry: dict) -> BotSettings:
        value = max(0.01, float(entry.get("value") or self.settings.entry_value))
        return replace(self.settings, entry_value=value, max_martingale=1, martingale_enabled=True)

    def manual_signal(self, entry: dict) -> Signal:
        asset_name = self.resolve_manual_asset_name(str(entry["asset"]).strip().upper())
        payout = 0
        active_id = 0
        known_asset = self.asset_by_name(asset_name)
        if known_asset:
            payout = known_asset.payout
            active_id = known_asset.active_id
        if self.client:
            try:
                payout = self.client.get_payout(asset_name)
            except Exception:
                pass
            if not active_id:
                try:
                    assets = self.client.get_assets(1, 100)
                    found = next((asset for asset in assets if asset.name.upper() == asset_name), None)
                    if found:
                        active_id = found.active_id
                        payout = found.payout or payout
                except Exception:
                    pass
        return Signal(
            asset=asset_name,
            active_id=active_id,
            payout=payout,
            pattern=f"Entrada manual {entry['time']} G2",
            direction=str(entry["direction"]),
            sequence_color="MANUAL",
            timestamp=datetime.now(),
        )

    def resolve_manual_asset_name(self, requested_name: str) -> str:
        if self.client:
            try:
                return self.client.resolve_active_name(requested_name)
            except Exception:
                pass
        candidates = [requested_name]
        if "-OTC" not in requested_name:
            candidates.append(f"{requested_name}-OTC")
        candidates.append(requested_name.replace("-OTC", ""))

        known_assets = self.assets
        if self.client:
            try:
                known_assets = self.client.get_assets(1, 100)
            except Exception:
                pass
        for candidate in candidates:
            found = next((asset for asset in known_assets if asset.name.upper() == candidate), None)
            if found:
                return found.name
        return requested_name

    @staticmethod
    def normalize_manual_direction(direction: str) -> str | None:
        value = direction.strip().upper()
        if value in {"CALL", "COMPRA", "COMPRAR"}:
            return "CALL"
        if value in {"PUT", "VENDA", "VENDER"}:
            return "PUT"
        return None

    @staticmethod
    def normalize_manual_time(value: str) -> str | None:
        raw = value.strip()
        for fmt in ("%H:%M:%S", "%H:%M"):
            try:
                parsed = datetime.strptime(raw, fmt)
                return parsed.strftime("%H:%M:%S")
            except ValueError:
                continue
        return None

    def pair_watch_payload(self) -> dict:
        rows = []
        limit_seconds = self.settings.pair_watch_minutes * 60
        for asset in self.assets:
            state = self.pair_watch_states.get(asset.name, {})
            elapsed = int(state.get("elapsed_seconds", 0) or 0)
            remaining = max(0, limit_seconds - elapsed) if state.get("watching") else 0
            rows.append(
                {
                    "asset": asset.name,
                    "payout": asset.payout,
                    "trend": state.get("trend", "-"),
                    "target_color": state.get("target_color", "-"),
                    "signal_color": state.get("signal_color", "-"),
                    "signal_direction": state.get("signal_direction", "-"),
                    "elapsed_seconds": elapsed,
                    "remaining_seconds": remaining,
                    "first_candle_time": state.get("first_candle_time", "-"),
                    "deadline_time": state.get("deadline_time", "-"),
                    "status": state.get("status", "Aguardando"),
                    "last_colors": state.get("last_colors", "-"),
                    "equal_pairs_count": int(state.get("equal_pairs_count", 0) or 0),
                    "last_equal_pair_time": state.get("last_equal_pair_time", "-"),
                    "watching": bool(state.get("watching")),
                    "respected": bool(state.get("respected")),
                    "alert": bool(state.get("alert")),
                }
            )
        rows.sort(
            key=lambda row: (
                0 if row["alert"] else 1 if row["watching"] else 2 if row["respected"] else 3,
                -row["elapsed_seconds"] if row["alert"] else row["remaining_seconds"] if row["watching"] else 999999,
                row["asset"],
            )
        )
        active = next((row for row in rows if row["alert"]), None) or next((row for row in rows if row["watching"]), None)
        return {
            "limit_minutes": self.settings.pair_watch_minutes,
            "respected": self.pair_watch_respected,
            "entries": self.pair_watch_entries,
            "active": active,
            "assets": rows[:20],
        }

    @staticmethod
    def last_pair_watch_colors(candles: list[Candle]) -> str:
        return " ".join(candle_color(candle) for candle in candles[-8:]) or "-"

    @staticmethod
    def pair_watch_window_stats(candles: list[Candle], limit_minutes: int) -> tuple[int, str, str]:
        window = candles[-max(1, int(limit_minutes)) :]
        equal_pairs_count = 0
        last_equal_pair_time = "-"
        for index in range(1, len(window)):
            color = candle_color(window[index])
            if color == candle_color(window[index - 1]):
                equal_pairs_count += 1
                last_equal_pair_time = datetime.fromtimestamp(
                    int(window[index].timestamp),
                    BULLEX_TIMEZONE,
                ).strftime("%H:%M:%S")
        colors = " ".join(candle_color(candle) for candle in window) or "-"
        return equal_pairs_count, last_equal_pair_time, colors

    @staticmethod
    def pair_color_label(color: str | None) -> str:
        if color == "GREEN":
            return "verde"
        if color == "RED":
            return "vermelho"
        return "-"

    @staticmethod
    def is_pair_watch_signal(signal: Signal) -> bool:
        pattern = (signal.pattern or "").lower()
        return "estrategia 02" in pattern or pattern.startswith("par de cores atrasado") or "minutos sem 2 candles iguais" in pattern

    def refresh_account(self) -> None:
        try:
            if self.client:
                self.last_account = account_snapshot(self.client)
        except Exception:
            pass

    def refresh_account_if_due(self) -> None:
        now = time.time()
        if now - self.last_account_update < 2:
            return
        self.last_account_update = now
        self.refresh_account()

    def asset_by_name(self, name: str | None) -> Asset | None:
        return next((asset for asset in self.assets if asset.name == name), None) if name else None

    @staticmethod
    def visual_sequence_count(asset: Asset) -> int:
        if not asset.candles:
            return 0
        last = candle_color(asset.candles[-1])
        if last == "DOJI":
            return 0
        count = 0
        for candle in reversed(asset.candles):
            if candle_color(candle) != last:
                break
            count += 1
        return count

    @staticmethod
    def visual_sequence(asset: Asset) -> str:
        count = WebBot.visual_sequence_count(asset)
        if not count:
            return "Aguardando"
        color = candle_color(asset.candles[-1])
        return f"{count} {'verdes' if color == 'GREEN' else 'vermelhos'}"

    def is_reentry_signal(signal: Signal) -> bool:
        return False

    @staticmethod
    def signal_key(asset: Asset, signal: Signal) -> tuple:
        closed = [candle for candle in asset.candles if candle.closed]
        last_timestamp = int(closed[-1].timestamp) if closed else 0
        return (asset.name, signal.direction, signal.pattern, last_timestamp)

    def signal_key_for_signal(self, signal: Signal) -> tuple:
        asset = self.asset_by_name(signal.asset)
        if asset:
            return self.signal_key(asset, signal)
        return (signal.asset, signal.direction, signal.pattern, int(signal.timestamp.timestamp()))

    def is_asset_in_signal_cooldown(self, asset: Asset) -> bool:
        cooldown_timestamp = self.asset_signal_cooldowns.get(asset.name)
        if not cooldown_timestamp:
            return False
        closed = [candle for candle in asset.candles if candle.closed]
        if not closed:
            return False
        return int(closed[-1].timestamp) <= cooldown_timestamp

    def mark_asset_signal_cooldown(self, signal: Signal) -> None:
        asset = self.asset_by_name(signal.asset)
        if not asset:
            return
        closed = [candle for candle in asset.candles if candle.closed]
        if closed:
            self.asset_signal_cooldowns[asset.name] = int(closed[-1].timestamp)

    def is_strategy_in_cooldown(self, asset: Asset, signal: Signal) -> bool:
        return self.signal_family(signal) in self.asset_strategy_cooldowns.get(asset.name, set())

    def mark_strategy_cooldown(self, signal: Signal) -> None:
        family = self.signal_family(signal)
        if family:
            self.asset_strategy_cooldowns.setdefault(signal.asset, set()).add(family)

    def clear_strategy_cooldown(self, asset: Asset) -> None:
        self.asset_strategy_cooldowns.pop(asset.name, None)

    def release_inactive_strategy_cooldowns(self, asset: Asset, signal: Signal) -> None:
        family = self.signal_family(signal)
        cooldowns = self.asset_strategy_cooldowns.get(asset.name)
        if cooldowns and family not in cooldowns:
            self.asset_strategy_cooldowns.pop(asset.name, None)

    @staticmethod
    def signal_family(signal: Signal) -> str:
        pattern = (signal.pattern or "").lower()
        if "estrategia 01" in pattern:
            return "estrategia 01"
        if "estrategia 02" in pattern:
            return "estrategia 02"
        if "estrategia 03" in pattern:
            return "estrategia 03"
        if "estrategia 04" in pattern:
            return "estrategia 04"
        if "estrategia 05" in pattern:
            return "estrategia 05"
        if "velas 5, 6 e 7" in pattern:
            return "ma21 wickless"
        if "comprar no segundo 33" in pattern:
            return "ma21 call 33"
        if "operar vendido no segundo 33" in pattern:
            return "ma21 put 33"
        if "negativo aos 33s" in pattern:
            return "ma21 negative 33"
        if "verde aos 33s" in pattern:
            return "ma21 positive 33"
        if "minutos sem 2 candles iguais" in pattern:
            return "estrategia 02"
        return pattern

    def is_signal_strategy_enabled(self, signal: Signal) -> bool:
        return self.signal_family(signal) in self.settings.enabled_strategies

    @staticmethod
    def format_seconds(seconds: int) -> str:
        minutes, secs = divmod(max(0, seconds), 60)
        hours, minutes = divmod(minutes, 60)
        return f"{hours:02d}:{minutes:02d}:{secs:02d}" if hours else f"{minutes:02d}:{secs:02d}"

    def reentry_status(self) -> str:
        return ""

    def monitored_assets_state(self) -> list[dict]:
        rows = []
        for asset in self.assets:
            closed = [candle for candle in asset.candles if candle.closed]
            last = closed[-1] if closed else asset.current_candle
            signal = asset.signal or "-"
            hot = signal != "-" and signal != "Analisando"
            proximity = self.asset_radar_score(asset)[0]
            rows.append(
                {
                    "asset": asset.name,
                    "payout": asset.payout,
                    "open": asset.open,
                    "sequence": asset.sequence,
                    "signal": signal,
                    "color": candle_color(last) if last else "DOJI",
                    "hot": hot,
                    "proximity": proximity,
                }
            )
        return sorted(rows, key=lambda row: (-int(row.get("proximity", 0)), not row["hot"], row["asset"]))[:20]

    def strategy_moment_state(self, monitored_assets: list[dict]) -> dict:
        if self.operation_open and self.last_signal:
            return {
                "asset": self.last_signal.asset,
                "title": f"Operando {self.last_signal.asset}: {self.last_signal.pattern}",
                "detail": f"{self.last_signal.direction} em andamento com martingale dobrando se precisar",
            }

        hot_assets = [row for row in monitored_assets if row.get("hot")]
        if hot_assets:
            row = hot_assets[0]
            return {
                "asset": row["asset"],
                "title": f"{row['asset']}: {row['signal']}",
                "detail": f"Sequencia atual {row.get('sequence') or '-'} | payout {row.get('payout', 0)}%",
            }

        focus = self.asset_by_name(self.focused_asset)
        if focus:
            signal = focus.signal or "Analisando"
            return {
                "asset": focus.name,
                "title": f"{focus.name}: {signal}",
                "detail": f"Sequencia atual {self.visual_sequence(focus)} | estrategias analisadas sem ordem fixa",
            }

        return {
            "asset": None,
            "title": "Escaneando Estrategia 02",
            "detail": "13 minutos sem verde+verde ou vermelho+vermelho; candidato entra somente em verde.",
        }

    def state(self) -> dict:
        paused = False
        candles = []
        monitored_assets = self.monitored_assets_state()
        strategy_moment = self.strategy_moment_state(monitored_assets)
        focus = self.asset_by_name(strategy_moment.get("asset")) or self.asset_by_name(self.focused_asset)
        if focus and self.focused_asset != focus.name and not self.operation_open:
            self.focused_asset = focus.name
        moving_average = moving_average_snapshot(focus) if focus else moving_average_snapshot(Asset(name="", active_id=0, payout=0))
        if focus:
            closed_for_ma: list[Candle] = []
            ma_by_timestamp: dict[int, float] = {}
            for item in focus.candles:
                if not item.closed:
                    continue
                closed_for_ma.append(item)
                if len(closed_for_ma) >= MOVING_AVERAGE_PERIOD:
                    ma_by_timestamp[item.timestamp] = sum(c.close for c in closed_for_ma[-MOVING_AVERAGE_PERIOD:]) / MOVING_AVERAGE_PERIOD
            ma_value = moving_average["value"]
            for candle in focus.candles[-CANDLE_LOOKBACK:]:
                candle_ma = ma_by_timestamp.get(candle.timestamp, ma_value if not candle.closed else None)
                candles.append(
                    {
                        "time": (candle.update_time if not candle.closed else candle.time).strftime("%H:%M:%S"),
                        "color": candle_color(candle),
                        "status": "EM ANDAMENTO" if not candle.closed else "FECHADA",
                        "price": round(candle.close, 6),
                        "move": round(candle.close - candle.open, 6),
                        "open": round(candle.open, 6),
                        "high": round(candle.high, 6),
                        "low": round(candle.low, 6),
                        "ma21": round(candle_ma, 6) if candle_ma is not None else None,
                        "tick": max(0, int(time.time()) - int(candle.update_timestamp or time.time())),
                    }
                )
        wins = self.session_wins
        losses = self.session_losses
        total = wins + losses
        return {
            "connected": self.connected,
            "running": self.running,
            "auto_trade": self.auto_trade,
            "operation_open": self.operation_open,
            "starting": self.starting,
            "paused": paused,
            "status": self.status,
            "reentry_status": self.reentry_status(),
            "stop_reason": self.stop_reason,
            "manual_paused": self.manual_paused,
            "settings_saved": self.settings_saved,
            "account": self.last_account,
            "strategy": "Estrategia 02",
            "strategy_detail": "13 minutos sem 2 velas iguais; quando fechar ou nascer verde, entrar em verde.",
            "strategy_moment": strategy_moment["title"],
            "strategy_moment_detail": strategy_moment["detail"],
            "target_sequence": self.active_strategy,
            "next_sequence": self.next_strategy,
            "asset": focus.name if focus else None,
            "sequence": self.visual_sequence(focus) if focus else "-",
            "signal": signal_payload(self.last_signal) if self.last_signal else None,
            "trade": self.executor.current_trade if self.executor else "Nenhuma operaÃƒÂ§ÃƒÂ£o",
            "last_green_time": self.last_green_time,
            "moving_average": {
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in moving_average.items()
            },
            "candles": candles,
            "monitored_assets": monitored_assets,
            "pair_watch": self.pair_watch_payload(),
            "wins": wins,
            "losses": losses,
            "greens": wins,
            "win_rate": round((wins / total) * 100, 2) if total else 0,
            "profit": self.session_profit,
            "results": self.session_results,
            "manual_entries": self.manual_entries,
            "settings": {
                "entry_value": self.settings.entry_value,
                "stop_win": self.settings.stop_win,
                "stop_loss": self.settings.stop_loss,
                "payout_min": self.settings.payout_min,
                "martingale_multiplier": self.settings.martingale_multiplier,
                "max_martingale": self.settings.max_martingale,
                "timeframe": self.settings.timeframe,
                "enabled_strategies": self.settings.enabled_strategies,
                "strategy_options": [
                    {"key": key, "label": label} for key, label in STRATEGY_OPTIONS
                ],
                "schedule_enabled": self.schedule_enabled,
                "schedule_start": self.schedule_start,
                "schedule_stop": self.schedule_stop,
                "real_confirmed": self.risk.real_confirmed,
            },
        }

    def hourly_sequences(self, requested_asset: str) -> tuple[dict | None, str | None]:
        if not self.client or not self.connected:
            return None, "FaÃƒÂ§a login na BullEx primeiro."
        requested_asset = requested_asset.strip().upper()
        if not requested_asset:
            return None, "Informe o nome do ativo."

        cached = self.sequence_cache.get(requested_asset)
        if cached and time.time() - cached[0] < 30:
            return cached[1], None

        if not self.analysis_lock.acquire(blocking=False):
            return None, "JÃƒÂ¡ existe uma anÃƒÂ¡lise em andamento. Aguarde alguns segundos."
        try:
            asset = self.client.resolve_active_name(requested_asset)
            endtime = int(time.time())
            candles: list[Candle] = []
            for _ in range(2):
                batch = self.client.get_candles(asset, "M1", 750, endtime=endtime)
                if not batch:
                    break
                candles.extend(batch)
                endtime = min(item.timestamp for item in batch) - 1

            cutoff = bullex_now() - timedelta(hours=24) if candles else None
            unique = {
                candle.timestamp: candle
                for candle in candles
                if candle.closed and (cutoff is None or candle.time >= cutoff)
            }
            rows = analyze_hourly_sequences(list(unique.values()))
            if not rows:
                return None, f"Nenhum candle histÃƒÂ³rico encontrado para {asset}."

            best = max(rows, key=lambda row: row["sequence"])
            result = {
                "ok": True,
                "asset": asset,
                "period": "ÃƒÅ¡ltimas 24 horas",
                "updated_at": bullex_now().strftime("%H:%M:%S"),
                "total_candles": len(unique),
                "best": best,
                "long_sequences": count_long_sequence_milestones(list(unique.values())),
                "hours": list(reversed(rows)),
            }
            self.sequence_cache[requested_asset] = (time.time(), result)
            return result, None
        except Exception as exc:
            return None, f"NÃƒÂ£o foi possÃƒÂ­vel consultar {requested_asset}: {exc}"
        finally:
            self.analysis_lock.release()

    def monitored_hourly_sequences(self, force: bool = False) -> tuple[dict | None, str | None]:
        if not self.client or not self.connected:
            return None, "FaÃƒÂ§a login na BullEx primeiro."

        monitored = self.assets or [
            Asset(name=name, active_id=0, payout=0, open=True)
            for name in ASSET_PRIORITY
        ]
        asset_names = tuple(asset.name for asset in monitored)
        now = bullex_now()
        target_time = now.replace(minute=0, second=0, microsecond=0)
        target_key = target_time.strftime("%Y-%m-%d %H:00")
        day_start = target_time.replace(hour=0)
        hour_times = [day_start + timedelta(hours=hour) for hour in range(target_time.hour + 1)]
        hour_keys = [hour.strftime("%Y-%m-%d %H:00") for hour in hour_times]
        if (
            not force
            and
            self.monitored_sequence_cache
            and self.monitored_sequence_cache[0] == target_key
            and self.monitored_sequence_cache[1] == asset_names
        ):
            cached_result = dict(self.monitored_sequence_cache[2])
            cached_result["updated_at"] = bullex_now().strftime("%H:%M:%S")
            return cached_result, None

        if not self.analysis_lock.acquire(blocking=False):
            return None, "JÃƒÂ¡ existe uma anÃƒÂ¡lise em andamento. Aguarde alguns segundos."
        try:
            rows = []
            for asset in monitored:
                try:
                    minutes_needed = min(1500, max(65, int((now - day_start).total_seconds() / 60) + 5))
                    candles: list[Candle] = []
                    endtime = int(time.time())
                    while minutes_needed > 0:
                        batch = self.client.get_candles(
                            asset.name,
                            "M1",
                            min(750, minutes_needed),
                            endtime=endtime,
                        )
                        if not batch:
                            break
                        candles.extend(batch)
                        endtime = min(item.timestamp for item in batch) - 1
                        minutes_needed -= len(batch)
                    day_candles = [
                        candle
                        for candle in {item.timestamp: item for item in candles}.values()
                        if candle.closed and candle.time.strftime("%Y-%m-%d") == target_time.strftime("%Y-%m-%d")
                    ]
                    analyzed = analyze_hourly_sequences(day_candles)
                    by_hour = {item["key"]: item for item in analyzed}
                    fallback_sequence = max(analyzed, key=lambda item: item["key"]) if analyzed else None
                    sequence = by_hour.get(target_key) or fallback_sequence or {
                        "sequence": 0,
                        "color": "DOJI",
                        "start": "-",
                        "end": "-",
                        "candles": 0,
                        "average": 0,
                        "sequence_count": 0,
                    }
                    hourly = [
                        {
                            "key": key,
                            "hour": hour_times[index].strftime("%H:00"),
                            "sequence": by_hour.get(key, {}).get("sequence", 0),
                            "average": by_hour.get(key, {}).get("average", 0),
                            "color": by_hour.get(key, {}).get("color", "DOJI"),
                        }
                        for index, key in enumerate(hour_keys)
                    ]
                    sequence_total = sum(item["sequence_count"] for item in analyzed)
                    weighted_total = sum(item["average"] * item["sequence_count"] for item in analyzed)
                    daily_average = round(weighted_total / sequence_total, 2) if sequence_total else 0
                    daily_max = max((item["sequence"] for item in analyzed), default=0)
                    daily_long_sequences = count_long_sequence_milestones(day_candles)
                    target_candles = [
                        candle for candle in day_candles
                        if candle.time.strftime("%Y-%m-%d %H:00") == target_key
                    ]
                    rows.append(
                        {
                            "asset": asset.name,
                            "payout": asset.payout,
                            "sequence": sequence["sequence"],
                            "color": sequence["color"],
                            "start": sequence["start"],
                            "end": sequence["end"],
                            "candles": sequence["candles"],
                            "average": sequence["average"],
                            "sequence_count": sequence["sequence_count"],
                            "daily_average": daily_average,
                            "daily_max": daily_max,
                            "daily_long_sequences": daily_long_sequences,
                            "hourly": hourly,
                            "close": round(target_candles[-1].close, 6) if target_candles else None,
                            "status": "ATIVO" if day_candles else "AGUARDANDO",
                        }
                    )
                except Exception:
                    rows.append(
                        {
                            "asset": asset.name,
                            "payout": asset.payout,
                            "sequence": 0,
                            "color": "DOJI",
                            "start": "-",
                            "end": "-",
                            "candles": 0,
                            "average": 0,
                            "sequence_count": 0,
                            "daily_average": 0,
                            "daily_max": 0,
                            "daily_long_sequences": count_long_sequence_milestones([]),
                            "hourly": [
                                {"key": key, "hour": hour_times[index].strftime("%H:00"), "sequence": 0, "average": 0, "color": "DOJI"}
                                for index, key in enumerate(hour_keys)
                            ],
                            "close": None,
                            "status": "AGUARDANDO",
                        }
                    )

            rows.sort(key=lambda row: (-row["daily_max"], -row["sequence"], row["asset"]))
            long_sequence_totals = {
                str(level): sum(
                    int(row.get("daily_long_sequences", {}).get("counts", {}).get(str(level), 0))
                    for row in rows
                )
                for level in LONG_SEQUENCE_LEVELS
            }
            result = {
                "ok": True,
                "period": f"{target_time.strftime('%d/%m %H:00')}Ã¢â‚¬â€œ{target_time.strftime('%H:59')}",
                "day": target_time.strftime("%d/%m/%Y"),
                "updated_at": bullex_now().strftime("%H:%M:%S"),
                "next_update": (target_time + timedelta(hours=1)).strftime("%H:00"),
                "long_sequence_totals": long_sequence_totals,
                "hours": [hour.strftime("%H:00") for hour in hour_times],
                "assets": rows,
            }
            self.monitored_sequence_cache = (target_key, asset_names, result)
            return result, None
        finally:
            self.analysis_lock.release()


class NoneLogger:
    def info(self, *_args, **_kwargs) -> None:
        return None


def signal_payload(signal: Signal | None) -> dict | None:
    if not signal:
        return None
    return {
        "asset": signal.asset,
        "active_id": signal.active_id,
        "payout": signal.payout,
        "pattern": signal.pattern,
        "direction": signal.direction,
        "sequence_color": signal.sequence_color,
        "timestamp": signal.timestamp.strftime("%H:%M:%S"),
    }


bot = WebBot()


@app.get("/", response_class=HTMLResponse)
def index() -> HTMLResponse:
    return HTMLResponse(
        Path(__file__).with_name("frontend.html").read_text(encoding="utf-8"),
        headers={
            "Cache-Control": "no-store, no-cache, must-revalidate, max-age=0",
            "Pragma": "no-cache",
            "Expires": "0",
        },
    )


@app.get("/favicon.ico", include_in_schema=False)
def favicon() -> Response:
    return Response(status_code=204)


@app.post("/api/login")
def api_login(payload: LoginPayload):
    ok, error = bot.login(payload.email, payload.password, payload.account_mode, payload.real_confirmation)
    return JSONResponse({"ok": ok, "error": error})


@app.post("/api/start")
def api_start():
    ok, error = bot.start(auto_trade=True)
    return JSONResponse({"ok": ok, "error": error})


@app.post("/api/monitor")
def api_monitor():
    ok, error = bot.start(auto_trade=False)
    return JSONResponse({"ok": ok, "error": error})


@app.post("/api/stop")
def api_stop():
    bot.stop()
    return JSONResponse({"ok": True})


@app.post("/api/logout")
def api_logout():
    bot.logout()
    return JSONResponse({"ok": True})


@app.post("/api/pause")
def api_pause():
    bot.pause()
    return JSONResponse({"ok": True})


@app.post("/api/resume")
def api_resume():
    ok, error = bot.resume()
    return JSONResponse({"ok": ok, "error": error})


@app.post("/api/settings")
def api_settings(payload: SettingsPayload):
    bot.update_settings(payload)
    return JSONResponse({"ok": True})


@app.post("/api/manual-entries")
def api_add_manual_entry(payload: ManualEntryPayload):
    ok, error = bot.add_manual_entry(payload)
    return JSONResponse({"ok": ok, "error": error})


@app.delete("/api/manual-entries/{entry_id}")
def api_delete_manual_entry(entry_id: str):
    return JSONResponse({"ok": bot.delete_manual_entry(entry_id)})


@app.post("/api/manual-entries/{entry_id}/mark-win")
def api_mark_manual_entry_win(entry_id: str):
    return JSONResponse({"ok": bot.mark_manual_entry_win(entry_id)})


@app.post("/api/manual-entries/{entry_id}/retry")
def api_retry_manual_entry(entry_id: str):
    return JSONResponse({"ok": bot.retry_manual_entry(entry_id)})


@app.get("/api/state")
def api_state():
    return JSONResponse(bot.state())


def parse_trade_datetime(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%d %H:%M:%S", "%d/%m/%Y %H:%M:%S"):
        try:
            return datetime.strptime(str(value), fmt)
        except ValueError:
            continue
    return None


def parse_trade_profit(value) -> float:
    if value is None:
        return 0.0
    if isinstance(value, (int, float)):
        return float(value)
    try:
        cleaned = str(value).replace("R$", "").replace("+", "").replace(" ", "").strip()
        return float(cleaned)
    except ValueError:
        return 0.0


def strategy_name_from_pattern(pattern: str | None) -> str:
    text = str(pattern or "").strip()
    lower = text.lower()
    if not lower:
        return "Sem estrategia registrada"
    if "entrada manual" in lower:
        return "Entrada Manual"
    if "estrategia 03" in lower:
        return "Estrategia 03"
    if "estrategia 04" in lower:
        return "Estrategia 04"
    if "estrategia 05" in lower:
        return "Estrategia 05"
    if "estrategia 02" in lower:
        return "Estrategia 02"
    if "estrategia 01" in lower or "8 candles" in lower or "8 velas" in lower:
        return "Estrategia 01"
    if "minutos sem 2 candles iguais" in lower or "par de cores atrasado" in lower or "pares 18min" in lower or "pares 13min" in lower:
        return "Pares 13min"
    if "velas 5, 6 e 7" in lower:
        return "MA21 Sem Pavio"
    if "comprar no segundo 33" in lower:
        return "CALL MA21 33s"
    if "operar vendido no segundo 33" in lower or "venda no 33" in lower:
        return "PUT MA21 33s"
    if "negativo aos 33s" in lower:
        return "CALL MA21 Virada"
    if "verde aos 33s" in lower:
        return "PUT MA21 Virada"
    if "ma21" in lower:
        return "MA21"
    return "Sem estrategia registrada"


def normalize_history_trade(trade: dict, index: int) -> dict:
    pattern = trade.get("pattern") or trade.get("motivo") or ""
    normalized = dict(trade)
    normalized["pattern"] = pattern
    normalized["strategy_name"] = strategy_name_from_pattern(pattern)
    normalized["profit"] = round(parse_trade_profit(trade.get("profit")), 2)
    normalized["direction"] = trade.get("direction") or trade.get("position") or ""
    normalized["cycle_id"] = trade.get("cycle_id") or f"legacy-{index}"
    return normalized


def trade_day_key(trade: dict) -> str:
    dt = parse_trade_datetime(trade.get("timestamp") or trade.get("time"))
    return dt.strftime("%Y-%m-%d") if dt else ""



@app.get("/api/history/stats")
def api_history_stats(day: str = ""):
    try:
        raw_trades = bot.history.all()
    except Exception:
        raw_trades = []

    requested_day = day if day else today_key()
    trades = [
        normalize_history_trade(trade, index)
        for index, trade in enumerate(raw_trades)
        if trade_day_key(trade) == requested_day
    ]
    sorted_trades = sorted(trades, key=lambda row: row.get("timestamp") or "", reverse=True)

    daily_profits: dict[str, float] = {}
    for trade in trades:
        dt = parse_trade_datetime(trade.get("timestamp"))
        if not dt:
            continue
        day_key = dt.strftime("%Y-%m-%d")
        daily_profits[day_key] = daily_profits.get(day_key, 0.0) + parse_trade_profit(trade.get("profit"))

    balance_evolution = []
    cumulative = 0.0
    for day_key in sorted(daily_profits.keys()):
        cumulative += daily_profits[day_key]
        balance_evolution.append(
            {
                "date": datetime.strptime(day_key, "%Y-%m-%d").strftime("%d/%m"),
                "profit": round(daily_profits[day_key], 2),
                "cumulative": round(cumulative, 2),
            }
        )

    cycles: dict[str, dict] = {}
    for trade in trades:
        result = trade.get("result")
        if result not in {"WIN", "LOSS", "DOJI"}:
            continue
        cycle = cycles.setdefault(
            trade["cycle_id"],
            {"strategy": trade["strategy_name"], "results": [], "profit": 0.0},
        )
        if cycle["strategy"] == "Sem estrategia registrada" and trade["strategy_name"] != "Sem estrategia registrada":
            cycle["strategy"] = trade["strategy_name"]
        cycle["results"].append(result)
        cycle["profit"] += parse_trade_profit(trade.get("profit"))

    strategy_groups: dict[str, dict] = {}
    for cycle in cycles.values():
        strategy = cycle["strategy"]
        if strategy == "Sem estrategia registrada":
            continue
        group = strategy_groups.setdefault(strategy, {"wins": 0, "losses": 0, "total": 0, "profit": 0.0})
        cycle_result = "WIN" if "WIN" in cycle["results"] else "LOSS" if "LOSS" in cycle["results"] else "DOJI"
        group["total"] += 1
        group["profit"] += cycle["profit"]
        if cycle_result == "WIN":
            group["wins"] += 1
        elif cycle_result == "LOSS":
            group["losses"] += 1

    strategies_stats = []
    for name, group in strategy_groups.items():
        win_rate = (group["wins"] / group["total"]) * 100 if group["total"] else 0.0
        strategies_stats.append(
            {
                "name": name,
                "wins": group["wins"],
                "losses": group["losses"],
                "total": group["total"],
                "profit": round(group["profit"], 2),
                "win_rate": round(win_rate, 2),
                "loss_rate": round((group["losses"] / group["total"]) * 100, 2) if group["total"] else 0.0,
            }
        )

    strategies_best = sorted(strategies_stats, key=lambda row: (row["win_rate"], row["total"]), reverse=True)
    strategies_worst = sorted(strategies_stats, key=lambda row: (row["win_rate"], -row["total"]))

    heatmap_grid = {wd: {block: {"wins": 0, "total": 0} for block in range(12)} for wd in range(7)}
    for trade in trades:
        result = trade.get("result")
        if result not in {"WIN", "LOSS"}:
            continue
        dt = parse_trade_datetime(trade.get("timestamp"))
        if not dt:
            continue
        block = dt.hour // 2
        heatmap_grid[dt.weekday()][block]["total"] += 1
        if result == "WIN":
            heatmap_grid[dt.weekday()][block]["wins"] += 1

    heatmap_data = []
    for wd in range(7):
        row_data = []
        for block in range(12):
            group = heatmap_grid[wd][block]
            rate = (group["wins"] / group["total"] * 100) if group["total"] else 0.0
            row_data.append({"block": block, "win_rate": round(rate, 2), "total": group["total"]})
        heatmap_data.append(row_data)

    available_days = [requested_day]
    selected_day = requested_day
    ops_by_hour = [0] * 24
    for trade in trades:
        dt = parse_trade_datetime(trade.get("timestamp"))
        if not dt:
            continue
        if selected_day and dt.strftime("%Y-%m-%d") != selected_day:
            continue
        ops_by_hour[dt.hour] += 1

    return JSONResponse(
        {
            "trades": sorted_trades,
            "balance_evolution": balance_evolution,
            "strategies_best": strategies_best,
            "strategies_worst": strategies_worst,
            "heatmap": heatmap_data,
            "ops_by_hour": ops_by_hour,
            "available_days": available_days,
            "selected_day": selected_day,
        }
    )


@app.post("/api/results/clear")
def api_clear_results():
    with bot.lock:
        bot.reset_session_stats()
    return JSONResponse({"ok": True})


@app.get("/api/hourly-sequences")
def api_hourly_sequences(asset: str = ""):
    result, error = bot.hourly_sequences(asset)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return JSONResponse(result)


@app.get("/api/monitored-hourly-sequences")
def api_monitored_hourly_sequences(force: bool = False):
    try:
        result, error = bot.monitored_hourly_sequences(force=force)
    except Exception as exc:
        logger.exception("Falha na rota de sequencias por hora")
        return JSONResponse({"ok": False, "error": f"Falha ao buscar sequencias: {exc}"})
    if error:
        return JSONResponse({"ok": False, "error": error})
    return JSONResponse(result or {"ok": False, "error": "Sem dados de sequencias no momento."})


HTML = r"""
<!doctype html>
<html lang="pt-BR">
<head>
  <meta charset="utf-8" />
  <meta name="viewport" content="width=device-width, initial-scale=1" />
  <title>AndersonAnalisesTrader</title>
  <style>
    :root { color-scheme: dark; --bg:#090d13; --panel:#111923; --panel2:#0c121a; --line:#1a9bd7; --text:#edf7ff; --muted:#8ba6b8; --green:#15c77f; --red:#ef4d45; --yellow:#eac84d; }
    * { box-sizing: border-box; }
    body { margin:0; font-family: Segoe UI, Arial, sans-serif; background:var(--bg); color:var(--text); }
    header { min-height:58px; display:flex; align-items:center; justify-content:space-between; gap:12px; padding:10px 22px; border-bottom:1px solid #192332; background:#0b1119; position:sticky; top:0; z-index:2; }
    h1 { font-size:18px; margin:0; color:#48c8ff; }
    h2 { margin:0 0 14px; font-size:22px; }
    main { max-width:1040px; margin:0 auto; padding:22px; }
    .panel { border:1px solid var(--line); background:var(--panel); border-radius:6px; padding:18px; }
    .center { max-width:440px; margin:56px auto; }
    .grid { display:grid; grid-template-columns: 1fr 1fr; gap:14px; }
    .stats { display:grid; grid-template-columns: repeat(5, minmax(110px, 1fr)); gap:10px; }
    .menuGrid { display:grid; grid-template-columns: repeat(2, minmax(220px, 1fr)); gap:12px; }
    input, select { width:100%; padding:12px; border-radius:5px; border:1px solid #284056; background:#07101a; color:var(--text); }
    label { display:block; color:var(--muted); margin:12px 0 6px; }
    button { border:0; border-radius:5px; padding:12px 14px; color:#061017; background:#39c5ff; font-weight:700; cursor:pointer; }
    button.secondary { background:#1d2a3a; color:var(--text); border:1px solid #2d4258; }
    button.danger { background:#ef4d45; color:white; }
    .hidden { display:none; }
    .metric { padding:12px; border:1px solid #24394c; border-radius:5px; background:var(--panel2); }
    .metric span { display:block; color:var(--muted); font-size:12px; }
    .metric strong { font-size:20px; }
    .green { color:var(--green); }
    .red { color:var(--red); }
    .yellow { color:var(--yellow); }
    table { width:100%; border-collapse:collapse; margin-top:10px; }
    th, td { padding:8px 9px; border-bottom:1px solid #1d2b3a; text-align:left; white-space:nowrap; }
    th { color:#b7d7ec; font-size:13px; }
    .badge { display:inline-block; padding:5px 8px; border-radius:4px; color:white; font-weight:700; font-size:12px; }
    .badge.green { background:var(--green); color:#03140d; }
    .badge.red { background:var(--red); }
    .badge.doji { background:#d9d9d9; color:#111; }
    tr.hot td { background:#13281e; }
    .status { color:#d7edff; line-height:1.45; }
    .price { font-size:34px; font-weight:800; margin:6px 0; }
    .pause { text-align:center; padding:70px 20px; }
    .pause h2 { color:var(--yellow); }
    .topline { display:flex; align-items:center; justify-content:space-between; gap:12px; margin-bottom:14px; }
    .nav { display:flex; gap:8px; flex-wrap:wrap; justify-content:flex-end; }
    @media (max-width: 820px) { .grid, .menuGrid, .stats { grid-template-columns:1fr; } main { padding:12px; } }
    @media (max-width: 560px) {
      header { align-items:flex-start; flex-direction:column; padding:12px; }
      h1 { font-size:16px; line-height:1.2; }
      .nav { width:100%; display:grid; grid-template-columns:repeat(3, minmax(0, 1fr)); gap:8px; }
      .nav button { width:100%; min-height:42px; padding:10px 8px; font-size:12px; }
      .center { margin:24px auto; }
      .topline { align-items:flex-start; flex-direction:column; gap:6px; }
      .menuGrid button { width:100%; min-height:46px; }
      th, td { padding:7px 8px; }
      table { display:block; overflow-x:auto; }
    }
  </style>
</head>
<body>
  <header>
    <h1>AndersonAnalisesTrader</h1>
    <div class="nav">
      <button class="secondary hidden" id="menuBtn" onclick="showMenu()">Menu inicial</button>
      <button class="danger hidden" id="stopBtn" onclick="stopBot()">Parar</button>
      <button class="danger hidden" id="logoutBtn" onclick="logout()">Deslogar</button>
    </div>
  </header>
  <main>
    <section id="login" class="panel center">
      <h2>Login BullEx</h2>
      <label>Email</label>
      <input id="email" autocomplete="username" />
      <label>Senha</label>
      <input id="password" type="password" autocomplete="current-password" />
      <label>Tipo de conta</label>
      <select id="accountMode">
        <option value="DEMO">DEMO</option>
        <option value="REAL">REAL</option>
      </select>
      <label>ConfirmaÃƒÂ§ÃƒÂ£o REAL</label>
      <input id="realConfirmation" placeholder="Digite CONFIRMO REAL para liberar operaÃƒÂ§ÃƒÂµes reais" />
      <p id="loginMsg" class="yellow"></p>
      <button onclick="login()">Entrar</button>
    </section>

    <section id="menu" class="hidden">
      <div class="topline">
        <h2>Menu inicial</h2>
        <p id="menuAccount" class="status"></p>
      </div>
      <div class="menuGrid">
        <button onclick="startBot()">Monitorar e operar</button>
        <button class="secondary" onclick="monitorOnly()">Somente monitorar</button>
        <button class="secondary" onclick="showResults()">Resultados</button>
        <button class="danger" onclick="logout()">Deslogar</button>
        <button class="danger" onclick="stopBot()">Parar robÃƒÂ´</button>
      </div>
    </section>

    <section id="results" class="hidden">
      <div class="topline"><h2>Resultados reais</h2></div>
      <div class="stats">
        <div class="metric"><span>Saldo</span><strong id="balance">-</strong></div>
        <div class="metric"><span>Taxa de WIN</span><strong id="winRate">-</strong></div>
        <div class="metric"><span>GREEN</span><strong id="greens" class="green">-</strong></div>
        <div class="metric"><span>RED</span><strong id="losses" class="red">-</strong></div>
        <div class="metric"><span>Profit</span><strong id="profit">-</strong></div>
      </div>
    </section>

    <section id="monitor" class="hidden">
      <div class="topline">
        <h2>AnÃƒÂ¡lise em tempo real</h2>
        <p id="status" class="status">Aguardando...</p>
      </div>
      <div id="pausePanel" class="panel pause hidden"></div>
      <div id="analysisPanel" class="grid">
        <div class="panel">
          <h2 id="asset">Aguardando ativo</h2>
          <p id="sequence" class="status">EstratÃƒÂ©gia do momento: EstratÃƒÂ©gia 01</p>
          <p id="signal" class="status">Sinal: aguardando</p>
          <div id="liveColor" class="badge doji">DOJI</div>
          <div id="price" class="price">-</div>
          <p id="ohlc" class="status"></p>
        </div>
        <div class="panel">
          <h2>ÃƒÅ¡ltimas velas</h2>
          <table>
            <thead><tr><th>Hora</th><th>Cor</th><th>Status</th><th>PreÃƒÂ§o</th><th>Mov.</th></tr></thead>
            <tbody id="candles"></tbody>
          </table>
        </div>
      </div>
      <div class="panel" style="margin-top:14px;">
        <h2>OperaÃƒÂ§ÃƒÂ£o</h2>
        <p id="trade">Nenhuma operaÃƒÂ§ÃƒÂ£o</p>
        <p>ÃƒÅ¡ltimo GREEN: <strong id="lastGreen" class="green">-</strong></p>
      </div>
      <div class="panel" style="margin-top:14px;">
        <h2>Ativos monitorados</h2>
        <table>
          <thead><tr><th>Ativo</th><th>Payout</th><th>Cor</th><th>SequÃƒÂªncia</th><th>PadrÃƒÂ£o</th></tr></thead>
          <tbody id="monitoredAssets"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let polling = null;

    function stopPolling() {
      if (!polling) return;
      clearInterval(polling);
      polling = null;
    }

    function showLogin(message = "") {
      $("login").classList.remove("hidden");
      $("menu").classList.add("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.add("hidden");
      $("menuBtn").classList.add("hidden");
      $("stopBtn").classList.add("hidden");
      $("logoutBtn").classList.add("hidden");
      $("loginMsg").textContent = message;
    }

    function showMenu() {
      $("login").classList.add("hidden");
      $("menu").classList.remove("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.add("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
      $("logoutBtn").classList.remove("hidden");
    }

    function showMonitor() {
      $("login").classList.add("hidden");
      $("menu").classList.add("hidden");
      $("results").classList.add("hidden");
      $("monitor").classList.remove("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
      $("logoutBtn").classList.remove("hidden");
    }

    function showResults() {
      $("login").classList.add("hidden");
      $("menu").classList.add("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.remove("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
      $("logoutBtn").classList.remove("hidden");
    }

    async function login() {
      $("loginMsg").textContent = "Conectando...";
      const res = await fetch("/api/login", {
        method:"POST",
        headers:{"Content-Type":"application/json"},
        body:JSON.stringify({
          email:$("email").value,
          password:$("password").value,
          account_mode:$("accountMode").value,
          real_confirmation:$("realConfirmation").value
        })
      });
      const data = await res.json();
      if (!data.ok) {
        $("loginMsg").textContent = data.error || "Falha no login";
        return;
      }
      $("login").classList.add("hidden");
      $("menu").classList.remove("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.add("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
      $("logoutBtn").classList.remove("hidden");
      startPolling();
    }

    async function startBot() { await fetch("/api/start", {method:"POST"}); showMonitor(); startPolling(); }
    async function monitorOnly() { await fetch("/api/monitor", {method:"POST"}); showMonitor(); startPolling(); }
    async function stopBot() { await fetch("/api/stop", {method:"POST"}); }
    async function logout() {
      stopPolling();
      await fetch("/api/logout", {method:"POST"});
      showLogin("Sessao encerrada.");
    }

    function startPolling() {
      if (polling) return;
      refresh();
      polling = setInterval(refresh, 1000);
    }

    async function refresh() {
      const data = await (await fetch("/api/state")).json();
      if (!data.connected) {
        stopPolling();
        showLogin("");
        return;
      }
      $("status").textContent = data.status;
      $("menuAccount").textContent = data.connected ? `Conta: ${data.account.mode || "-"} | Saldo: ${data.account.currency || ""} ${Number(data.account.balance || 0).toFixed(2)}` : "";
      $("balance").textContent = `${data.account.currency || ""} ${Number(data.account.balance || 0).toFixed(2)}`;
      $("winRate").textContent = data.connected ? `${data.win_rate}%` : "-";
      $("greens").textContent = data.connected ? data.greens : "-";
      $("losses").textContent = data.connected ? data.losses : "-";
      $("profit").textContent = data.connected ? Number(data.profit || 0).toFixed(2) : "-";
      $("trade").textContent = data.trade || "Nenhuma operaÃƒÂ§ÃƒÂ£o";
      $("lastGreen").textContent = data.last_green_time || "-";

      if (data.paused) {
        $("pausePanel").classList.remove("hidden");
        $("analysisPanel").classList.add("hidden");
        $("pausePanel").innerHTML = `<h2>${data.status}</h2><p>ÃƒÅ¡ltimo GREEN: <b class="green">${data.last_green_time}</b></p><p>Saldo: <b>${$("balance").textContent}</b></p>`;
        return;
      }

      $("pausePanel").classList.add("hidden");
      $("analysisPanel").classList.remove("hidden");
      $("asset").textContent = data.asset || "Aguardando ativo";
      $("sequence").textContent = `Estrategia do momento: ${data.strategy || "Estrategia 02"} - 13 minutos sem 2 velas iguais; entrada somente em verde`;
      $("signal").textContent = data.signal ? `Sinal: ${data.signal.direction} (${data.signal.pattern})` : "Sinal: aguardando estrategia";
      const last = data.candles[data.candles.length - 1];
      if (last) {
        const cls = last.color === "GREEN" ? "green" : last.color === "RED" ? "red" : "doji";
        $("liveColor").className = `badge ${cls}`;
        $("liveColor").textContent = last.color === "GREEN" ? "VERDE" : last.color === "RED" ? "VERMELHA" : "DOJI";
        $("price").textContent = Number(last.price).toFixed(6);
        $("price").className = `price ${cls === "red" ? "red" : cls === "green" ? "green" : ""}`;
        $("ohlc").textContent = `Abertura: ${last.open}  MÃƒÂ¡xima: ${last.high}  MÃƒÂ­nima: ${last.low}  Tick: ${last.tick}s`;
      }
      $("candles").innerHTML = data.candles.map(c => {
        const cls = c.color === "GREEN" ? "green" : c.color === "RED" ? "red" : "doji";
        const label = c.color === "GREEN" ? "VERDE" : c.color === "RED" ? "VERMELHA" : "DOJI";
        return `<tr><td>${c.time}</td><td><span class="badge ${cls}">${label}</span></td><td>${c.status}</td><td>${Number(c.price).toFixed(6)}</td><td class="${Number(c.move) >= 0 ? "green" : "red"}">${Number(c.move).toFixed(6)}</td></tr>`;
      }).join("");
      $("monitoredAssets").innerHTML = (data.monitored_assets || []).map(a => {
        const cls = a.color === "GREEN" ? "green" : a.color === "RED" ? "red" : "doji";
        const label = a.color === "GREEN" ? "VERDE" : a.color === "RED" ? "VERMELHA" : "DOJI";
        return `<tr class="${a.hot ? "hot" : ""}"><td><strong>${a.asset}</strong></td><td>${a.payout}%</td><td><span class="badge ${cls}">${label}</span></td><td>${a.sequence || "-"}</td><td>${a.signal || "-"}</td></tr>`;
      }).join("");
    }
  </script>
</body>
</html>
"""


