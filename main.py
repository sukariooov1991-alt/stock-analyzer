"""
main.py
FastAPI + WebSocket + Longbridge
متوافق مع Render (PORT ديناميكي + 0.0.0.0)
"""
import os
import json
import asyncio
from contextlib import asynccontextmanager
from datetime import datetime, timezone

from fastapi import FastAPI, WebSocket, WebSocketDisconnect, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse
from fastapi.staticfiles import StaticFiles

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

# ============================================================
# الإعدادات — من متغيرات البيئة (Render / .env محلياً)
# ============================================================
LB_KEY    = os.environ.get("LONGBRIDGE_APP_KEY", "")
LB_SECRET = os.environ.get("LONGBRIDGE_APP_SECRET", "")
LB_TOKEN  = os.environ.get("LONGBRIDGE_ACCESS_TOKEN", "")
PORT      = int(os.environ.get("PORT", 10000))

_lb_config = Config(
    app_key=LB_KEY,
    app_secret=LB_SECRET,
    access_token=LB_TOKEN,
)

_quote_ctx: QuoteContext | None = None


def get_ctx() -> QuoteContext:
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.US"


# ============================================================
# تحويل شموع Longbridge إلى DataFrame
# ============================================================
def candles_to_df(candles) -> "pd.DataFrame":
    import pandas as pd
    rows = [{
        "time":   c.timestamp,
        "open":   float(c.open),
        "high":   float(c.high),
        "low":    float(c.low),
        "close":  float(c.close),
        "volume": int(c.volume),
    } for c in candles]
    df = pd.DataFrame(rows)
    return df


# ============================================================
# جلب الشموع من Longbridge
# ============================================================
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
        norm(symbol),
        period,
        count,
        AdjustType.NoAdjust,
        trade_session=TradeSessions.Intraday,
    )


