"""
main.py — FastAPI + WebSocket + Longbridge
مطابق لوثائق Longbridge الرسمية (v4.x)
"""
import os
import asyncio
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date as date_cls

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
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
    # ✅ trade_session (مفرد) حسب الوثائق
    return ctx.candlesticks(
        norm(symbol), period, count,
        AdjustType.NoAdjust,
        trade_session=TradeSessions.Intraday,
    )


# ============================================================
# ✅ fetch_option_data — مطابق للوثائق (البنية المتداخلة)
# ============================================================
def fetch_option_data(symbol: str, direction: str, price: float, strategy: str = "daily") -> dict:
    """
    بنية option_chain_info_by_date:
    - strike_price: float
    - call: {symbol, last_done, iv, delta, gamma}
    - put:  {symbol, last_done, iv, delta, gamma}
    """
    ctx = get_ctx()
    sym = norm(symbol)

    result = {
        "strike": "—", "expiry": "—", "dte": "—", "premium": "—",
        "delta": None,
        "call_oi": [], "put_oi": [],
    }

    try:
        # ===== 1) تواريخ الانتهاء =====
        raw_dates = ctx.option_chain_info_by_date(sym)
        if not raw_dates:
            return result

        today = datetime.now(timezone.utc).date()
        target_dte = 37 if strategy == "daily" else 52

        parsed_dates = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today:
                    parsed_dates.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today:
                        parsed_dates.append(dd)
                except Exception:
                    continue

        if not parsed_dates:
            return result

        exp_date, dte = min(
            [(d, (d - today).days) for d in parsed_dates],
            key=lambda x: abs(x[1] - target_dte)
        )
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        # ===== 2) سلسلة العقود =====
        chain = ctx.option_chain_info_by_date(sym, exp_date.isoformat())
        if not chain:
            return result

        # ===== 3) استخراج الحقول (البنية المتداخلة) =====
        def get_strike(c):
            v = getattr(c, "strike_price", None)
            try:
                return float(v) if v is not None else 0.0
            except Exception:
                return 0.0

        def get_call_obj(c):
            return getattr(c, "call", None)

        def get_put_obj(c):
            return getattr(c, "put", None)

        def get_call_symbol(c):
            co = get_call_obj(c)
            return getattr(co, "symbol", None) if co else None

        def get_put_symbol(c):
            po = get_put_obj(c)
            return getattr(po, "symbol", None) if po else None

        # ===== 4) اختيار العقد =====
        if direction == "bullish":
            target = price * 1.015
            cands = [c for c in chain if get_call_symbol(c) and get_strike(c) > price]
            if not cands:
                return result
            best = min(cands, key=lambda c: abs(get_strike(c) - target))
            option_symbol = get_call_symbol(best)
            strike = get_strike(best)
            opt_type = "C"
        else:
            target = price * 0.985
            cands = [c for c in chain if get_put_symbol(c) and get_strike(c) < price]
            if not cands:
                return result
            best = min(cands, key=lambda c: abs(get_strike(c) - target))
            option_symbol = get_put_symbol(best)
            strike = get_strike(best)
            opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"

        # ===== 5) سعر العقد =====
        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                # ✅ حسب الوثائق: الحقل last_done
                for attr in ("last_done", "last", "price"):
                    v = getattr(oq, attr, None)
                    if v is not None:
                        result["premium"] = round(float(v), 2)
                        break
                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception:
            pass

        # ===== 6) OI و Volume للعقود القريبة =====
        nearby = sorted(chain, key=lambda c: abs(get_strike(c) - price))[:5]

        call_syms, put_syms = [], []
        for c in nearby:
            cs = get_call_symbol(c)
            ps = get_put_symbol(c)
            if cs: call_syms.append(cs)
            if ps: put_syms.append(ps)

        all_syms = call_syms + put_syms
        if all_syms:
            try:
                qs = ctx.option_quote(all_syms)
                qmap = {q.symbol: q for q in qs} if qs else {}

                call_data, put_data = [], []
                for c in nearby:
                    cs = get_call_symbol(c)
                    ps = get_put_symbol(c)
                    if cs and cs in qmap:
                        q = qmap[cs]
                        call_data.append({
                            "strike": int(get_strike(c)),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0),
                        })
                    if ps and ps in qmap:
                        q = qmap[ps]
                        put_data.append({
                            "strike": int(get_strike(c)),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0),
                        })

                call_data.sort(key=lambda x: x["strike"], reverse=True)
                put_data.sort(key=lambda x: x["strike"], reverse=True)
                result["call_oi"] = call_data
                result["put_oi"] = put_data
            except Exception:
                pass

    except Exception:
        pass

    return result


# ============================================================
# compute_levels
# ============================================================
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

    return {
        "entry":   round(entry, 2),
        "stop":    round(stop, 2),
        "target1": round(t1, 2),
        "target2": round(t2, 2),
    }


# ============================================================
# analyze_symbol
# ============================================================
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
    use_daily = trend_1d

    if use_daily:
        htf_df, exec_df = df_1d, df_15m
        strategy = "daily"
    else:
        htf_df, exec_df = df_4h, df_1h
        strategy = "weekly"

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
    rth_ok = True
    atr_val = float(exec_df["atr"].iloc[-1])
    momentum_ok = check_momentum(exec_df, atr_val) if sweep else False
    retest_ok = check_retest(exec_df, ifvg) if ifvg else False

    score = calculate_score(sweep, ifvg, trend_ok, rth_ok, momentum_ok, retest_ok)
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
            "label": label,
            "trend": "up" if up else "down",
            "ema20": round(float(r["ema20"]), 2),
            "ema50": round(float(r["ema50"]), 2),
            "rsi":   round(float(r["rsi"]), 1),
            "adx":   round(float(r["adx"]), 1),
            "rvol":  round(rvol(df), 2),
        }

    timeframes = [
        tf_snapshot(df_1d,  "1D"),
        tf_snapshot(df_4h,  "4H"),
        tf_snapshot(df_1h,  "1H"),
        tf_snapshot(df_15m, "15M"),
    ]

    last = exec_df.iloc[-1]
    supports = [
        round(float(exec_df["low"].iloc[-20:].min()), 2),
        round(float(prev_low), 2),
    ]
    resistances = [
        round(float(prev_high), 2),
        round(float(exec_df["high"].iloc[-20:].max()), 2),
    ]

    return {
        "symbol":  symbol.upper(),
        "price":   round(price, 2),
        "prevClose": round(prev_close, 2),
        "change":  round(price - prev_close, 2),
        "changePercent": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "card":    card,
        "levels":  levels,
        "timeframes": timeframes,
        "vwap":    round(float(last["vwap"]), 2),
        "supports":    supports,
        "resistances": resistances,
        "sweep":   sweep,
        "ifvg":    ifvg,
        "mss":     mss,
        "call_oi": opt.get("call_oi", []),
        "put_oi":  opt.get("put_oi", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# FastAPI + Keep-Alive
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    try:
        get_ctx()
    except Exception:
        pass

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
def status():
    try:
        ctx = get_ctx()
        q = ctx.quote(["AAPL.US"])
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
            "error": str(e),
            "type": type(e).__name__,
            "traceback": traceback.format_exc().split("\n")[-10:],
        })


# ============================================================
# WebSocket مع إعادة الاتصال
# ============================================================
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
        "high":   float(event.high),
        "low":    float(event.low),
        "volume": int(event.volume),
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
            ctx.set_on_quote(_on_quote)
            ctx.subscribe([sym_us], [SubType.Quote], is_first_push=True)
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
