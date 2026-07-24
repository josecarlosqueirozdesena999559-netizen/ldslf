from __future__ import annotations

from getpass import getpass

from rich.prompt import Confirm, FloatPrompt, IntPrompt, Prompt

from app.logger import setup_logger
from app.terminal_ui import TerminalUI
from bullex.account import account_snapshot
from bullex.client import BullExClient
from config import DEFAULT_PAYOUT, DEFAULT_TIMEFRAME, TIMEFRAMES
from models.settings import BotSettings
from robot.engine import RobotEngine
from robot.risk import RiskManager
from storage.history import HistoryStore


class BotMenu:
    def __init__(self) -> None:
        self.ui = TerminalUI()
        self.logger = setup_logger()
        self.client: BullExClient | None = None
        self.settings = BotSettings()
        self.risk = RiskManager()
        self.history = HistoryStore()
        self.account_mode = "DEMO"
        self.engine: RobotEngine | None = None

    def run(self) -> None:
        while True:
            self.ui.console.clear()
            self.ui.title()
            self.ui.console.print(
                "\n[bold]1[/bold] - Login BullEx\n"
                "[bold]2[/bold] - Selecionar tipo de conta DEMO/REAL\n"
                "[bold]3[/bold] - Configurar robo\n"
                "[bold]4[/bold] - Monitorar e operar automaticamente na conta selecionada\n"
                "[bold]5[/bold] - Monitorar candles em tempo real sem operar\n"
                "[bold]6[/bold] - Monitorar e operar pares de cores atrasados 18 minutos\n"
                "[bold]7[/bold] - Parar robo\n"
                "[bold]8[/bold] - Ver historico\n"
                "[bold]9[/bold] - Sair\n"
            )
            option = Prompt.ask("Escolha", choices=["1", "2", "3", "4", "5", "6", "7", "8", "9"])
            if option == "1":
                self.login()
            elif option == "2":
                self.select_account()
            elif option == "3":
                self.configure()
            elif option == "4":
                self.start_robot(auto_trade=True, display_mode="individual")
            elif option == "5":
                self.start_robot(auto_trade=False, display_mode="individual")
            elif option == "6":
                self.start_robot(auto_trade=True, display_mode="pair_watch")
            elif option == "7":
                self.stop_robot()
            elif option == "8":
                self.show_history()
            elif option == "9":
                self.stop_robot()
                break

    def login(self) -> None:
        email = Prompt.ask("Email BullEx")
        password = getpass("Senha BullEx: ")
        self.select_account()
        if self.account_mode == "REAL":
            text = Prompt.ask("Digite CONFIRMO REAL para liberar operacoes em REAL", default="")
            if not self.risk.confirm_real(text):
                self.ui.console.print("[yellow]A conta REAL sera conectada, mas as operacoes ficarao bloqueadas.[/yellow]")

        self.client = BullExClient()
        ok, error = self.client.connect(email, password, self.account_mode)
        if ok:
            self.logger.info("[LOGIN] conectado com sucesso")
            snapshot = account_snapshot(self.client)
            self.ui.console.print(self.ui.render_account_panel(snapshot))
        else:
            self.logger.info("[LOGIN] falha: %s", error)
            self.ui.console.print(f"[red]Falha no login:[/red] {error}")
        Prompt.ask("Pressione ENTER para continuar", default="")

    def select_account(self) -> None:
        option = Prompt.ask("Tipo de conta: 1 DEMO / 2 REAL", choices=["1", "2"], default="1")
        self.account_mode = "DEMO" if option == "1" else "REAL"
        if self.account_mode == "DEMO":
            self.risk.real_confirmed = False

    def configure(self) -> None:
        self.settings.entry_value = FloatPrompt.ask("Valor da entrada inicial", default=self.settings.entry_value)
        self.settings.stop_win = FloatPrompt.ask("Stop win diario", default=self.settings.stop_win)
        self.settings.stop_loss = FloatPrompt.ask("Stop loss diario", default=self.settings.stop_loss)
        self.settings.timeframe = Prompt.ask("Timeframe", choices=list(TIMEFRAMES), default=DEFAULT_TIMEFRAME)
        self.settings.payout_min = IntPrompt.ask("Payout minimo", default=DEFAULT_PAYOUT)
        self.settings.martingale_enabled = Confirm.ask("Martingale ativo?", default=True)
        self.settings.max_martingale = 2
        self.settings.martingale_multiplier = 2.0
        self.settings.asset_limit = max(1, IntPrompt.ask("Quantidade de ativos no monitor", default=self.settings.asset_limit))
        self.settings.pair_watch_minutes = max(
            1,
            IntPrompt.ask(
                "Minutos para alertar pares seguidos atrasados",
                default=self.settings.pair_watch_minutes,
            ),
        )
        self.ui.console.print("[green]Configuracao salva.[/green]")
        Prompt.ask("Pressione ENTER para continuar", default="")

    def start_robot(self, auto_trade: bool, display_mode: str = "dashboard") -> None:
        if not self.client or not self.client.connected:
            self.ui.console.print("[red]Faca login primeiro.[/red]")
            Prompt.ask("Pressione ENTER para continuar", default="")
            return
        self.engine = RobotEngine(
            client=self.client,
            settings=self.settings,
            risk=self.risk,
            history=self.history,
            ui=self.ui,
            logger=self.logger,
            account_mode=self.account_mode,
            auto_trade=auto_trade,
            display_mode=display_mode,
        )
        try:
            self.engine.start()
        except KeyboardInterrupt:
            self.stop_robot()

    def stop_robot(self) -> None:
        if self.engine:
            self.engine.stop()
            self.logger.info("[RISK] robo parado manualmente")

    def show_history(self) -> None:
        summary = self.history.summary()
        self.ui.console.print(self.ui.render_history(summary))
        rows = self.history.all()[-20:]
        for row in rows:
            self.ui.console.print(
                f"{row.get('timestamp')} | {row.get('asset')} | {row.get('direction')} | "
                f"{row.get('attempt')} | {row.get('result')} | {float(row.get('profit', 0)):.2f}"
            )
        Prompt.ask("Pressione ENTER para continuar", default="")