# ============================================================
# الحساب الكامل للبطاقة
# ============================================================
def analyze_symbol(symbol: str) -> dict:
    import pandas as pd

    # 1) جلب الشموع لكل الفريمات المطلوبة
    df_15m = candles_to_df(fetch_candles(symbol, "15m", 300))
    df_1h  = candles_to_df(fetch_candles(symbol, "1h",  300))
    df_4h  = candles_to_df(fetch_candles(symbol, "4h",  300))
    df_1d  = candles_to_df(fetch_candles(symbol, "1d",  300))

    # 2) حساب المؤشرات على كل فريم
    def enrich(df):
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi"]   = rsi(df["close"], 14)
        df["atr"]   = atr(df, 14)
        df["adx"]   = adx(df, 14)
        df["vwap"]  = vwap(df)
        return df

    df_15m, df_1h, df_4h, df_1d = map(enrich, [df_15m, df_1h, df_4h, df_1d])

    # 3) السعر الحالي
    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)

    # 4) تحديد الفريم القيادي وفريم التنفيذ
    #    إذا الاتجاه واضح على 1D → الاستراتيجية اليومية (1D + 15m)
    #    وإلا → الأسبوعية (1W/4H + 1H)
    trend_1d = df_1d["close"].iloc[-1] > df_1d["ema50"].iloc[-1]
    use_daily = trend_1d

    if use_daily:
        htf_df, exec_df, exec_tf = df_1d, df_15m, "15m"
    else:
        htf_df, exec_df, exec_tf = df_4h, df_1h, "1h"

    # 5) مستوى السيولة: قاع آخر شمعة على الفريم القيادي
    prev = htf_df.iloc[-2]
    prev_low  = float(prev["low"])
    prev_high = float(prev["high"])

    # 6) سحب السيولة على فريم التنفيذ
    sweep = detect_sweep(exec_df, prev_low, "low")
    direction = "bullish"
    if sweep is None:
        sweep = detect_sweep(exec_df, prev_high, "high")
        direction = "bearish" if sweep else "bullish"

    # 7) IFVG
    atr_series = exec_df["atr"]
    ifvg = find_ifvg(exec_df, atr_series, direction) if sweep else None

    # 8) MSS
    mss = False
    if sweep:
        if direction == "bullish":
            sh = swing_highs(exec_df, 2)
            last_sh = sh[-1] if sh else None
            mss = check_mss(exec_df, last_sh, "bullish")
        else:
            sl = swing_lows(exec_df, 2)
            last_sl = sl[-1] if sl else None
            mss = check_mss(exec_df, last_sl, "bearish")

    # 9) الفلاتر الإضافية
    trend_ok = (direction == "bullish" and trend_1d) or (direction == "bearish" and not trend_1d)
    rth_ok = True  # TODO: تحديد الجلسة الرسمية من timestamp
    atr_val = float(exec_df["atr"].iloc[-1])
    momentum_ok = check_momentum(exec_df, atr_val) if sweep else False
    retest_ok = check_retest(exec_df, ifvg) if ifvg else False

    # 10) النقاط
    score = calculate_score(sweep, ifvg, trend_ok, rth_ok, momentum_ok, retest_ok)
    card = classify_card(sweep, ifvg, direction)
    card["score"] = score

    # 11) المستويات
    levels = {}
    if ifvg and sweep:
        entry = ifvg["top"] if direction == "bullish" else ifvg["bottom"]
        levels = compute_levels(
            entry=entry,
            sweep_level=sweep["sweep"],
            ifvg_bottom=ifvg["bottom"],
            htf_target=prev_high if direction == "bullish" else prev_low,
            atr_val=atr_val,
        )

    # 12) لقطة الفريمات
    def tf_snapshot(df, label):
        r = df.iloc[-1]
        up = float(r["ema20"]) > float(r["ema50"])
        return {
            "label":    label,
            "trend":    "up" if up else "down",
            "ema20":    round(float(r["ema20"]), 2),
            "ema50":    round(float(r["ema50"]), 2),
            "rsi":      round(float(r["rsi"]), 1),
            "adx":      round(float(r["adx"]), 1),
            "rvol":     round(rvol(df), 2),
        }

    timeframes = [
        tf_snapshot(df_1d,  "1D"),
        tf_snapshot(df_4h,  "4H"),
        tf_snapshot(df_1h,  "1H"),
        tf_snapshot(df_15m, "15M"),
    ]

    # 13) VWAP + دعوم/مقاومات
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
        "change":  round(price - float(q.prev_close), 2),
        "changePercent": round((price - float(q.prev_close)) / float(q.prev_close) * 100, 2),
        "card":    card,
        "levels":  levels,
        "timeframes": timeframes,
        "vwap":    round(float(last["vwap"]), 2),
        "supports":    supports,
        "resistances": resistances,
        "sweep":   sweep,
        "ifvg":    ifvg,
        "mss":     mss,
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


# ============================================================
# FastAPI App
# ============================================================
@asynccontextmanager
async def lifespan(app: FastAPI):
    get_ctx()
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


@app.get("/")
def root():
    return {"status": "ok", "service": "stock-analyzer"}


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/analyze/{symbol}")
def analyze(symbol: str):
    try:
        return analyze_symbol(symbol)
    except Exception as e:
        raise HTTPException(status_code=500, detail=str(e))


# ============================================================
# WebSocket — Live
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
_subscribed: set[str] = set()


def _on_quote(symbol: str, event: PushQuote):
    """Callback من Longbridge — يبث للمتصفحات"""
    msg = {
        "symbol":   symbol.replace(".US", ""),
        "price":    float(event.last_done),
        "open":     float(event.open),
        "high":     float(event.high),
        "low":      float(event.low),
        "volume":   int(event.volume),
        "timestamp": event.timestamp.isoformat(),
    }
    try:
        loop = asyncio.get_event_loop()
        loop.create_task(manager.broadcast(symbol.replace(".US", ""), msg))
    except Exception:
        pass


@app.websocket("/ws/{symbol}")
async def ws_endpoint(ws: WebSocket, symbol: str):
    symbol = symbol.upper()
    await manager.connect(symbol, ws)

    # اشترك في Longbridge إذا لم يكن مشتركاً
    sym_us = norm(symbol)
    if sym_us not in _subscribed:
        ctx = get_ctx()
        ctx.set_on_quote(_on_quote)
        ctx.subscribe([sym_us], [SubType.Quote], is_first_push=True)
        _subscribed.add(sym_us)

    try:
        while True:
            await ws.receive_text()
    except WebSocketDisconnect:
        manager.disconnect(symbol, ws)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
