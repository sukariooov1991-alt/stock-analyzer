"""
main.py — FastAPI + WebSocket + Longbridge
مع تحديث لحظي للشرائط والحيتان
"""
import os
import asyncio
import traceback
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

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()

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


# ============================================================
# دوال مشتركة للأوبشن
# ============================================================
def _get_strike(c):
    for attr in ("strike_price", "strike", "price"):
        v = getattr(c, attr, None)
        if v is not None:
            try:
                return float(v)
            except Exception:
                pass
    return 0.0


def _get_call_symbol(c):
    v = getattr(c, "call_symbol", None)
    if v: return v
    co = getattr(c, "call", None)
    if co is not None:
        return getattr(co, "symbol", None)
    return None


def _get_put_symbol(c):
    v = getattr(c, "put_symbol", None)
    if v: return v
    po = getattr(c, "put", None)
    if po is not None:
        return getattr(po, "symbol", None)
    return None


def _parse_expiries(raw_dates, today, target_dte):
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
        return None, None
    return min([(d, (d - today).days) for d in parsed], key=lambda x: abs(x[1] - target_dte))


def _build_options_payload(chain, qmap, price):
    """يبني OI/Volume/Whales من السلسلة"""
    all_call_syms, all_put_syms = [], []
    for c in chain:
        cs = _get_call_symbol(c)
        ps = _get_put_symbol(c)
        if cs: all_call_syms.append(cs)
        if ps: all_put_syms.append(ps)

    total_call_oi = total_put_oi = total_call_vol = total_put_vol = 0
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

    nearby = sorted(chain, key=lambda c: abs(_get_strike(c) - price))[:5]
    call_data, put_data = [], []
    for c in nearby:
        cs = _get_call_symbol(c)
        ps = _get_put_symbol(c)
        sk = int(_get_strike(c))
        if cs and cs in qmap:
            q = qmap[cs]
            call_data.append({
                "strike": sk,
                "oi": int(getattr(q, "open_interest", 0) or 0),
                "volume": int(getattr(q, "volume", 0) or 0),
            })
        if ps and ps in qmap:
            q = qmap[ps]
            put_data.append({
                "strike": sk,
                "oi": int(getattr(q, "open_interest", 0) or 0),
                "volume": int(getattr(q, "volume", 0) or 0),
            })
    call_data.sort(key=lambda x: x["strike"], reverse=True)
    put_data.sort(key=lambda x: x["strike"], reverse=True)

    # الحيتان
    whales = []
    wide = sorted(chain, key=lambda c: abs(_get_strike(c) - price))[:10]
    for c in wide:
        cs = _get_call_symbol(c)
        ps = _get_put_symbol(c)
        sk = int(_get_strike(c))
        for sym_opt, opt_type in ((cs, "CALL"), (ps, "PUT")):
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
            direction_w = "mid"
            if ask > bid > 0:
                spread = ask - bid
                pos = (last - bid) / spread if spread > 0 else 0.5
                if pos >= 0.7:   direction_w = "buy"
                elif pos <= 0.3: direction_w = "sell"
            whales.append({
                "strike": sk, "type": opt_type,
                "volume": vol, "oi": oi,
                "bid": round(bid, 2), "ask": round(ask, 2), "last": round(last, 2),
                "direction": direction_w,
            })
    whales.sort(key=lambda w: w["volume"], reverse=True)

    return {
        "call_oi": call_data,
        "put_oi": put_data,
        "total_call_oi": total_call_oi,
        "total_put_oi": total_put_oi,
        "total_call_vol": total_call_vol,
        "total_put_vol": total_put_vol,
        "whales": whales[:5],
    }


def _fetch_full_chain(sym, exp_date):
    """يجلب السلسلة ويجمع كل الـ quotes"""
    ctx = get_ctx()
    chain = ctx.option_chain_info_by_date(sym, exp_date)
    if not chain:
        return None, {}

    all_syms = []
    for c in chain:
        cs = _get_call_symbol(c)
        ps = _get_put_symbol(c)
        if cs: all_syms.append(cs)
        if ps: all_syms.append(ps)

    qmap = {}
    BATCH = 100
    for i in range(0, len(all_syms), BATCH):
        batch = all_syms[i:i + BATCH]
        try:
            qs = ctx.option_quote(batch)
            if qs:
                for q in qs:
                    qmap[q.symbol] = q
        except Exception:
            pass
    return chain, qmap


