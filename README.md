# Claude Bot – Python-Version

Ein Paper-Trading-Bot mit der Strategie Trend+RSI. Er läuft ohne Claude und verbraucht deshalb keine Tokens.
Gehandelt wird nur Spielgeld: ein simuliertes Konto im Ordner `state/` mit 100.000 $ Start.

## Strategie (wie in TradingView „SMC Lab v3“ → TrendRSI)
- Long: Kurs über EMA 200 und RSI(14) kreuzt über 35
- Short: Kurs unter EMA 200 und RSI(14) kreuzt unter 65
- Stop 1,5 × ATR(14), Ziel 2 × Stop-Abstand, 2 % Risiko pro Trade, max. 3 Positionen
- BTC, ETH, SOL im 4-Stunden-Chart, Kursdaten von Binance (öffentlich), Ersatz: Kraken

## Automatisch
`.github/workflows/bot.yml` startet den Bot 7 Minuten nach jedem 4h-Kerzenschluss auf GitHub-Servern
und speichert den Kontostand (`state/`) als Commit. Kosten: 0 €.

## Lokal
```
python3 bot.py backtest   # Test seit 2024: Lern- vs. Prüf-Zeitraum
python3 bot.py run        # ein Live-Durchlauf
python3 bot.py status     # Konto und letzte Trades
```
