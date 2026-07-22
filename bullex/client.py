from __future__ import annotations

import json
import logging
import threading
import time
from typing import Any

from bullexapi import global_value
from bullexapi.stable_api import Bullex

from config import ASSET_PRIORITY, DEFAULT_ASSET_LIMIT, TIMEFRAMES
from models.asset import Asset
from models.candle import Candle


class BullExClient:
    def __init__(self) -> None:
        self.api: Bullex | None = None
        self.connected = False
        self.account_mode = "DEMO"
        self.candle_history_lock = threading.Lock()

    def connect(self, email: str, password: str, account_mode: str) -> tuple[bool, str | None]:
        api_mode = self._to_api_mode(account_mode)
        self._reset_global_session()
        self.api = Bullex(email, password, active_account_type=api_mode)
        ok, message = self.api.connect()
        if not ok:
            self.connected = False
            return False, self._normalize_login_error(message)
        self.api.change_balance(api_mode)
        self.connected = bool(self.api.check_connect())
        self.account_mode = account_mode
        return self.connected, None

    @staticmethod
    def _reset_global_session() -> None:
        global_value.SSID = None
        global_value.balance_id = None
        global_value.check_websocket_if_connect = None
        global_value.check_websocket_if_error = False
        global_value.websocket_error_reason = None

    @staticmethod
    def _normalize_login_error(message: Any) -> str:
        if message == "2FA":
            return "A BullEx pediu verificacao para essa conta. Entre direto no site/app da BullEx com esse usuario e confirme a conta antes de usar no robo."

        raw = "" if message is None else str(message)
        payload: Any = None
        try:
            payload = json.loads(raw)
        except (TypeError, ValueError):
            payload = None

        text_parts = [raw]
        if isinstance(payload, dict):
            for key in ("message", "error", "code", "reason", "detail"):
                value = payload.get(key)
                if value is not None:
                    text_parts.append(str(value))
        text = " ".join(text_parts).lower()

        credential_markers = (
            "credential",
            "credentials",
            "credenciais",
            "invalid_login",
            "invalid credentials",
            "invalid identifier",
            "invalid password",
            "usuario ou senha",
            "usuário ou senha",
            "email or password",
            "e-mail ou senha",
            "senha invalida",
            "senha inválida",
        )
        if any(marker in text for marker in credential_markers):
            return "Credenciais invalidas: confira o e-mail e a senha desse usuario na BullEx."

        code_markers = ("invalid code", "codigo invalido", "código inválido", "invalid verification")
        if any(marker in text for marker in code_markers):
            return "A BullEx recusou a verificacao dessa conta. Entre direto no site/app da BullEx com esse usuario e depois tente no robo."

        return raw or "Falha no login da BullEx."

    def disconnect(self) -> None:
        if self.api:
            self.api.logout()
        self.connected = False

    def get_balance(self) -> float:
        self._require_api()
        return float(self.api.get_balance())

    def get_balance_mode(self) -> str:
        self._require_api()
        mode = self.api.get_balance_mode()
        return "DEMO" if mode == "PRACTICE" else str(mode)

    def get_currency(self) -> str:
        self._require_api()
        return str(self.api.get_currency())

    def get_assets(self, payout_min: int, limit: int = DEFAULT_ASSET_LIMIT) -> list[Asset]:
        self._require_api()
        self.api.update_ACTIVES_OPCODE()
        opcodes = self.api.get_all_ACTIVES_OPCODE()
        try:
            open_time = self.api.get_all_open_time()
        except Exception:
            open_time = {}
        profits = self.api.get_all_profit()
        assets: list[Asset] = []

        names = sorted(opcodes, key=lambda name: (0 if "-OTC" in name else 1, self._priority_index(name), name))
        for name in names:
            payout = self._extract_payout(name, profits)
            is_open = self._is_open(name, open_time) or name in profits
            if not is_open or payout < payout_min:
                continue
            assets.append(Asset(name=name, active_id=int(opcodes[name]), payout=payout, open=is_open))
            if len(assets) >= limit:
                break
        return assets

    def get_priority_assets_fast(self, payout_min: int, limit: int = DEFAULT_ASSET_LIMIT) -> list[Asset]:
        self._require_api()
        opcodes = self.api.get_all_ACTIVES_OPCODE()
        assets: list[Asset] = []
        for name in ASSET_PRIORITY:
            active_id = opcodes.get(name)
            if active_id is None:
                continue
            assets.append(Asset(name=name, active_id=int(active_id), payout=max(payout_min, 80), open=True))
            if len(assets) >= limit:
                break
        return assets

    def get_payout(self, active_name: str) -> int:
        self._require_api()
        return self._extract_payout(active_name, self.api.get_all_profit())

    def resolve_active_name(self, active_name: str) -> str:
        self._require_api()
        requested = active_name.strip().upper()
        candidates = [requested]
        if "-OTC" not in requested:
            candidates.append(f"{requested}-OTC")
        else:
            candidates.append(requested.replace("-OTC", ""))
        self.api.update_ACTIVES_OPCODE()
        opcodes = self.api.get_all_ACTIVES_OPCODE()
        try:
            open_time = self.api.get_all_open_time()
        except Exception:
            open_time = {}
        profits = self.api.get_all_profit()
        names_by_upper = {name.upper(): name for name in opcodes}
        for candidate in candidates:
            name = names_by_upper.get(candidate)
            if name and (self._is_open(name, open_time) or name in profits):
                return name
        for candidate in candidates:
            name = names_by_upper.get(candidate)
            if name:
                return name
        return active_name

    def get_candles(
        self,
        active_name: str,
        timeframe: str,
        count: int = 12,
        endtime: int | None = None,
    ) -> list[Candle]:
        self._require_api()
        interval = TIMEFRAMES[timeframe]
        with self.candle_history_lock:
            raw = self.api.get_candles(active_name, interval, count, endtime or int(time.time())) or []
        return self._parse_candle_collection(raw, interval, active_name)

    def start_candles_stream(self, active_name: str, timeframe: str, maxdict: int = 30) -> bool:
        self._require_api()
        interval = TIMEFRAMES[timeframe]
        try:
            return bool(self.api.start_candles_stream(active_name, interval, maxdict))
        except Exception:
            return False

    def start_all_size_candles_stream(self, active_name: str) -> bool:
        self._require_api()
        try:
            return bool(self.api.start_candles_all_size_stream(active_name))
        except Exception:
            return False

    def stop_candles_stream(self, active_name: str, timeframe: str) -> None:
        self._require_api()
        interval = TIMEFRAMES[timeframe]
        try:
            self.api.stop_candles_stream(active_name, interval)
        except Exception:
            pass

    def get_realtime_candles(self, active_name: str, timeframe: str, count: int = 30) -> list[Candle]:
        self._require_api()
        interval = TIMEFRAMES[timeframe]
        raw = self.api.get_realtime_candles(active_name, interval)
        if not raw:
            return self.get_candles(active_name, timeframe, count)
        if isinstance(raw, dict):
            raw_items = [raw[key] for key in sorted(raw, key=lambda item: int(item))]
        else:
            raw_items = list(raw)
        candles = self._parse_candle_collection(raw_items[-count:], interval, active_name)
        if candles:
            candles[-1].closed = False
        return candles

    def _parse_candle_collection(self, raw: list[dict[str, Any]], interval: int, active_name: str) -> list[Candle]:
        candles: list[Candle] = []
        now = self._server_timestamp()
        for item in raw:
            candle = self._parse_candle(item, interval, now, active_name)
            if candle:
                candles.append(candle)
        return candles

    def buy(self, active_name: str, direction: str, value: float, duration: int) -> tuple[bool, Any]:
        self._require_api()
        result = self.api.buy(value, active_name, direction.lower(), duration)
        ok, response = result
        logging.info("[BUY_RESULT] active=%s direction=%s ok=%s response=%s", active_name, direction, ok, response)
        return result

    def get_result(self, order_id: Any) -> tuple[str, float]:
        self._require_api()
        result, profit = self.api.check_win_v4(order_id)
        logging.info("[RESULT_RAW] order=%s result=%s profit=%s", order_id, result, profit)
        raw_result = str(result or "").strip().lower()
        if raw_result in {"win", "won", "profit"}:
            status = "WIN"
        elif raw_result in {"loose", "lose", "loss", "lost"}:
            status = "LOSS"
        elif raw_result in {"equal", "draw", "doji"}:
            status = "DOJI"
        else:
            status = "WIN" if profit > 0 else "LOSS" if profit < 0 else "DOJI"
        return status, float(profit)

    def _require_api(self) -> None:
        if not self.api or not self.connected:
            raise RuntimeError("BullEx não conectado. Faça login primeiro.")

    @staticmethod
    def _to_api_mode(account_mode: str) -> str:
        return "PRACTICE" if account_mode.upper() == "DEMO" else "REAL"

    def _server_timestamp(self) -> int:
        try:
            return int(self.api.api.timesync.server_timestamp)
        except Exception:
            return int(time.time())

    @staticmethod
    def _parse_candle(item: dict[str, Any], interval: int, now: int, active_name: str) -> Candle | None:
        try:
            timestamp = BullExClient._normalize_timestamp(item.get("from") or item.get("timestamp") or item.get("time") or item.get("at"))
            update_raw = item.get("at")
            update_timestamp = BullExClient._normalize_timestamp(update_raw) if update_raw else now
            close = float(item.get("close") or item.get("value") or item.get("bid") or item.get("ask"))
            open_price = float(item.get("open", close))
            high = float(item.get("max") or item.get("high") or max(open_price, close))
            low = float(item.get("min") or item.get("low") or min(open_price, close))
            high = max(high, open_price, close)
            low = min(low, open_price, close)
            closed = timestamp + interval <= now
            return Candle(
                open=open_price,
                close=close,
                high=high,
                low=low,
                timestamp=timestamp,
                closed=closed,
                update_timestamp=update_timestamp,
                asset=active_name,
            )
        except (KeyError, TypeError, ValueError):
            return None

    @staticmethod
    def _normalize_timestamp(value: Any) -> int:
        timestamp = int(float(value))
        if timestamp > 10_000_000_000:
            timestamp = timestamp // 1000
        return timestamp

    @staticmethod
    def _extract_payout(name: str, profits: dict[str, Any]) -> int:
        data = profits.get(name, {})
        for market in ("turbo", "binary"):
            if market in data:
                return int(float(data[market]) * 100)
        for value in data.values():
            return int(float(value) * 100)
        return 0

    @staticmethod
    def _is_open(name: str, open_time: dict[str, Any]) -> bool:
        for market in ("turbo", "binary"):
            try:
                if open_time[market][name]["open"]:
                    return True
            except Exception:
                continue
        return False

    @staticmethod
    def _priority_index(name: str) -> int:
        try:
            return ASSET_PRIORITY.index(name)
        except ValueError:
            return len(ASSET_PRIORITY)
