
from flask import Flask, jsonify, render_template, request
from dotenv import load_dotenv
from neo_api_client import NeoAPI
import requests, os, time, threading, csv, io, re
from datetime import datetime, date, timedelta

load_dotenv()

app = Flask(__name__)
# Always re-read templates/index.html from disk on change (dev convenience),
# so UI edits are picked up without relying on a full process restart.
app.config["TEMPLATES_AUTO_RELOAD"] = True


@app.after_request
def add_no_cache_headers(resp):
    """Never let the browser cache the UI, so code fixes show up on reload."""
    resp.headers["Cache-Control"] = "no-store, no-cache, must-revalidate"
    resp.headers["Pragma"] = "no-cache"
    resp.headers["Expires"] = "0"
    return resp


NEO_CONSUMER_KEY = os.getenv("NEO_CONSUMER_KEY", "").strip()
ENVIRONMENT = os.getenv("NEO_ENVIRONMENT", "prod").strip()

# Kotak Neo's current official SDK supports quotes without completed 2FA.
# This app only reads market data; no orders are placed.
client = NeoAPI(consumer_key=NEO_CONSUMER_KEY, environment=ENVIRONMENT) if NEO_CONSUMER_KEY else None

CACHE = {
    "cm_rows": None,
    "fo_rows": None,
    "master_ts": 0,
    "quote_cache": {},
    "quote_ts": {}
}
lock = threading.Lock()
MASTER_TTL = 900
QUOTE_TTL = 3


def ok_data(resp):
    if not isinstance(resp, dict):
        return None
    data = resp.get("data")
    if data is not None:
        return data
    return resp


def strip_key(k):
    return re.sub(r"[\s;:_-]+", "", str(k or "")).lower()


def normalize_row(row):
    # Normalize Kotak legacy-style names such as pSymbolName / pTrdSymbol / pLotSize.
    out = {}
    for k, v in row.items():
        out[strip_key(k)] = "" if v is None else str(v).strip()
    return out


def getv(row, *keys, default=""):
    for key in keys:
        v = row.get(strip_key(key), "")
        if v != "":
            return v
    return default


def to_float(v):
    try:
        return float(str(v).replace(",", ""))
    except Exception:
        return None


def to_int(v):
    try:
        return int(float(str(v).replace(",", "")))
    except Exception:
        return None


def get_lot_size(row):
    """Return the contract lot size across Neo scrip-master generations.

    The current transformed NSE F&O master uses ``lLotSize``.  Older master
    files have used the ``pLotSize`` spelling, so keep both forms here rather
    than coupling the UI to a particular SDK/master-file version.
    """
    return to_int(getv(
        row,
        "lLotSize",      # current Kotak transformed scrip master
        "pLotSize",      # legacy Kotak scrip master
        "iLotSize",
        "lotSize",
        "marketLot",
        "pMarketLot",
        "lMarketLot",
        "lot",
    ))


def parse_expiry(v):
    if v is None or v == "":
        return None
    s = str(v).strip()

    # Common text formats.
    for fmt in (
        "%d-%b-%Y", "%d-%b-%y", "%d/%m/%Y", "%d-%m-%Y",
        "%Y-%m-%d", "%d %b %Y", "%d %B %Y"
    ):
        try:
            return datetime.strptime(s.upper(), fmt).date()
        except Exception:
            pass

    # Numeric epoch handling. Kotak legacy master files use seconds since
    # 1980-01-01, whereas other feeds use Unix seconds/milliseconds.  The two
    # seconds-based values overlap numerically, so choose the interpretation
    # that is closest to today (the master contains currently tradable
    # contracts, not historic expiries).
    try:
        n = float(s)
        if n > 10_000_000_000:  # Unix milliseconds
            return datetime.fromtimestamp(n / 1000).date()
        if n > 1_000_000_000:  # Unix or Kotak's 1980-based seconds
            unix_date = datetime.fromtimestamp(n).date()
            kotak_date = (datetime(1980, 1, 1) + timedelta(seconds=n)).date()
            return min((unix_date, kotak_date), key=lambda d: abs((d - date.today()).days))
        if n > 1_000_000:  # Kotak seconds since 1980-01-01
            return (datetime(1980, 1, 1) + timedelta(seconds=n)).date()
        return None
    except Exception:
        return None


