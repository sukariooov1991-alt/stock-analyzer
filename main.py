"""
main.py — FastAPI + WebSocket + Longbridge
"""
import os
import asyncio
import traceback
from contextlib import asynccontextmanager
from datetime import datetime, timezone

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
    calculate_score, classify_card, compute_levels,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

_quote_ctx: QuoteContext | None = None
_main_loop: asyncio.AbstractEventLoop | None = None


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
        trade_sessions=TradeSessions.Intraday,
    )


# ============================================================
# جلب بيانات الأوبشن
# ============================================================
def fetch_option_data(symbol: str, direction: str, price: float, strategy: str = "daily") -> dict:
    """
    يختار عقد حسب: Strike OTM 1-2%، DTE 30-45 (daily) / 45-60 (weekly)
    ويُرجع premium + delta + OI للعقود القريبة.
    """
    ctx = get_ctx()
    sym = norm(symbol)

    result = {
        "strike": "—", "expiry": "—", "dte": "—", "premium": "—",
        "delta": None, "option_symbol": None,
        "call_oi": [], "put_oi": [],
    }

    try:
        # 1) تواريخ الانتهاء
        expiries = ctx.option_chain_info_by_date(sym)
        if not expiries:
            return result

        today = datetime.now(timezone.utc).date()
        target_dte = 37 if strategy == "daily" else 52

        parsed = []
        for e in expiries:
            ed = getattr(e, "expiry_date", e)
            if isinstance(ed, str):
                try:
                    ed = datetime.fromisoformat(ed.replace("Z", "")).date()
                except Exception:
                    continue
            if hasattr(ed, "date"):
                ed = ed.date()
            parsed.append(ed)

        valid = [(d, (d - today).days) for d in parsed if (d - today).days > 0]
        if not valid:
            return result

        exp_date, dte = min(valid, key=lambda x: abs(x[1] - target_dte))
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        # 2) سلسلة العقود
        chain = ctx.option_chain_info_by_date(sym, exp_date.isoformat())
        if not chain:
            return result

        # 3) اختيار Strike OTM 1-2%
        if direction == "bullish":
            target_strike = price * 1.015
            cands = [
                c for c in chain
                if getattr(c, "call_symbol", None)
                and price * 1.005 <= c.strike_price <= price * 1.025
            ]
            if not cands:
                cands = [c for c in chain if getattr(c, "call_symbol", None) and c.strike_price > price]
            if not cands:
                return result
            best = min(cands, key=lambda c: abs(c.strike_price - target_strike))
            option_symbol = best.call_symbol
            strike = best.strike_price
            opt_type = "C"
        else:
            target_strike = price * 0.985
            cands = [
                c for c in chain
                if getattr(c, "put_symbol", None)
                and price * 0.975 <= c.strike_price <= price * 0.995
            ]
            if not cands:
                cands = [c for c in chain if getattr(c, "put_symbol", None) and c.strike_price < price]
            if not cands:
                return result
            best = min(cands, key=lambda c: abs(c.strike_price - target_strike))
            option_symbol = best.put_symbol
            strike = best.strike_price
            opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"
        result["option_symbol"] = option_symbol

        # 4) سعر العقد (premium + delta)
        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                result["premium"] = round(float(oq.last_done), 2)
                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception:
            pass

        # 5) OI للعقود القريبة
        nearby = sorted(chain, key=lambda c: abs(c.strike_price - price))[:6]
        call_syms = [c.call_symbol for c in nearby if getattr(c, "call_symbol", None)]
        put_syms  = [c.put_symbol  for c in nearby if getattr(c, "put_symbol",  None)]

        try:
            all_syms = call_syms + put_syms
            if all_syms:
                qs = ctx.option_quote(all_syms)
                qmap = {q.symbol: q for q in qs}

                call_data = []
                for c in nearby:
                    s = getattr(c, "call_symbol", None)
                    if s in qmap:
                        q = qmap[s]
                        call_data.append({
                            "strike": int(c.strike_price),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0),
                        })

                put_data = []
                for c in nearby:
                    s = getattr(c, "put_symbol", None)
                    if s in qmap:
                        q = qmap[s]
                        put_data.append({
                            "strike": int(c.strike_price),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0),
                        })

                call_data.sort(key=lambda x: x["strike"], reverse=True)
                put_data.sort(key=lambda x: x["strike"], reverse=True)
                result["call_oi"] = call_data[:4]
                result["put_oi"]  = put_data[:4]
        except Exception as e:
            result["oi_error"] = str(e)

    except Exception as e:
        result["error"] = str(e)

    return result


# ============================================================
# التحليل الكامل
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

    # ✅ المستويات — تُحسب دائماً (بسعر الحالي إن لا يوجد sweep)
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
        entry=entry,
        sweep_level=stop_level,
        ifvg_bottom=stop_level,
        htf_target=htf_target,
        atr_val=atr_val,
    )

    # ✅ بيانات الأوبشن
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


@asynccontextmanager
async def lifespan(app: FastAPI):
    global _main_loop
    _main_loop = asyncio.get_running_loop()
    try:
        get_ctx()
    except Exception:
        pass
    yield
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
        return JSONResponse(status_code=503, content={
            "connected": False, "error": str(e), "type": type(e).__name__,
        })


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
_subscribed: set[str] = set()


def _on_quote(symbol: str, event: PushQuote):
    """Callback من Thread منفصل — نستخدم run_coroutine_threadsafe"""
    global _main_loop
    if _main_loop is None:
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
            _main_loop,
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
            await ws.receive_text()
    except WebSocketDisconnect:
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
