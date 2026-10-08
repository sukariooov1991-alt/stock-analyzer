"""
main.py — FastAPI + WebSocket + Longbridge + Telegram Alerts
"""
import os
import asyncio
import traceback
import requests
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date as date_cls

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, Query
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from longbridge.openapi import (
    Config, QuoteContext, Period, AdjustType,
    TradeSessions, SubType, PushQuote,
)

from analysis import (
    ema, rsi, atr, adx, vwap, rvol,
    swing_highs, swing_lows,
    detect_sweep, find_ifvg, check_mss,
    check_momentum, check_retest,
    calculate_score, classify_card,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_sent_alerts: set[str] = set()

WHALE_MIN_VOLUME = 3000
WHALE_MIN_OI     = 5000


def get_ctx() -> QuoteContext:
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.US"


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        print("[TELEGRAM] not configured", flush=True)
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        payload = {
            "chat_id": TELEGRAM_CHAT_ID,
            "text": message,
            "parse_mode": "HTML",
            "disable_web_page_preview": True,
        }
        r = requests.post(url, json=payload, timeout=10)
        if r.status_code == 200:
            print("[TELEGRAM] sent ok", flush=True)
            return True
        print(f"[TELEGRAM] error {r.status_code}: {r.text[:200]}", flush=True)
    except Exception as e:
        print(f"[TELEGRAM] error: {type(e).__name__}: {e}", flush=True)
    return False


def candles_to_df(candles):
    import pandas as pd
    return pd.DataFrame([{
        "time":   c.timestamp,
        "open":   float(c.open),
        "high":   float(c.high),
        "low":    float(c.low),
        "close":  float(c.close),
        "volume": int(c.volume),
    } for c in candles])


PERIOD_MAP = {
    "15m": Period.Min_15,
    "1h":  Period.Min_60,
    "4h":  Period.Min_240,
    "1d":  Period.Day,
    "1w":  Period.Week,
}


def fetch_candles(symbol: str, timeframe: str, count: int = 300):
    ctx = get_ctx()
    period = PERIOD_MAP.get(timeframe.lower())
    if period is None:
        raise ValueError(f"فريم غير مدعوم: {timeframe}")
    return ctx.candlesticks(
        norm(symbol), period, count,
        AdjustType.NoAdjust,
        trade_sessions=TradeSessions.All,
    )


def _strike_of(c):
    for attr in ("strike_price", "strike", "price"):
        v = getattr(c, attr, None)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def _call_of(c):
    v = getattr(c, "call_symbol", None)
    if v: return v
    co = getattr(c, "call", None)
    if co is not None:
        return getattr(co, "symbol", None)
    return None


def _put_of(c):
    v = getattr(c, "put_symbol", None)
    if v: return v
    po = getattr(c, "put", None)
    if po is not None:
        return getattr(po, "symbol", None)
    return None


def fetch_option_data(symbol: str, direction: str, price: float, strategy: str = "daily") -> dict:
    ctx = get_ctx()
    sym = norm(symbol)

    result = {
        "strike": "—", "expiry": "—", "dte": "—", "premium": "—",
        "delta": None,
        "call_oi": [], "put_oi": [],
        "total_call_oi": 0, "total_put_oi": 0,
        "total_call_vol": 0, "total_put_vol": 0,
        "whales": [],
    }

    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates:
            return result

        today = datetime.now(timezone.utc).date()
        target_dte = 37 if strategy == "daily" else 52

        parsed = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today:
                    parsed.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today:
                        parsed.append(dd)
                except Exception:
                    continue

        if not parsed:
            return result

        exp_date, dte = min(
            [(d, (d - today).days) for d in parsed],
            key=lambda x: abs(x[1] - target_dte)
        )
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        chain = ctx.option_chain_info_by_date(sym, exp_date)
        if not chain:
            return result

        if direction == "bullish":
            target = price * 1.015
            cands = [c for c in chain if _call_of(c) and _strike_of(c) > price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _call_of(best)
            strike = _strike_of(best)
            opt_type = "C"
        else:
            target = price * 0.985
            cands = [c for c in chain if _put_of(c) and _strike_of(c) < price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _put_of(best)
            strike = _strike_of(best)
            opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"

        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                for attr in ("last_done", "last", "price"):
                    v = getattr(oq, attr, None)
                    if v is not None:
                        result["premium"] = round(float(v), 2)
                        break
                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception:
            pass

        base_match = re.match(r'^([A-Z]+)', sym.replace(".US", ""))
        base_sym = base_match.group(1) if base_match else sym.replace(".US", "")

        all_call_syms, all_put_syms = [], []
        for c in chain:
            cs = _call_of(c)
            ps = _put_of(c)
            sk = _strike_of(c)

            if cs:
                all_call_syms.append(cs)
            elif sk > 0:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                all_call_syms.append(f"{base_sym}{yy}{mm}{dd}C{str(int(sk * 1000))}.US")

            if ps:
                all_put_syms.append(ps)
            elif sk > 0:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                all_put_syms.append(f"{base_sym}{yy}{mm}{dd}P{str(int(sk * 1000))}.US")

        qmap = {}
        all_syms = all_call_syms + all_put_syms
        for i in range(0, len(all_syms), 30):
            batch = all_syms[i:i + 30]
            try:
                qs = ctx.option_quote(batch)
                if qs:
                    for q in qs:
                        qmap[q.symbol] = q
            except Exception:
                pass

        total_call_oi = total_put_oi = 0
        total_call_vol = total_put_vol = 0
        for s in all_call_syms:
            if s in qmap:
                q = qmap[s]
                total_call_oi  += int(getattr(q, "open_interest", 0) or 0)
                total_call_vol += int(getattr(q, "volume", 0) or 0)
        for s in all_put_syms:
            if s in qmap:
                q = qmap[s]
                total_put_oi  += int(getattr(q, "open_interest", 0) or 0)
                total_put_vol += int(getattr(q, "volume", 0) or 0)

        result["total_call_oi"]  = total_call_oi
        result["total_put_oi"]   = total_put_oi
        result["total_call_vol"] = total_call_vol
        result["total_put_vol"]  = total_put_vol

        nearby = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:5]
        call_data, put_data = [], []
        for c in nearby:
            sk = int(_strike_of(c))

            cs = _call_of(c)
            if not cs:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                cs = f"{base_sym}{yy}{mm}{dd}C{str(int(_strike_of(c) * 1000))}.US"
            if cs in qmap:
                q = qmap[cs]
                call_data.append({
                    "strike": sk,
                    "oi": int(getattr(q, "open_interest", 0) or 0),
                    "volume": int(getattr(q, "volume", 0) or 0),
                })

            ps = _put_of(c)
            if not ps:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                ps = f"{base_sym}{yy}{mm}{dd}P{str(int(_strike_of(c) * 1000))}.US"
            if ps in qmap:
                q = qmap[ps]
                put_data.append({
                    "strike": sk,
                    "oi": int(getattr(q, "open_interest", 0) or 0),
                    "volume": int(getattr(q, "volume", 0) or 0),
                })

        call_data.sort(key=lambda x: x["strike"], reverse=True)
        put_data.sort(key=lambda x: x["strike"], reverse=True)
        result["call_oi"] = call_data
        result["put_oi"]  = put_data

        whales = []
        wide = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:10]
        for c in wide:
            sk = int(_strike_of(c))
            cs = _call_of(c)
            if not cs:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                cs = f"{base_sym}{yy}{mm}{dd}C{str(int(_strike_of(c) * 1000))}.US"
            ps = _put_of(c)
            if not ps:
                yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
                ps = f"{base_sym}{yy}{mm}{dd}P{str(int(_strike_of(c) * 1000))}.US"

            for sym_opt, opt_type_w in ((cs, "CALL"), (ps, "PUT")):
                if not sym_opt or sym_opt not in qmap:
                    continue
                q = qmap[sym_opt]
                vol = int(getattr(q, "volume", 0) or 0)
                oi  = int(getattr(q, "open_interest", 0) or 0)
                if vol < WHALE_MIN_VOLUME and oi < WHALE_MIN_OI:
                    continue
                bid = float(getattr(q, "bid", 0) or 0)
                ask = float(getattr(q, "ask", 0) or 0)
                last = float(getattr(q, "last_done", 0) or getattr(q, "last", 0) or 0)
                dir_w = "mid"
                if ask > bid > 0:
                    spread = ask - bid
                    pos = (last - bid) / spread if spread > 0 else 0.5
                    if pos >= 0.7:   dir_w = "buy"
                    elif pos <= 0.3: dir_w = "sell"
                whales.append({
                    "strike": sk, "type": opt_type_w,
                    "volume": vol, "oi": oi,
                    "bid": round(bid, 2), "ask": round(ask, 2), "last": round(last, 2),
                    "direction": dir_w,
                })
        whales.sort(key=lambda w: w["volume"], reverse=True)
        result["whales"] = whales[:5]

    except Exception:
        pass

    return result


def compute_levels(entry, sweep_level, ifvg_bottom, htf_target, atr_val, direction="bullish"):
    stop = min(sweep_level, ifvg_bottom) - 0.1 * atr_val if direction == "bullish" \
           else max(sweep_level, ifvg_bottom) + 0.1 * atr_val
    risk = abs(entry - stop)
    if direction == "bullish":
        t1 = entry + 2 * risk
        t2 = max(htf_target, entry + 3 * risk)
    else:
        t1 = entry - 2 * risk
        t2 = min(htf_target, entry - 3 * risk)
    return {"entry": round(entry, 2), "stop": round(stop, 2),
            "target1": round(t1, 2), "target2": round(t2, 2)}


def analyze_symbol(symbol: str) -> dict:
    df_15m = candles_to_df(fetch_candles(symbol, "15m", 300))
    df_1h  = candles_to_df(fetch_candles(symbol, "1h",  300))
    df_4h  = candles_to_df(fetch_candles(symbol, "4h",  300))
    df_1d  = candles_to_df(fetch_candles(symbol, "1d",  300))

    def enrich(df):
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi"]   = rsi(df["close"], 14)
        df["atr"]   = atr(df, 14)
        df["adx"]   = adx(df, 14)
        df["vwap"]  = vwap(df)
        return df

    df_15m, df_1h, df_4h, df_1d = map(enrich, [df_15m, df_1h, df_4h, df_1d])

    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    trend_1d = df_1d["close"].iloc[-1] > df_1d["ema50"].iloc[-1]

    if trend_1d:
        htf_df, exec_df, strategy = df_1d, df_15m, "daily"
    else:
        htf_df, exec_df, strategy = df_4h, df_1h, "weekly"

    prev = htf_df.iloc[-2]
    prev_low  = float(prev["low"])
    prev_high = float(prev["high"])

    sweep = detect_sweep(exec_df, prev_low, "low")
    direction = "bullish"
    if sweep is None:
        sweep = detect_sweep(exec_df, prev_high, "high")
        direction = "bearish" if sweep else "bullish"

    atr_series = exec_df["atr"]
    ifvg = find_ifvg(exec_df, atr_series, direction) if sweep else None

    mss = False
    if sweep:
        if direction == "bullish":
            sh = swing_highs(exec_df, 2)
            mss = check_mss(exec_df, sh[-1] if sh else None, "bullish")
        else:
            sl = swing_lows(exec_df, 2)
            mss = check_mss(exec_df, sl[-1] if sl else None, "bearish")

    trend_ok = (direction == "bullish" and trend_1d) or (direction == "bearish" and not trend_1d)
    atr_val = float(exec_df["atr"].iloc[-1])
    momentum_ok = check_momentum(exec_df, atr_val) if sweep else False
    retest_ok = check_retest(exec_df, ifvg) if ifvg else False

    score = calculate_score(sweep, ifvg, trend_ok, True, momentum_ok, retest_ok)
    card = classify_card(sweep, ifvg, direction)
    card["score"] = score

    if ifvg and sweep:
        entry = ifvg["top"] if direction == "bullish" else ifvg["bottom"]
        stop_level = min(sweep["sweep"], ifvg["bottom"]) if direction == "bullish" \
                     else max(sweep["sweep"], ifvg["top"])
        htf_target = prev_high if direction == "bullish" else prev_low
    else:
        entry = price
        stop_level = price - 1.5 * atr_val if direction == "bullish" else price + 1.5 * atr_val
        htf_target = prev_high if direction == "bullish" else prev_low

    levels = compute_levels(
        entry=entry, sweep_level=stop_level, ifvg_bottom=stop_level,
        htf_target=htf_target, atr_val=atr_val, direction=direction,
    )

    opt = fetch_option_data(symbol, direction, price, strategy)
    levels["strike"]  = opt.get("strike", "—")
    levels["expiry"]  = opt.get("expiry", "—")
    levels["dte"]     = opt.get("dte", "—")
    levels["premium"] = opt.get("premium", "—")

    def tf_snapshot(df, label):
        r = df.iloc[-1]
        up = float(r["ema20"]) > float(r["ema50"])
        return {
            "label": label, "trend": "up" if up else "down",
            "ema20": round(float(r["ema20"]), 2),
            "ema50": round(float(r["ema50"]), 2),
            "rsi":   round(float(r["rsi"]), 1),
            "adx":   round(float(r["adx"]), 1),
            "rvol":  round(rvol(df), 2),
        }

    timeframes = [
        tf_snapshot(df_1d, "1D"), tf_snapshot(df_4h, "4H"),
        tf_snapshot(df_1h, "1H"), tf_snapshot(df_15m, "15M"),
    ]

    last = exec_df.iloc[-1]
    supports = [round(float(exec_df["low"].iloc[-20:].min()), 2), round(float(prev_low), 2)]
    resistances = [round(float(prev_high), 2), round(float(exec_df["high"].iloc[-20:].max()), 2)]

    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "change": round(price - prev_close, 2),
        "changePercent": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "card": card, "levels": levels, "timeframes": timeframes,
        "vwap": round(float(last["vwap"]), 2),
        "supports": supports, "resistances": resistances,
        "sweep": sweep, "ifvg": ifvg, "mss": mss,
        "call_oi": opt.get("call_oi", []),
        "put_oi": opt.get("put_oi", []),
        "total_call_oi":  opt.get("total_call_oi", 0),
        "total_put_oi":   opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol":  opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def build_alert_message(data: dict) -> str:
    sym = data["symbol"]
    price = data["price"]
    card = data.get("card", {})
    lv = data.get("levels", {})
    color = card.get("color", "gray")

    if color == "green":
        header = f"🟢 <b>إشارة CALL</b> — {sym}"
    elif color == "red":
        header = f"🔴 <b>إشارة PUT</b> — {sym}"
    else:
        return ""

    return f"""{header}

💪 قوة الإشارة: <b>{card.get('score', 0)}%</b>
💰 السعر الحالي: <b>${price}</b>

📋 <b>العقد المقترح:</b>
  • STRIKE: <b>{lv.get('strike','—')}</b>
  • EXPIRY: <b>{lv.get('expiry','—')}</b> (DTE: {lv.get('dte','—')})
  • PREMIUM: <b>${lv.get('premium','—')}</b>

📊 <b>المستويات:</b>
  • ENTRY: ${lv.get('entry','—')}
  • STOP: ${lv.get('stop','—')}
  • TARGET 1: ${lv.get('target1','—')}
  • TARGET 2: ${lv.get('target2','—')}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
"""


WATCHLIST_FILE = "watchlist.json"


def get_watchlist() -> list:
    import json
    try:
        if os.path.exists(WATCHLIST_FILE):
            with open(WATCHLIST_FILE, "r") as f:
                data = json.load(f)
                return [s.upper() for s in data.get("symbols", [])]
    except Exception:
        pass
    return ["AAPL", "NVDA", "TSLA", "META", "AMD", "MSFT", "GOOGL", "AMZN"]


def save_watchlist(symbols: list):
    import json
    try:
        with open(WATCHLIST_FILE, "w") as f:
            json.dump({"symbols": [s.upper() for s in symbols]}, f)
    except Exception:
        pass


async def watchlist_checker():
    global _sent_alerts
    await asyncio.sleep(60)

    while True:
        try:
            watchlist = get_watchlist()
            print(f"[WATCH] checking {len(watchlist)} symbols...", flush=True)

            for sym in watchlist:
                try:
                    data = await asyncio.to_thread(analyze_symbol, sym)
                    card = data.get("card", {})
                    color = card.get("color", "gray")

                    if color not in ("green", "red"):
                        continue

                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    alert_key = f"{sym}:{color}:{today}"

                    if alert_key in _sent_alerts:
                        continue

                    msg = build_alert_message(data)
                    if msg:
                        await asyncio.to_thread(send_telegram_alert, msg)
                        _sent_alerts.add(alert_key)
                        print(f"[WATCH] alert sent for {sym} ({color})", flush=True)

                except Exception as e:
                    print(f"[WATCH] {sym} error: {e}", flush=True)

        except Exception as e:
            print(f"[WATCH] fatal: {e}", flush=True)

        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_running_loop()

    try:
        ctx = get_ctx()
        ctx.set_on_quote(_on_quote)
        print("[STARTUP] callback registered", flush=True)
    except Exception as e:
        print(f"[STARTUP] error: {e}", flush=True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            send_telegram_alert("🚀 <b>محلل الأسهم</b> — تم تشغيل النظام بنجاح")
        except Exception:
            pass

    async def keepalive_task():
        while True:
            await asyncio.sleep(300)
            try:
                get_ctx().quote(["AAPL.US"])
            except Exception:
                pass

    keepalive = asyncio.create_task(keepalive_task())
    watcher = asyncio.create_task(watchlist_checker())

    yield

    keepalive.cancel()
    watcher.cancel()
    global _quote_ctx
    _quote_ctx = None


app = FastAPI(title="Stock Analyzer", lifespan=lifespan)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["*"],
    allow_headers=["*"],
)


@app.get("/api/status")
def status(symbol: str = Query("AAPL")):
    try:
        ctx = get_ctx()
        q = ctx.quote([norm(symbol)])
        if q:
            return {"connected": True, "price": str(q[0].last_done), "symbol": q[0].symbol}
        return {"connected": False, "error": "لا توجد بيانات"}
    except Exception as e:
        return JSONResponse(status_code=503, content={"connected": False, "error": str(e)})


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/test-telegram")
def test_telegram():
    ok = send_telegram_alert("✅ <b>اختبار ناجح</b>\nإذا وصلتك هذه الرسالة، فالتنبيهات تعمل.")
    return {"sent": ok, "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


@app.get("/api/watchlist")
def get_wl():
    return {"symbols": get_watchlist()}


@app.post("/api/watchlist")
async def update_wl(payload: dict):
    symbols = payload.get("symbols", [])
    if not isinstance(symbols, list):
        return JSONResponse(status_code=400, content={"error": "symbols must be list"})
    save_watchlist(symbols)
    return {"ok": True, "symbols": get_watchlist()}


@app.get("/api/analyze/{symbol}")
def analyze(symbol: str):
    try:
        return analyze_symbol(symbol)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc().split("\n")[-10:],
        })