def extract_file_urls(resp):
    """
    Current/legacy Neo scrip_master implementations can return either:
      {"filesPaths": ["https://...nse_fo.csv", ...], "baseFolder": "..."}
    or a direct list / string.
    """
    if isinstance(resp, dict):
        paths = resp.get("filesPaths") or resp.get("filePaths") or resp.get("files")
        if isinstance(paths, list):
            return [str(x) for x in paths]
        if isinstance(paths, str):
            return [paths]
    if isinstance(resp, list):
        return [str(x) for x in resp if str(x).startswith("http")]
    if isinstance(resp, str) and resp.startswith("http"):
        return [resp]
    return []


def download_csv(url):
    r = requests.get(url, timeout=30)
    r.raise_for_status()
    # Handle csv, txt, zip-contained CSV only if response is text.
    text = r.text
    return list(csv.DictReader(io.StringIO(text)))


def load_master(force=False):
    now = time.time()
    if CACHE["fo_rows"] is not None and CACHE["cm_rows"] is not None and not force and now - CACHE["master_ts"] < MASTER_TTL:
        return CACHE["cm_rows"], CACHE["fo_rows"]

    if not client:
        raise RuntimeError("NEO_CONSUMER_KEY is not configured in .env")

    cm_resp = client.scrip_master(exchange_segment="nse_cm")
    fo_resp = client.scrip_master(exchange_segment="nse_fo")

    cm_urls = extract_file_urls(cm_resp)
    fo_urls = extract_file_urls(fo_resp)

    if not cm_urls or not fo_urls:
        raise RuntimeError(
            "Kotak Neo scrip_master did not return downloadable CSV URLs. "
            f"CASH response keys={list(cm_resp.keys()) if isinstance(cm_resp, dict) else type(cm_resp)}, "
            f"FNO response keys={list(fo_resp.keys()) if isinstance(fo_resp, dict) else type(fo_resp)}"
        )

    cm_raw = []
    for u in cm_urls:
        if "nse_cm" in u.lower() or not cm_raw:
            cm_raw.extend(download_csv(u))
    fo_raw = []
    for u in fo_urls:
        if "nse_fo" in u.lower() or not fo_raw:
            fo_raw.extend(download_csv(u))

    cm_rows = [normalize_row(x) for x in cm_raw]
    fo_rows = [normalize_row(x) for x in fo_raw]

    with lock:
        CACHE["cm_rows"] = cm_rows
        CACHE["fo_rows"] = fo_rows
        CACHE["master_ts"] = now

    return cm_rows, fo_rows


def is_stock_future(row):
    inst = getv(row, "pInstType", "instrumentType", "instType").upper()
    name = getv(row, "pSymbolName", "symbolName", "underlyingSymbol").upper()
    trd = getv(row, "pTrdSymbol", "tradingSymbol", "tradingsymbol").upper()

    if inst in {"FUTIDX", "INDEXFUT", "OPTIDX", "OPTSTK", "INDEXOPTION"}:
        # OPTSTK is not relevant; futures handled below.
        return False
    if inst in {"FUTSTK", "FUTURE", "FUTURES"}:
        return True
    if trd.endswith("FUT") and name and not name.startswith(("NIFTY", "BANKNIFTY", "FINNIFTY", "MIDCPNIFTY")):
        return True
    return False


