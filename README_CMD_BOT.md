# Robô BullEx - CMD Terminal

Sistema terminal em Python para login BullEx, leitura de ativos binários, payout, candles, sinais por sequência de candles e execução automática com martingale.

## Como rodar

```powershell
cd "C:\Users\josec\Downloads\bullexapi-main(3)\bullexapi-main"
python -m pip install -r requirements.txt
python main.py
```

## Fluxo

1. Login BullEx com email e senha oculta.
2. Seleção de conta DEMO ou REAL.
3. Configuração de entrada, stops, timeframe, payout mínimo, quantidade de ativos, candles seguidos e martingale.
4. Opção 4: monitoramento com entrada automática somente em DEMO.
5. Opção 5: monitoramento dos candles em tempo real sem abrir ordens.
6. Operação contra a tendência após a sequência configurada de candles iguais, sempre dobrando o valor nos gales. Se ganhar antes, não faz as próximas entradas.

## Seguranca

- A senha não é salva em arquivo nem impressa em logs.
- Conta REAL só opera após digitação exata de `CONFIRMO REAL`.
- A entrada automática do monitor fica travada em DEMO; em REAL ele abre apenas o monitor sem operar.
- Stop win, stop loss, saldo, payout mínimo, ativo aberto e modo de conta são validados antes de cada entrada.

## Arquivos principais

- `main.py`: entrada do programa.
- `app/menu.py`: menu interativo.
- `app/terminal_ui.py`: paineis Rich no terminal.
- `bullex/client.py`: adaptador da API BullEx existente.
- `robot/engine.py`: loop de monitoramento.
- `robot/strategy.py`: regra de candles seguidos e leitura da sequência atual.
- `robot/executor.py`: compra, resultado, martingale e histórico.
- `storage/history.py`: histórico JSON em `data/history.json`.