@app.get("/api/options/{symbol}")
def options_live(symbol: str):
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        q = ctx.quote([sym])
        price = float(q[0].last_done) if q else 0.0
        opt = fetch_option_data(symbol, "bullish", price, "daily")
        return {
            "expiry":         opt.get("expiry", "—"),
            "dte":            opt.get("dte", "—"),
            "price":          round(price, 2),
            "call_oi":        opt.get("call_oi", []),
            "put_oi":         opt.get("put_oi", []),
            "total_call_oi":  opt.get("total_call_oi", 0),
            "total_put_oi":   opt.get("total_put_oi", 0),
            "total_call_vol": opt.get("total_call_vol", 0),
            "total_put_vol":  opt.get("total_put_vol", 0),
            "whales":         opt.get("whales", []),
        }
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


@app.get("/api/price/{symbol}")
def price_only(symbol: str):
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        try:
            candles = ctx.candlesticks(sym, Period.Min_1, 1, AdjustType.NoAdjust,
                                       trade_sessions=TradeSessions.All)
            if candles:
                last = candles[-1]
                return {"symbol": symbol.upper(), "price": float(last.close),
                        "open": float(last.open), "high": float(last.high),
                        "low": float(last.low), "volume": int(last.volume),
                        "timestamp": last.timestamp.isoformat()}
        except Exception:
            pass

        q = ctx.quote([sym])
        if q:
            return {"symbol": symbol.upper(), "price": float(q[0].last_done),
                    "open": float(q[0].open), "high": float(q[0].high),
                    "low": float(q[0].low), "volume": int(q[0].volume),
                    "timestamp": q[0].timestamp.isoformat()}
        return JSONResponse(status_code=404, content={"error": "no data"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class ConnectionManager:
    def __init__(self):
        self.active: dict[str, list[WebSocket]] = {}

    async def connect(self, symbol: str, ws: WebSocket):
        await ws.accept()
        self.active.setdefault(symbol, []).append(ws)

    def disconnect(self, symbol: str, ws: WebSocket):
        if symbol in self.active and ws in self.active[symbol]:
            self.active[symbol].remove(ws)

    async def broadcast(self, symbol: str, message: dict):
        for ws in list(self.active.get(symbol, [])):
            try:
                await ws.send_json(message)
            except Exception:
                self.disconnect(symbol, ws)


manager = ConnectionManager()


def _on_quote(symbol: str, event: PushQuote):
    global _event_loop
    if _event_loop is None:
        return
    msg = {"symbol": symbol.replace(".US", ""), "price": float(event.last_done)}
    try:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(symbol.replace(".US", ""), msg),
            _event_loop,
        )
    except Exception:
        pass


@app.websocket("/ws/{symbol}")
async def ws_endpoint(ws: WebSocket, symbol: str):
    symbol = symbol.upper()
    await manager.connect(symbol, ws)
    sym_us = norm(symbol)
    if sym_us not in _subscribed:
        try:
            ctx = get_ctx()
            ctx.subscribe([sym_us], [SubType.Quote])
            _subscribed.add(sym_us)
        except Exception:
            pass
    try:
        while True:
            await asyncio.wait_for(ws.receive_text(), timeout=90)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        manager.disconnect(symbol, ws)


@app.get("/")
def index():
    return FileResponse("index.html")


@app.get("/style.css")
def css():
    return FileResponse("style.css")


@app.get("/app.js")
def js():
    return FileResponse("app.js")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