def stock_fno_universe():
    cm, fo = load_master()
    futures = {}
    for r in fo:
        if is_stock_future(r):
            sym = getv(r, "pSymbolName", "symbolName", "underlyingSymbol").upper()
            token = getv(r, "pSymbol", "symbol", "instrumentToken", "token")
            ts = getv(r, "pTrdSymbol", "tradingSymbol", "tradingsymbol")
            expiry = parse_expiry(getv(r, "pExpiryDate", "expiryDate", "expiry", "expiryDateVal"))
            lot = get_lot_size(r)
            if sym and token and ts and expiry:
                futures.setdefault(sym, []).append({
                    "symbol": sym,
                    "instrument_token": token,
                    "trading_symbol": ts,
                    "expiry": expiry.isoformat(),
                    "lot_size": lot,
                    "tick_size": to_float(getv(r, "pTickSize", "tickSize")),
                })

    cash_map = {}
    for r in cm:
        sym = getv(r, "pSymbolName", "symbolName", "tradingSymbol", "symbol").upper()
        ts = getv(r, "pTrdSymbol", "tradingSymbol", "tradingsymbol")
        token = getv(r, "pSymbol", "symbol", "instrumentToken", "token")
        if sym and token:
            # Prefer equity rows.
            inst = getv(r, "pInstType", "instrumentType", "instType").upper()
            if inst in ("", "EQ", "EQUITY", "EQUITIES"):
                cash_map[sym] = {
                    "symbol": sym, "instrument_token": token,
                    "trading_symbol": ts or f"{sym}-EQ"
                }

    out = []
    for sym in sorted(futures):
        out.append({
            "symbol": sym,
            "name": sym,
            "cash": cash_map.get(sym),
            "futures": sorted(futures[sym], key=lambda x: x["expiry"])
        })
    return out


def quote_many(tokens, quote_type="all"):
    if not client:
        raise RuntimeError("NEO_CONSUMER_KEY is not configured in .env")
    tokens = [x for x in tokens if x.get("instrument_token") and x.get("exchange_segment")]
    if not tokens:
        return []
    # The SDK accepts multiple instruments; chunk conservatively.
    all_rows = []
    for i in range(0, len(tokens), 25):
        resp = client.quotes(
            instrument_tokens=tokens[i:i+25],
            quote_type=quote_type
        )
        data = resp.get("data") if isinstance(resp, dict) else None
        if isinstance(data, list):
            all_rows.extend(data)
        elif isinstance(data, dict):
            # Some API versions return a dict keyed by token.
            all_rows.extend(data.values())
        elif isinstance(resp, list):
            all_rows.extend(resp)
    return all_rows


def parse_quote_rows(rows):
    out = {}
    for q in rows:
        if not isinstance(q, dict):
            continue
        token = str(
            q.get("exchange_token")
            or q.get("instrument_token")
            or q.get("pSymbol")
            or q.get("token")
            or ""
        )
        ltp = to_float(q.get("ltp") or q.get("last_price") or q.get("lp"))
        out[token] = {
            "ltp": ltp,
            "volume": to_float(q.get("last_volume") or q.get("volume") or q.get("v")),
            "change": to_float(q.get("change")),
            "per_change": to_float(q.get("per_change")),
            "oi": to_float(q.get("oi") or q.get("open_interest")),
            "display_symbol": q.get("display_symbol") or q.get("trading_symbol"),
        }
    return out


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/disclaimer")
def disclaimer():
    """Static legal page: the app is informational/educational only and is not
    SEBI-registered investment advice, a trading call, or a recommendation."""
    return render_template("disclaimer.html")


@app.get("/api/status")
def api_status():
    return jsonify({
        "ok": bool(NEO_CONSUMER_KEY),
        "broker": "Kotak Neo",
        "sdk": "kotakneoapi",
        "message": "Ready for market data" if NEO_CONSUMER_KEY else "Configure NEO_CONSUMER_KEY in .env",
        "two_factor_required_for_quotes": False
    })


@app.get("/api/diagnostics")
def diagnostics():
    try:
        cm, fo = load_master()
        inst_counts = {}
        for r in fo:
            k = getv(r, "pInstType", "instrumentType", "instType") or "<blank>"
            inst_counts[k] = inst_counts.get(k, 0) + 1
        stocks = stock_fno_universe()
        contracts = [f for stock in stocks for f in stock["futures"]]
        contracts_with_lot_size = sum(
            1 for contract in contracts if contract["lot_size"] is not None
        )
        return jsonify({
            "ok": True,
            "cash_rows": len(cm),
            "fo_rows": len(fo),
            "fno_stock_count": len(stocks),
            "fno_contract_count": len(contracts),
            "contracts_with_lot_size": contracts_with_lot_size,
            "instrument_type_counts": dict(sorted(inst_counts.items(), key=lambda kv: kv[0])[:50]),
            "sample_stock": stocks[:3],
            "message": "Scrip master parsed successfully."
        })
    except Exception as e:
        return jsonify({
            "ok": False,
            "error": str(e),
            "has_consumer_key": bool(NEO_CONSUMER_KEY)
        }), 502