# ============================================================
# fetch_option_data — النسخة الكاملة (للتحليل الأولي)
# ============================================================
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
        exp_date, dte = _parse_expiries(raw_dates, today, target_dte)
        if not exp_date:
            return result

        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        chain, qmap = _fetch_full_chain(sym, exp_date)
        if not chain:
            return result

        # اختيار العقد الرئيسي
        if direction == "bullish":
            target = price * 1.015
            cands = [c for c in chain if _get_call_symbol(c) and _get_strike(c) > price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_get_strike(c) - target))
            option_symbol = _get_call_symbol(best)
            strike = _get_strike(best)
            opt_type = "C"
        else:
            target = price * 0.985
            cands = [c for c in chain if _get_put_symbol(c) and _get_strike(c) < price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_get_strike(c) - target))
            option_symbol = _get_put_symbol(best)
            strike = _get_strike(best)
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

        payload = _build_options_payload(chain, qmap, price)
        result.update(payload)

    except Exception:
        pass

    return result


# ============================================================
# ✅ endpoint خفيف للتحديث اللحظي
# ============================================================
@app.get("/api/options/{symbol}")
def options_live(symbol: str):
    """يُرجع فقط OI/Volume/Whales — لتحديث البطاقة كل 30 ثانية"""
    try:
        ctx = get_ctx()
        sym = norm(symbol)

        q = ctx.quote([sym])
        price = float(q[0].last_done) if q else 0.0

        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates:
            return JSONResponse(status_code=404, content={"error": "no dates"})

        today = datetime.now(timezone.utc).date()
        exp_date, dte = _parse_expiries(raw_dates, today, 37)
        if not exp_date:
            return JSONResponse(status_code=404, content={"error": "no future dates"})

        chain, qmap = _fetch_full_chain(sym, exp_date)
        if not chain:
            return JSONResponse(status_code=404, content={"error": "no chain"})

        payload = _build_options_payload(chain, qmap, price)
        payload["expiry"] = exp_date.strftime("%b %d").upper()
        payload["dte"] = dte
        payload["price"] = round(price, 2)
        return payload

    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


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
        "total_call_oi": opt.get("total_call_oi", 0),
        "total_put_oi": opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol": opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


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

    async def keepalive_task():
        while True:
            await asyncio.sleep(300)
            try:
                get_ctx().quote(["AAPL.US"])
            except Exception:
                pass

    task = asyncio.create_task(keepalive_task())
    yield
    task.cancel()
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


@app.get("/api/analyze/{symbol}")
def analyze(symbol: str):
    try:
        return analyze_symbol(symbol)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc().split("\n")[-10:],
        })


@app.get("/api/price/{symbol}")
def price_only(symbol: str):
    try:
        ctx = get_ctx()
        sym = norm(symbol)

        try:
            candles = ctx.candlesticks(
                sym, Period.Min_1, 1,
                AdjustType.NoAdjust,
                trade_sessions=TradeSessions.All,
            )
            if candles:
                last = candles[-1]
                return {
                    "symbol": symbol.upper(),
                    "price": float(last.close),
                    "open": float(last.open),
                    "high": float(last.high),
                    "low": float(last.low),
                    "volume": int(last.volume),
                    "timestamp": last.timestamp.isoformat(),
                }
        except Exception:
            pass

        q = ctx.quote([sym])
        if q:
            return {
                "symbol": symbol.upper(),
                "price": float(q[0].last_done),
                "open": float(q[0].open),
                "high": float(q[0].high),
                "low": float(q[0].low),
                "volume": int(q[0].volume),
                "timestamp": q[0].timestamp.isoformat(),
            }
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
    msg = {
        "symbol": symbol.replace(".US", ""),
        "price":  float(event.last_done),
    }
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
