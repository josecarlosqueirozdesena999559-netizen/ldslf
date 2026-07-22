from __future__ import annotations

import threading
import time
import json
import queue
import uuid
from dataclasses import replace
from datetime import datetime, timedelta, timezone
from pathlib import Path

from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse, Response
from pydantic import BaseModel

from bullex.account import account_snapshot
from bullex.client import BullExClient
from config import ASSET_PRIORITY
from models.asset import Asset
from models.candle import Candle
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


def bullex_now() -> datetime:
    return datetime.now(timezone.utc) - timedelta(hours=3)


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
        self.active_strategy = "8 candles"
        self.next_strategy = "Reversao 3/4/5, continuacao 4/5/6, MA21 5/6/7 ou compra no 33"
        self.schedule_enabled = False
        self.schedule_start = ""
        self.schedule_stop = ""
        self.settings_saved = False
        self.scheduler_thread: threading.Thread | None = None
        self.operation_open = False
        self.used_signal_keys: set[tuple] = set()
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
        self.analysis_lock = threading.Lock()
        self.sequence_cache: dict[str, tuple[float, dict]] = {}
        self.monitored_sequence_cache: tuple[str, tuple[str, ...], dict] | None = None
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
            return False, "Tempo limite ao conectar na BullEx. Verifique senha, bloqueio por IP/VPS ou sessão aberta em outro lugar."

    def start(self, auto_trade: bool = True, reset_stats: bool = True) -> tuple[bool, str | None]:
        with self.lock:
            if not self.client or not self.connected:
                return False, "Faça login primeiro."
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
            self.active_strategy = "8 candles"
            self.next_strategy = "Reversao 3/4/5, continuacao 4/5/6, MA21 5/6/7 ou compra no 33"
            self.status = "Carregando ativos"

        try:
            assets = self.client.get_priority_assets_fast(self.settings.payout_min, self.settings.asset_limit)
            if not assets:
                with self.lock:
                    self.starting = False
                    self.status = "Nenhum ativo aberto com payout mínimo"
                return False, "Nenhum ativo aberto com payout mínimo."
            for asset in assets:
                self.client.start_candles_stream(asset.name, self.settings.timeframe, CANDLE_LOOKBACK)
            with self.lock:
                self.assets = assets
                self.status = "Carregando candles para estrategias de 8 candles"
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
        self.stop()
        client = self.client
        if client:
            try:
                client.disconnect()
            except Exception as exc:
                logger.warning("Falha ao deslogar da BullEx: %s", exc)
        with self.lock:
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
            self.status = "Aguardando login"
            self.last_account = {"connected": False, "mode": "DEMO", "currency": "", "balance": 0.0}
            self.settings_saved = False

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
        while True:
            with self.lock:
                if not self.running:
                    return

            if self.auto_trade and not self.operation_open:
                signal = self.update_market_and_find_signal()
            else:
                self.update_candles()
                self.update_focus_asset()
                signal = self.find_best_signal()
            with self.lock:
                self.last_signal = signal
                self.status = "Escaneando ativos em tempo real / aguardando sinal"
            if signal and self.auto_trade and not self.operation_open:
                self.start_trade(signal)
            self.refresh_account_if_due()
            time.sleep(0.05)

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
        update_payout = time.time() - self.last_payout_update >= 30
        if update_payout:
            self.last_payout_update = time.time()
        for asset in self.ordered_assets():
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
            return signal
        return None

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
        best = max(
            ready,
            key=lambda asset: (
                self.asset_recency_score(asset),
                self.visual_sequence_count(asset),
                1 if asset.open else 0,
                asset.payout,
                asset.name,
            ),
        )
        self.focused_asset = best.name

    @staticmethod
    def asset_recency_score(asset: Asset) -> int:
        current = asset.current_candle
        if not current:
            return 0
        updated_at = int(current.update_timestamp or current.timestamp)
        return max(0, 120 - (int(time.time()) - updated_at))

    def find_best_signal(self) -> Signal | None:
        return self.find_signal_for_sequences(mark_used=False)

    def find_signal_for_sequences(self, mark_used: bool = True) -> Signal | None:
        signals = []
        for asset in self.assets:
            if asset.open and asset.payout >= self.settings.payout_min:
                signal = generate_signal(asset)
                if signal:
                    key = self.signal_key(asset, signal)
                    if key in self.used_signal_keys:
                        continue
                    signals.append((signal, key))
        if not signals:
            return None
        signal, key = max(signals, key=lambda item: item[0].payout)
        if mark_used:
            self.used_signal_keys.add(key)
        return signal

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
        self.trade_thread = threading.Thread(target=self.execute_trade, args=(signal,), daemon=True)
        self.trade_thread.start()

    def execute_trade(self, signal: Signal) -> None:
        try:
            account_mode = str(self.last_account.get("mode") or "DEMO")
            is_reentry = False
            trade = self.executor.execute_cycle(signal, self.settings, account_mode) if self.executor else None
            cycle_trades = self.executor.last_cycle_trades if self.executor else []
            if trade and trade.result == "WIN":
                self.add_session_cycle(cycle_trades or [trade], pattern=signal.pattern)
                self.last_green_time = time.strftime("%H:%M:%S")
                self.save_session_score()
                self.finish_cycle_after_trade()
            elif trade:
                self.add_session_cycle(cycle_trades or [trade], pattern=signal.pattern)
                self.finish_cycle_after_trade()
            else:
                if self.executor and self.executor.current_trade.startswith("Falha:"):
                    self.executor.current_trade = "Aguardando outro sinal"
                    self.update_focus_asset()
                    self.last_signal = None
                    self.used_signal_keys.discard(self.signal_key_for_signal(signal))
                elif self.executor and "stop win" in self.executor.current_trade.lower():
                    self.stop_reason = "STOP WIN atingido. Robô parado."
                    self.status = self.stop_reason
                    self.running = False
                elif self.executor and "stop loss" in self.executor.current_trade.lower():
                    self.stop_reason = "STOP LOSS atingido. Robô parado."
                    self.status = self.stop_reason
                    self.running = False
                else:
                    if self.executor and self.executor.current_trade == "Aguardando outro sinal":
                        self.used_signal_keys.discard(self.signal_key_for_signal(signal))
                    self.status = "Escaneando ativos em tempo real / aguardando sinal"
        finally:
            with self.lock:
                self.operation_open = False
            self.refresh_account()

    def reset_session_stats(self) -> None:
        self.session_wins = 0
        self.session_losses = 0
        self.session_profit = 0.0
        self.session_results = []
        self.used_signal_keys = set()
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
        
        # Clean/Format pattern to be a user-friendly Portuguese explanation
        motivo = pattern or "Estratégia do Robô"
        motivo_lower = motivo.lower()
        if "8 velas" in motivo_lower or "8 candles" in motivo_lower:
            motivo = "8 Velas Consecutivas"
        elif "reversao" in motivo_lower or "reversão" in motivo_lower:
            motivo = "Reversão de Tendência"
        elif "ma21" in motivo_lower:
            motivo = "Rompimento MA21"
        elif "compra no 33" in motivo_lower:
            motivo = "Estratégia Compra no 33"
        elif "call 33" in motivo_lower or "put 33" in motivo_lower:
            motivo = "Retração aos 33s"
            
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
            self.stop_reason = "STOP WIN atingido. Robô parado."
            self.status = self.stop_reason
            self.running = False
            return
        if self.risk.check_stop_loss(self.settings):
            self.stop_reason = "STOP LOSS atingido. Robô parado."
            self.status = self.stop_reason
            self.running = False
            return
        self.next_strategy = "Reversao 3/4/5, continuacao 4/5/6, MA21 5/6/7 ou compra no 33"
        self.active_strategy = "8 candles"
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
        self.session_wins = int(data.get("wins", self.session_wins))
        self.session_losses = int(data.get("losses", self.session_losses))
        self.session_profit = float(data.get("profit", self.session_profit))
        results = data.get("results", [])
        self.session_results = results if isinstance(results, list) else []
        self.last_green_time = str(data.get("last_green_time", self.last_green_time))
        self.risk.daily_profit = self.session_profit

    def save_session_score(self) -> None:
        data = {
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
            return False, "Direção inválida. Use COMPRA/CALL ou VENDA/PUT."
        if not entry_time:
            return False, "Horário inválido. Use HH:MM ou HH:MM:SS."

        entry = {
            "id": uuid.uuid4().hex,
            "asset": asset,
            "time": entry_time,
            "direction": direction,
            "direction_label": "COMPRA" if direction == "CALL" else "VENDA",
            "value": value,
            "market": market,
            "status": "AGUARDANDO",
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
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
        now = datetime.now()
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
                    self.last_green_time = time.strftime("%H:%M:%S")
                    self.save_session_score()
                entry["status"] = "WIN" if win_trade else trade.result
                entry["last_executed_date"] = datetime.now().strftime("%Y-%m-%d")
                entry["message"] = f"Resultado automático: {'WIN' if win_trade else trade.result} lucro {sum(float(item.profit or 0) for item in cycle_trades or [trade]):.2f}"
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
            entry["last_executed_date"] = datetime.now().strftime("%Y-%m-%d")
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
            rows.append(
                {
                    "asset": asset.name,
                    "payout": asset.payout,
                    "open": asset.open,
                    "sequence": asset.sequence,
                    "signal": signal,
                    "color": candle_color(last) if last else "DOJI",
                    "hot": hot,
                }
            )
        return sorted(rows, key=lambda row: (not row["hot"], row["asset"]))[:20]

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
            "title": "Escaneando estrategias sem ordem fixa",
            "detail": "8 candles, reversao, continuacao, MA21 contra, compra no segundo 33 e rompimento MA21 aos 33s",
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
            "starting": self.starting,
            "paused": paused,
            "status": self.status,
            "reentry_status": self.reentry_status(),
            "stop_reason": self.stop_reason,
            "manual_paused": self.manual_paused,
            "settings_saved": self.settings_saved,
            "account": self.last_account,
            "strategy": "8 candles",
            "strategy_detail": "Reversao: 2 candles contrarios e entradas 3/4/5. Continuacao: 3 candles iguais e entradas 4/5/6. MA21: vermelho sem pavio abaixo da media, fechado ate 33s, mais 4 verdes e entradas 5/6/7. Compra no 33: verde acima da MA21 fechado depois de 33s, com 2 entradas. CALL 33 MA21: candle verde rompe a MA21 para cima; candle seguinte fica negativo aos 33s e fecha verde positivo, uma CALL nas velas 3/4/5. PUT 33 MA21: candle vermelho rompe a MA21 para baixo; candle seguinte fica verde aos 33s e fecha vermelho negativo, uma PUT nas velas 3/4/5. Sempre com martingale dobrando.",
            "strategy_moment": strategy_moment["title"],
            "strategy_moment_detail": strategy_moment["detail"],
            "target_sequence": self.active_strategy,
            "next_sequence": self.next_strategy,
            "asset": focus.name if focus else None,
            "sequence": self.visual_sequence(focus) if focus else "-",
            "signal": signal_payload(self.last_signal) if self.last_signal else None,
            "trade": self.executor.current_trade if self.executor else "Nenhuma operação",
            "last_green_time": self.last_green_time,
            "moving_average": {
                key: round(value, 6) if isinstance(value, float) else value
                for key, value in moving_average.items()
            },
            "candles": candles,
            "monitored_assets": monitored_assets,
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
                "schedule_enabled": self.schedule_enabled,
                "schedule_start": self.schedule_start,
                "schedule_stop": self.schedule_stop,
                "real_confirmed": self.risk.real_confirmed,
            },
        }

    def hourly_sequences(self, requested_asset: str) -> tuple[dict | None, str | None]:
        if not self.client or not self.connected:
            return None, "Faça login na BullEx primeiro."
        requested_asset = requested_asset.strip().upper()
        if not requested_asset:
            return None, "Informe o nome do ativo."

        cached = self.sequence_cache.get(requested_asset)
        if cached and time.time() - cached[0] < 30:
            return cached[1], None

        if not self.analysis_lock.acquire(blocking=False):
            return None, "Já existe uma análise em andamento. Aguarde alguns segundos."
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
                return None, f"Nenhum candle histórico encontrado para {asset}."

            best = max(rows, key=lambda row: row["sequence"])
            result = {
                "ok": True,
                "asset": asset,
                "period": "Últimas 24 horas",
                "updated_at": bullex_now().strftime("%H:%M:%S"),
                "total_candles": len(unique),
                "best": best,
                "long_sequences": count_long_sequence_milestones(list(unique.values())),
                "hours": list(reversed(rows)),
            }
            self.sequence_cache[requested_asset] = (time.time(), result)
            return result, None
        except Exception as exc:
            return None, f"Não foi possível consultar {requested_asset}: {exc}"
        finally:
            self.analysis_lock.release()

    def monitored_hourly_sequences(self, force: bool = False) -> tuple[dict | None, str | None]:
        if not self.client or not self.connected:
            return None, "Faça login na BullEx primeiro."

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
            return self.monitored_sequence_cache[2], None

        if not self.analysis_lock.acquire(blocking=False):
            return None, "Já existe uma análise em andamento. Aguarde alguns segundos."
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
                            "status": "ATIVO" if day_candles else "SEM DADOS",
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
                            "status": "INDISPONÍVEL",
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
                "period": f"{target_time.strftime('%d/%m %H:00')}–{target_time.strftime('%H:59')}",
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
    result, error = bot.monitored_hourly_sequences(force=force)
    if error:
        return JSONResponse({"ok": False, "error": error}, status_code=400)
    return JSONResponse(result)


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
    header { height:58px; display:flex; align-items:center; justify-content:space-between; padding:0 22px; border-bottom:1px solid #192332; background:#0b1119; position:sticky; top:0; z-index:2; }
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
    .nav { display:flex; gap:8px; }
    @media (max-width: 820px) { .grid, .menuGrid, .stats { grid-template-columns:1fr; } main { padding:12px; } }
  </style>
</head>
<body>
  <header>
    <h1>AndersonAnalisesTrader</h1>
    <div class="nav">
      <button class="secondary hidden" id="menuBtn" onclick="showMenu()">Menu inicial</button>
      <button class="danger hidden" id="stopBtn" onclick="stopBot()">Parar</button>
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
      <label>Confirmação REAL</label>
      <input id="realConfirmation" placeholder="Digite CONFIRMO REAL para liberar operações reais" />
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
        <button class="danger" onclick="stopBot()">Parar robô</button>
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
        <h2>Análise em tempo real</h2>
        <p id="status" class="status">Aguardando...</p>
      </div>
      <div id="pausePanel" class="panel pause hidden"></div>
      <div id="analysisPanel" class="grid">
        <div class="panel">
          <h2 id="asset">Aguardando ativo</h2>
          <p id="sequence" class="status">Estratégia do momento: 8 candles</p>
          <p id="signal" class="status">Sinal: aguardando</p>
          <div id="liveColor" class="badge doji">DOJI</div>
          <div id="price" class="price">-</div>
          <p id="ohlc" class="status"></p>
        </div>
        <div class="panel">
          <h2>Últimas velas</h2>
          <table>
            <thead><tr><th>Hora</th><th>Cor</th><th>Status</th><th>Preço</th><th>Mov.</th></tr></thead>
            <tbody id="candles"></tbody>
          </table>
        </div>
      </div>
      <div class="panel" style="margin-top:14px;">
        <h2>Operação</h2>
        <p id="trade">Nenhuma operação</p>
        <p>Último GREEN: <strong id="lastGreen" class="green">-</strong></p>
      </div>
      <div class="panel" style="margin-top:14px;">
        <h2>Ativos monitorados</h2>
        <table>
          <thead><tr><th>Ativo</th><th>Payout</th><th>Cor</th><th>Sequência</th><th>Padrão</th></tr></thead>
          <tbody id="monitoredAssets"></tbody>
        </table>
      </div>
    </section>
  </main>
  <script>
    const $ = (id) => document.getElementById(id);
    let polling = null;

    function showMenu() {
      $("login").classList.add("hidden");
      $("menu").classList.remove("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.add("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
    }

    function showMonitor() {
      $("login").classList.add("hidden");
      $("menu").classList.add("hidden");
      $("results").classList.add("hidden");
      $("monitor").classList.remove("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
    }

    function showResults() {
      $("login").classList.add("hidden");
      $("menu").classList.add("hidden");
      $("monitor").classList.add("hidden");
      $("results").classList.remove("hidden");
      $("menuBtn").classList.remove("hidden");
      $("stopBtn").classList.remove("hidden");
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
      startPolling();
    }

    async function startBot() { await fetch("/api/start", {method:"POST"}); showMonitor(); startPolling(); }
    async function monitorOnly() { await fetch("/api/monitor", {method:"POST"}); showMonitor(); startPolling(); }
    async function stopBot() { await fetch("/api/stop", {method:"POST"}); }

    function startPolling() {
      if (polling) return;
      refresh();
      polling = setInterval(refresh, 1000);
    }

    async function refresh() {
      const data = await (await fetch("/api/state")).json();
      $("status").textContent = data.status;
      $("menuAccount").textContent = data.connected ? `Conta: ${data.account.mode || "-"} | Saldo: ${data.account.currency || ""} ${Number(data.account.balance || 0).toFixed(2)}` : "";
      $("balance").textContent = `${data.account.currency || ""} ${Number(data.account.balance || 0).toFixed(2)}`;
      $("winRate").textContent = data.connected ? `${data.win_rate}%` : "-";
      $("greens").textContent = data.connected ? data.greens : "-";
      $("losses").textContent = data.connected ? data.losses : "-";
      $("profit").textContent = data.connected ? Number(data.profit || 0).toFixed(2) : "-";
      $("trade").textContent = data.trade || "Nenhuma operação";
      $("lastGreen").textContent = data.last_green_time || "-";

      if (data.paused) {
        $("pausePanel").classList.remove("hidden");
        $("analysisPanel").classList.add("hidden");
        $("pausePanel").innerHTML = `<h2>${data.status}</h2><p>Último GREEN: <b class="green">${data.last_green_time}</b></p><p>Saldo: <b>${$("balance").textContent}</b></p>`;
        return;
      }

      $("pausePanel").classList.add("hidden");
      $("analysisPanel").classList.remove("hidden");
      $("asset").textContent = data.asset || "Aguardando ativo";
      $("sequence").textContent = `Estratégia do momento: ${data.strategy || "8 candles"} - reversao 3/4/5, continuacao 4/5/6, MA21 5/6/7 ou compra no 33`;
      $("signal").textContent = data.signal ? `Sinal: ${data.signal.direction} (${data.signal.pattern})` : "Sinal: aguardando 8 candles";
      const last = data.candles[data.candles.length - 1];
      if (last) {
        const cls = last.color === "GREEN" ? "green" : last.color === "RED" ? "red" : "doji";
        $("liveColor").className = `badge ${cls}`;
        $("liveColor").textContent = last.color === "GREEN" ? "VERDE" : last.color === "RED" ? "VERMELHA" : "DOJI";
        $("price").textContent = Number(last.price).toFixed(6);
        $("price").className = `price ${cls === "red" ? "red" : cls === "green" ? "green" : ""}`;
        $("ohlc").textContent = `Abertura: ${last.open}  Máxima: ${last.high}  Mínima: ${last.low}  Tick: ${last.tick}s`;
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