@app.get("/api/stocks")
def api_stocks():
    try:
        stocks = stock_fno_universe()
        return jsonify({"ok": True, "stocks": stocks, "count": len(stocks)})
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.get("/api/stock/<symbol>")
def api_stock(symbol):
    try:
        symbol = symbol.upper().strip()
        stock = next((x for x in stock_fno_universe() if x["symbol"] == symbol), None)
        if not stock:
            raise RuntimeError(f"{symbol} was not found in Kotak Neo's NSE F&O scrip master.")

        cash_token = {
            "instrument_token": str(stock["cash"]["instrument_token"]),
            "exchange_segment": "nse_cm"
        } if stock.get("cash") else None

        fo_tokens = [
            {"instrument_token": str(f["instrument_token"]), "exchange_segment": "nse_fo"}
            for f in stock["futures"]
        ]

        quote_rows = quote_many(([cash_token] if cash_token else []) + fo_tokens, "all")
        quotes = parse_quote_rows(quote_rows)

        cash_q = quotes.get(str(cash_token["instrument_token"])) if cash_token else None
        spot = cash_q["ltp"] if cash_q else None

        futures = []
        for f in stock["futures"]:
            q = quotes.get(str(f["instrument_token"])) or {}
            ltp = q.get("ltp")
            futures.append({
                **f,
                "ltp": ltp,
                "basis": (ltp - spot) if ltp is not None and spot is not None else None,
                "oi": q.get("oi"),
                "volume": q.get("volume"),
                "per_change": q.get("per_change")
            })

        return jsonify({
            "ok": True,
            "symbol": symbol,
            "spot": spot,
            "cash_quote": cash_q,
            "futures": futures,
            "source": "Kotak Neo",
            "updated": datetime.now().astimezone().isoformat(timespec="seconds")
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


@app.get("/api/scan")
def api_scan():
    try:
        stocks = stock_fno_universe()
        requested_expiry = request.args.get("expiry", "").strip()
        # Default to front-month; if requested, use that exact expiry across
        # stocks that have the contract available.
        front = []
        for s in stocks:
            contract = next(
                (f for f in s["futures"] if f["expiry"] == requested_expiry),
                None
            ) if requested_expiry else (s["futures"][0] if s["futures"] else None)
            if contract:
                front.append((s, contract))

        tokens = []
        for s, f in front:
            if s.get("cash"):
                tokens.append({"instrument_token": str(s["cash"]["instrument_token"]), "exchange_segment": "nse_cm"})
            tokens.append({"instrument_token": str(f["instrument_token"]), "exchange_segment": "nse_fo"})

        quotes = parse_quote_rows(quote_many(tokens, "ltp"))

        results = []
        today = date.today()
        for s, f in front:
            if not s.get("cash"):
                continue
            cash_q = quotes.get(str(s["cash"]["instrument_token"]), {})
            fut_q = quotes.get(str(f["instrument_token"]), {})
            spot = cash_q.get("ltp")
            fut = fut_q.get("ltp")
            if spot is None or fut is None:
                continue
            basis = fut - spot
            expiry = date.fromisoformat(f["expiry"])
            d = max(1, (expiry - today).days)
            basis_pct = basis / spot * 100 if spot else None
            ann = basis_pct * 365 / d if basis_pct is not None else None
            results.append({
                "symbol": s["symbol"],
                "spot": spot,
                "future": fut,
                "expiry": f["expiry"],
                "lot_size": f["lot_size"],
                "basis": basis,
                "basis_pct": basis_pct,
                "days": d,
                "gross_ann": ann,
                "oi": fut_q.get("oi"),
                "volume": fut_q.get("volume")
            })
        results.sort(key=lambda x: x["gross_ann"] if x["gross_ann"] is not None else -999, reverse=True)
        return jsonify({
            "ok": True, "data": results, "count": len(results),
            "requested_expiry": requested_expiry or None,
            "updated": datetime.now().astimezone().isoformat(timespec="seconds")
        })
    except Exception as e:
        return jsonify({"ok": False, "error": str(e)}), 502


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=False)
