# Cash-and-Carry Arbitrage Analyzer — Kotak Neo

## Purpose

This version switches the live market-data adapter from Groww to Kotak Neo's official actively maintained Python SDK.

It is **READ-ONLY**:
- no orders are placed
- no positions are modified
- no trading action is automated

## Live data

The app uses:
- Kotak Neo scrip master for NSE cash + NSE F&O instruments
- Kotak Neo `quotes()` for live cash/futures LTP, OI and volume

The official SDK documentation says:
- `quotes()` accepts one or multiple instruments
- quote types include `ltp`, `oi`, `ohlc`, `market_depth` and `all`
- `quotes()` does not require completed TOTP/2FA; consumer_key is sufficient for this market-data call
- the current `kotak-neo-python` SDK is the actively maintained package and supersedes the legacy `kotak-neo-api-v2` package.

## Setup

1. Install Python 3.10+.
2. Clone this repository:
   ```bash
   git clone https://github.com/<your-username>/Manu_Cash_Future_Arbitrage_V2.git
   cd Manu_Cash_Future_Arbitrage_V2
   ```
3. Create a virtual environment (recommended):
   ```bash
   python -m venv .venv
   ```
   - **Windows (PowerShell):** `.venv\Scripts\Activate.ps1`
   - **Windows (CMD):** `.venv\Scripts\activate.bat`
   - **Linux / macOS:** `source .venv/bin/activate`
4. Copy `.env.example` → `.env`
5. Put your Kotak Neo Consumer Key in `NEO_CONSUMER_KEY`.
6. Run:
   ```bash
   pip install -r requirements.txt
   python server.py
   ```
7. Open: http://127.0.0.1:5000

### Windows

Double-click `run_windows.bat`.

## Diagnostics

Open: http://127.0.0.1:5000/api/diagnostics

This endpoint shows:
- cash master rows
- F&O master rows
- detected stock-future count
- instrument type counts
- sample parsed stock/future records

## How stock mapping works

The app reads the Kotak Neo NSE F&O scrip master and identifies stock futures using the instrument type / trading-symbol pattern. It gets:
- underlying symbol
- futures token
- trading symbol
- expiry
- lot size

Then it finds the matching NSE cash instrument and uses `nse_cm` for the stock quote and `nse_fo` for the future quote.

## Scrip-master compatibility

Kotak's current SDK can return file URLs from `scrip_master()`; the app handles the documented URL-list style response and downloads/normalizes the CSVs. Because Kotak has had both legacy and current SDK generations, the parser is defensive around field names such as:

`pSymbol`, `pSymbolName`, `pTrdSymbol`, `pExpiryDate`, `pLotSize`, `pInstType`.

The expiry parser also handles text dates and the known legacy-style numeric expiry representation used by some scrip-master versions.

## Security

- Do not put secrets in HTML/JavaScript.
- Keep `NEO_CONSUMER_KEY` in `.env` and **do not upload `.env` to GitHub** (it is excluded via `.gitignore`).

## Detailed Trade Analysis

Detailed Trade Analysis calculates:
- capital deployed
- gross basis
- estimated taxes and exchange charges
- dividend adjustment
- fixed-deposit opportunity cost
- savings-account opportunity cost
- estimated net profit
- annualized return
- break-even futures price required for your target return
- GOOD / MARGINAL / BAD

## Opportunity Scanner

Choose a specific futures expiry, or use the front-month contract for each stock. Results are ranked by net annualized return after estimated taxes and exchange charges, the entered other-cost input, dividend assumptions and fixed-deposit opportunity cost. Review the Detailed Analysis tab before making a decision, especially where dividends or broker-specific costs differ by stock.

## Important

The calculator is decision support, not a guarantee of arbitrage profit. Verify live quotes, contract, lot size, expiry, charges, dividends, liquidity, margin and settlement rules before trading.

## Kotak API reference

The implementation follows the current official Kotak Neo Python SDK:
https://github.com/Kotak-Neo/kotak-neo-python