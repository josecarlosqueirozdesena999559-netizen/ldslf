from __future__ import annotations

from rich import box
from rich.console import Console
from rich.layout import Layout
from rich.panel import Panel
from rich.table import Table
from rich.text import Text

from models.asset import Asset
from models.candle import Candle
from models.settings import BotSettings
from models.trade import Signal
from robot.strategy import candle_color, describe_latest_sequence


class TerminalUI:
    def __init__(self) -> None:
        self.console = Console()

    def title(self) -> None:
        self.console.print(
            Panel.fit(
                "[bold cyan]ROBO BULLEX - CMD TERMINAL[/bold cyan]",
                border_style="cyan",
                padding=(1, 6),
            )
        )

    def render_dashboard(
        self,
        account: dict,
        assets: list[Asset],
        settings: BotSettings,
        signal: Signal | None,
        current_trade: str,
        history_summary: dict,
        status: str,
        auto_trade: bool = False,
    ) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=10),
            Layout(name="middle", ratio=2),
            Layout(name="bottom", size=13),
        )
        layout["top"].split_row(
            Layout(self.render_account_panel(account), name="account"),
            Layout(self.render_summary(history_summary, settings, status, auto_trade), name="summary"),
        )
        layout["middle"].split_row(
            Layout(self.render_assets_table(assets), name="assets", ratio=2),
            Layout(self.render_chart(self._focus_asset(assets)), name="chart"),
        )
        layout["bottom"].split_row(
            Layout(self.render_signal_panel(signal), name="signal"),
            Layout(self.render_trade_panel(current_trade), name="trade"),
            Layout(self.render_history(history_summary), name="history"),
        )
        return layout

    def render_individual_monitor(
        self,
        account: dict,
        assets: list[Asset],
        focused_asset_name: str | None,
        settings: BotSettings,
        signal: Signal | None,
        current_trade: str,
        status: str,
        auto_trade: bool = False,
    ) -> Layout:
        asset = self._focus_asset(assets, focused_asset_name, require_focus=True)
        layout = Layout()
        layout.split_column(
            Layout(name="live", size=12),
            Layout(name="candles", ratio=1),
        )
        layout["live"].update(
            self.render_big_candle_panel(
                asset=asset,
                settings=settings,
                signal=signal,
                current_trade=current_trade,
                status=status,
                auto_trade=auto_trade,
                account=account,
            )
        )
        layout["candles"].update(self.render_candles_table(asset))
        return layout

    def render_pair_watch_monitor(
        self,
        account: dict,
        assets: list[Asset],
        states: dict[str, dict],
        settings: BotSettings,
        current_trade: str,
        status: str,
    ) -> Layout:
        layout = Layout()
        layout.split_column(
            Layout(name="top", size=9),
            Layout(name="table", ratio=1),
            Layout(name="bottom", size=6),
        )
        layout["top"].split_row(
            Layout(self.render_account_panel(account), name="account"),
            Layout(
                Panel(
                    f"Status: [bold]{status}[/bold]\n"
                    f"Limite: [bold]{settings.pair_watch_minutes} minutos[/bold]\n"
                    "Regra: 1 verde = CALL se atrasar; 1 vermelho = PUT se atrasar; 2 iguais = respeitou",
                    title="Monitor de pares",
                    border_style="yellow",
                ),
                name="summary",
            ),
        )
        layout["table"].update(self.render_pair_watch_table(assets, states, settings))
        layout["bottom"].update(self.render_trade_panel(current_trade))
        return layout

    def render_pair_watch_table(self, assets: list[Asset], states: dict[str, dict], settings: BotSettings) -> Panel:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for column in ("Ativo", "Payout", "Ultimas cores", "Tendencia", "Marcado", "Tempo", "Status"):
            table.add_column(column, no_wrap=False)

        for asset in assets:
            state = states.get(asset.name, {})
            target = state.get("target_color") or "-"
            elapsed = self._format_seconds(int(state.get("elapsed_seconds", 0)))
            status = state.get("status", "Aguardando")
            style = "bold red" if state.get("alert") else "green" if state.get("respected") else None
            table.add_row(
                asset.name,
                f"{asset.payout}%",
                self._format_pair_watch_colors(state.get("last_colors", "-")),
                state.get("trend", "-"),
                self._format_pair_watch_color(target),
                elapsed,
                status,
                style=style,
            )
        return Panel(table, title="Pares de cores monitorados", border_style="cyan")

    def render_pause_monitor(
        self,
        account: dict,
        status: str,
        current_trade: str,
        last_green_time: str,
    ) -> Panel:
        body = (
            f"[bold yellow]{status}[/bold yellow]\n\n"
            f"Último GREEN: [bold green]{last_green_time}[/bold green]\n"
            f"Operação: [bold]{current_trade or 'Nenhuma'}[/bold]\n"
            f"Saldo: [bold]{account.get('currency', '')} {float(account.get('balance', 0)):.2f}[/bold]\n\n"
            "Análise pausada. O robô não está olhando nenhum ativo agora."
        )
        return Panel(body, title="Pausa após GREEN", border_style="green")

    def render_account_panel(self, account: dict) -> Panel:
        body = (
            f"Conectado: [bold]{account.get('connected', False)}[/bold]\n"
            f"Conta: [bold]{account.get('mode', '-')}[/bold]\n"
            f"Saldo: [bold]{account.get('currency', '')} {float(account.get('balance', 0)):.2f}[/bold]"
        )
        return Panel(body, title="Conta", border_style="green")

    def render_assets_table(self, assets: list[Asset]) -> Panel:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for column in ("Ativo", "ID", "Payout", "Aberto", "Últimos 5", "Atual", "Tempo real", "Sequência", "Sinal"):
            table.add_column(column, no_wrap=False)
        for asset in assets:
            closed = [candle for candle in asset.candles if candle.closed]
            current = next((candle for candle in reversed(asset.candles) if not candle.closed), asset.current_candle)
            live_price = f"{current.close:.6f}" if current else "-"
            table.add_row(
                asset.name,
                str(asset.active_id),
                f"{asset.payout}%",
                "SIM" if asset.open else "NAO",
                " ".join(self._candle_icon(candle) for candle in closed[-5:]) or "-",
                self._candle_icon(current) if current else "-",
                live_price,
                asset.sequence,
                asset.signal,
            )
        return Panel(table, title="10 Ativos", border_style="cyan")

    def render_signal_panel(self, signal: Signal | None) -> Panel:
        if not signal:
            body = "Nenhum sinal no momento"
        else:
            body = (
                f"Ativo: [bold]{signal.asset}[/bold]\n"
                f"Payout: {signal.payout}%\n"
                f"Padrão: {signal.pattern}\n"
                f"Entrada: [bold yellow]{signal.direction}[/bold yellow]\n"
                f"Horário: {signal.timestamp:%H:%M:%S}\n"
                "Status: Aguardando próxima vela"
            )
        return Panel(body, title="Sinal encontrado", border_style="yellow")

    def render_trade_panel(self, current_trade: str) -> Panel:
        return Panel(current_trade or "Nenhuma operação", title="Operação atual", border_style="magenta")

    def render_history(self, summary: dict) -> Panel:
        last = summary.get("last") or {}
        body = (
            f"Wins: [green]{summary.get('wins', 0)}[/green]\n"
            f"Losses: [red]{summary.get('losses', 0)}[/red]\n"
            f"Profit: [bold]{float(summary.get('profit', 0)):.2f}[/bold]\n"
            f"Ultima: {last.get('asset', '-') if isinstance(last, dict) else '-'} "
            f"{last.get('result', '') if isinstance(last, dict) else ''}"
        )
        return Panel(body, title="Histórico WIN/LOSS", border_style="blue")

    def render_summary(self, summary: dict, settings: BotSettings, status: str, auto_trade: bool) -> Panel:
        body = (
            f"Status: [bold]{status}[/bold]\n"
            f"Entrada automática: [bold]{'DEMO ligada' if auto_trade else 'desligada'}[/bold]\n"
            f"Stop win: {settings.stop_win:.2f}\n"
            f"Stop loss: {settings.stop_loss:.2f}\n"
            f"Timeframe: {settings.timeframe}\n"
            f"Payout mínimo: {settings.payout_min}%\n"
            f"E01: 8V+2R=PUT; E03: 8V+3R=CALL; E04: R G R G=PUT/G1 CALL; E05: G R G R=CALL/G1 PUT"
        )
        return Panel(body, title="Resumo", border_style="white")

    def render_chart(self, asset: Asset | None) -> Panel:
        if not asset or not asset.candles:
            return Panel("Sem candles ainda", title="Gráfico", border_style="green")
        closes = [candle.close for candle in asset.candles[-20:]]
        high = max(candle.high for candle in asset.candles[-20:])
        low = min(candle.low for candle in asset.candles[-20:])
        chart = self._sparkline(closes)
        current = next((candle for candle in reversed(asset.candles) if not candle.closed), asset.current_candle)
        closed = [candle for candle in asset.candles if candle.closed][-5:]
        details = "\n".join(
            f"{candle.time:%H:%M} {candle_color(candle):5} O:{candle.open:.6f} C:{candle.close:.6f}"
            for candle in closed
        )
        _color, count, sequence = self._visual_sequence(asset)
        body = (
            f"[bold]{asset.name}[/bold]\n"
            f"{chart}\n"
            f"Close: {closes[-1]:.6f}\n"
            f"Máxima: {high:.6f}  Mínima: {low:.6f}\n"
            f"Candle atual: {self._candle_icon(current)} {current.time:%H:%M:%S}\n"
            f"Sequência agora: {sequence} ({count})\n"
            f"{details}"
        )
        return Panel(body, title="Gráfico em tempo real", border_style="green")

    def render_big_candle_panel(
        self,
        asset: Asset | None,
        settings: BotSettings,
        signal: Signal | None,
        current_trade: str,
        status: str,
        auto_trade: bool,
        account: dict,
    ) -> Panel:
        if not asset or not asset.candles:
            return Panel("Aguardando ativo com sequência de candles", title="Monitor individual", border_style="green")

        candles = self._asset_candles(asset)[-24:]
        if not candles:
            return Panel(f"Aguardando candles de {asset.name}", title="Monitor individual", border_style="green")
        current = next((candle for candle in reversed(candles) if not candle.closed), candles[-1])
        closed = [candle for candle in candles if candle.closed]
        _color, count, sequence = describe_latest_sequence(asset)
        trend_line = self._candle_blocks(closed[-20:])
        live_color = candle_color(current)
        progress = self._candle_progress(current, settings.timeframe)
        movement = current.close - current.open
        freshness = self._tick_freshness(current)
        signal_text = f"{signal.direction} na próxima vela" if signal else "aguardando Estratégia 01"
        trade_text = current_trade or "Nenhuma operação"
        price_style = "bold white on green" if live_color == "GREEN" else "bold white on red" if live_color == "RED" else "bold black on white"
        body = (
            f"[bold]{asset.name}[/bold]   Sequência: [bold]{sequence}[/bold] ({count})   {status}\n"
            f"Operação: [bold]{trade_text}[/bold]\n"
            f"Sinal: [bold]{signal_text}[/bold]   Gale: [bold]G1 e G2 dobrando[/bold]\n"
            f"{trend_line}\n"
            f"Tempo: {progress}\n\n"
            f"AO VIVO: {self._large_candle_icon(current)}   Preço: [{price_style}] {current.close:.6f} [/]\n"
            f"Abertura: [bold]{current.open:.6f}[/bold]   Movimento: [bold]{movement:+.6f}[/bold]\n"
            f"Máxima: [bold]{current.high:.6f}[/bold]   Mínima: [bold]{current.low:.6f}[/bold]   "
            f"Atualizado BullEx: [bold]{current.update_time:%H:%M:%S}[/bold]   Tick: [bold]{freshness}[/bold]"
        )
        border = "green" if live_color == "GREEN" else "red" if live_color == "RED" else "white"
        return Panel(body, title="Ativo analisado - vela em tempo real", border_style=border)

    def render_candles_table(self, asset: Asset | None) -> Panel:
        table = Table(box=box.SIMPLE_HEAVY, expand=True)
        for column in ("Hora", "Cor", "Status", "Preço", "Mov."):
            table.add_column(column, no_wrap=True)

        title = "Últimas velas do ativo analisado"
        if asset:
            title = f"Últimas velas de {asset.name}"
            for candle in self._asset_candles(asset)[-18:]:
                status = "[bold yellow]EM ANDAMENTO[/bold yellow]" if not candle.closed else "FECHADA"
                move = candle.close - candle.open
                table.add_row(
                    (candle.update_time if not candle.closed else candle.time).strftime("%H:%M:%S"),
                    self._candle_label(candle),
                    status,
                    f"{candle.close:.6f}",
                    f"{move:+.6f}",
                    style=self._candle_row_style(candle),
                )
        return Panel(table, title=title, border_style="cyan")

    def render_asset_strip(self, assets: list[Asset], focus: Asset | None) -> Panel:
        if not assets:
            return Panel("-", title="Ativos monitorados", border_style="cyan")
        lines = []
        for asset in assets[:12]:
            marker = ">" if focus and asset.name == focus.name else " "
            lines.append(f"{marker} {asset.name} {asset.payout}% {asset.sequence} {asset.signal}")
        return Panel("\n".join(lines), title="Ativos monitorados - foco automático na maior sequência", border_style="cyan")

    @staticmethod
    def _focus_asset(assets: list[Asset], focused_asset_name: str | None = None, require_focus: bool = False) -> Asset | None:
        ready_assets = [asset for asset in assets if asset.candles]
        if not ready_assets:
            return None
        if focused_asset_name:
            focused = next((asset for asset in ready_assets if asset.name == focused_asset_name), None)
            if focused:
                return focused
        if require_focus:
            return None

        def score(asset: Asset) -> tuple[int, int]:
            _color, count, _label = TerminalUI._visual_sequence(asset)
            return count, asset.payout

        return max(ready_assets, key=score)

    @staticmethod
    def _asset_candles(asset: Asset) -> list[Candle]:
        return [candle for candle in asset.candles if not candle.asset or candle.asset == asset.name]

    @staticmethod
    def _visual_sequence(asset: Asset) -> tuple[str | None, int, str]:
        candles = TerminalUI._asset_candles(asset)
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

    @staticmethod
    def _candle_icon(candle: Candle | None) -> str:
        if not candle:
            return "-"
        color = candle_color(candle)
        if color == "GREEN":
            return "[green]G[/green]"
        if color == "RED":
            return "[red]R[/red]"
        return "[white]D[/white]"

    @staticmethod
    def _large_candle_icon(candle: Candle | None) -> str:
        if not candle:
            return "-"
        color = candle_color(candle)
        if color == "GREEN":
            return "[bold white on green]  VERDE SUBINDO  [/bold white on green]"
        if color == "RED":
            return "[bold white on red]  VERMELHA CAINDO  [/bold white on red]"
        return "[bold black on white]  DOJI PARADA  [/bold black on white]"

    @staticmethod
    def _candle_label(candle: Candle) -> str:
        color = candle_color(candle)
        if color == "GREEN":
            return "[bold white on green] VERDE [/bold white on green]"
        if color == "RED":
            return "[bold white on red] VERMELHA [/bold white on red]"
        return "[bold black on white] DOJI [/bold black on white]"

    @staticmethod
    def _candle_row_style(candle: Candle) -> str:
        if not candle.closed:
            return "bold"
        color = candle_color(candle)
        if color == "GREEN":
            return "green"
        if color == "RED":
            return "red"
        return "white"

    @staticmethod
    def _candle_blocks(candles: list[Candle]) -> Text:
        text = Text()
        if not candles:
            text.append("Sem velas fechadas")
            return text
        for candle in candles:
            color = candle_color(candle)
            if color == "GREEN":
                text.append(" G ", style="bold white on green")
            elif color == "RED":
                text.append(" R ", style="bold white on red")
            else:
                text.append(" D ", style="bold black on white")
        return text

    @staticmethod
    def _candle_progress(candle: Candle, timeframe: str) -> Text:
        import time

        seconds_by_timeframe = {"M1": 60, "M5": 300, "M15": 900}
        total = seconds_by_timeframe.get(timeframe, 60)
        now = candle.update_timestamp if not candle.closed and candle.update_timestamp else int(time.time())
        elapsed = max(0, min(total, int(now) - candle.timestamp))
        filled = int((elapsed / total) * 24)
        text = Text()
        text.append("[", style="white")
        text.append("#" * filled, style="bold cyan")
        text.append("-" * (24 - filled), style="dim")
        text.append(f"] {elapsed:02d}/{total}s", style="white")
        return text

    @staticmethod
    def _tick_freshness(candle: Candle) -> str:
        import time

        if not candle.update_timestamp:
            return "-"
        age = max(0, int(time.time()) - int(candle.update_timestamp))
        return f"{age}s"

    @staticmethod
    def _sparkline(values: list[float]) -> Text:
        ticks = "._-~=*#"
        if not values:
            return Text("-")
        low = min(values)
        high = max(values)
        span = high - low or 1
        text = Text()
        for value in values:
            index = int((value - low) / span * (len(ticks) - 1))
            text.append(ticks[index], style="cyan")
        return text

    @staticmethod
    def _format_seconds(seconds: int) -> str:
        minutes, secs = divmod(max(0, seconds), 60)
        hours, minutes = divmod(minutes, 60)
        if hours:
            return f"{hours:02d}:{minutes:02d}:{secs:02d}"
        return f"{minutes:02d}:{secs:02d}"

    @staticmethod
    def _format_pair_watch_color(color: str) -> str:
        if color == "GREEN":
            return "[bold white on green] VERDE [/bold white on green]"
        if color == "RED":
            return "[bold white on red] VERMELHO [/bold white on red]"
        return "-"

    @classmethod
    def _format_pair_watch_colors(cls, colors: str) -> Text:
        text = Text()
        for color in str(colors or "-").split():
            if color == "GREEN":
                text.append(" G ", style="bold white on green")
            elif color == "RED":
                text.append(" R ", style="bold white on red")
            elif color == "DOJI":
                text.append(" D ", style="bold black on white")
            else:
                text.append("-")
            text.append(" ")
        return text
