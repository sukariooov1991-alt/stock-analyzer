"""
main.py — FastAPI + Longbridge + Telegram
الاستراتيجية: RSI + MACD Swing (CALL + PUT)
"""
import os
import time
import asyncio
import traceback
import requests
import re
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
    macd, swing_signal, compute_swing_levels,
)

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_sent_alerts: set[str] = set()
_watchlist: set[str] = set()
_analyze_cache: dict[str, tuple[float, dict]] = {}
_last_color: dict[str, str] = {}
_ANALYZE_TTL = 25

WHALE_MIN_VOLUME = 3000
WHALE_MIN_OI     = 5000


def get_ctx():
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol: str) -> str:
    s = symbol.strip().upper()
    return s if "." in s else f"{s}.US"


def send_telegram_alert(message: str) -> bool:
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={
            "chat_id": TELEGRAM_CHAT_ID, "text": message,
            "parse_mode": "HTML", "disable_web_page_preview": True,
        }, timeout=10)
        print(f"[TELEGRAM] {r.status_code}", flush=True)
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] {e}", flush=True)
        return False


def candles_to_df(candles):
    import pandas as pd
    return pd.DataFrame([{
        "time": c.timestamp, "open": float(c.open), "high": float(c.high),
        "low": float(c.low), "close": float(c.close), "volume": int(c.volume),
    } for c in candles])


PERIOD_MAP = {
    "15m": Period.Min_15, "1h": Period.Min_60,
    "4h": Period.Min_240, "1d": Period.Day, "1w": Period.Week,
}


def fetch_candles(symbol, timeframe, count=200):
    ctx = get_ctx()
    p = PERIOD_MAP.get(timeframe.lower())
    if p is None:
        raise ValueError(f"فريم غير مدعوم: {timeframe}")
    return ctx.candlesticks(norm(symbol), p, count,
                            AdjustType.NoAdjust,
                            trade_sessions=TradeSessions.All)


# ============================================================
# خيارات
# ============================================================
def _strike_of(c):
    for attr in ("strike_price", "strike", "price"):
        v = getattr(c, attr, None)
        if v is not None:
            try: return float(v)
            except Exception: pass
    return 0.0


def _call_of(c):
    v = getattr(c, "call_symbol", None)
    if v: return v
    co = getattr(c, "call", None)
    return getattr(co, "symbol", None) if co else None


def _put_of(c):
    v = getattr(c, "put_symbol", None)
    if v: return v
    po = getattr(c, "put", None)
    return getattr(po, "symbol", None) if po else None


def fetch_option_data(symbol, direction, price, strategy="weekly"):
    ctx = get_ctx()
    sym = norm(symbol)
    result = {"strike":"—","expiry":"—","dte":"—","premium":"—","delta":None,
              "call_oi":[],"put_oi":[],"total_call_oi":0,"total_put_oi":0,
              "total_call_vol":0,"total_put_vol":0,"whales":[]}
    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates: return result
        today = datetime.now(timezone.utc).date()
        target_dte = 52 if strategy == "weekly" else 37
        parsed = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today: parsed.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today: parsed.append(dd)
                except Exception: continue
        if not parsed: return result
        exp_date, dte = min([(d, (d - today).days) for d in parsed],
                            key=lambda x: abs(x[1] - target_dte))
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte

        chain = ctx.option_chain_info_by_date(sym, exp_date)
        if not chain: return result

        base_match = re.match(r'^([A-Z]+)', sym.replace(".US", ""))
        base_sym = base_match.group(1) if base_match else sym.replace(".US", "")
        yy = exp_date.strftime("%y"); mm = exp_date.strftime("%m"); dd = exp_date.strftime("%d")
        prefix = f"{base_sym}{yy}{mm}{dd}"

        def build_call_sym(sk):
            return f"{prefix}C{str(int(round(sk * 1000))).zfill(8)}.US"

        def build_put_sym(sk):
            return f"{prefix}P{str(int(round(sk * 1000))).zfill(8)}.US"

        if direction == "bullish":
            target = price * 1.015
            cands = [c for c in chain if _call_of(c) and _strike_of(c) > price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _call_of(best); strike = _strike_of(best); opt_type = "C"
        else:
            target = price * 0.985
            cands = [c for c in chain if _put_of(c) and _strike_of(c) < price]
            if not cands: return result
            best = min(cands, key=lambda c: abs(_strike_of(c) - target))
            option_symbol = _put_of(best); strike = _strike_of(best); opt_type = "P"

        result["strike"] = f"{opt_type} {int(strike)}"
        try:
            oqs = ctx.option_quote([option_symbol])
            if oqs:
                oq = oqs[0]
                for attr in ("last_done", "last", "price"):
                    v = getattr(oq, attr, None)
                    if v is not None:
                        result["premium"] = round(float(v), 2); break
                if hasattr(oq, "delta"):
                    result["delta"] = round(float(oq.delta), 3)
        except Exception: pass

        all_call_syms, all_put_syms = [], []
        strikes_map = {}

        for c in chain:
            sk = _strike_of(c)
            if sk <= 0: continue
            cs = _call_of(c) or build_call_sym(sk)
            ps = _put_of(c)  or build_put_sym(sk)
            all_call_syms.append(cs)
            all_put_syms.append(ps)
            strikes_map[sk] = (cs, ps)

        qmap = {}
        all_syms = list(set(all_call_syms + all_put_syms))
        for i in range(0, len(all_syms), 30):
            try:
                qs = ctx.option_quote(all_syms[i:i+30])
                if qs:
                    for q in qs: qmap[q.symbol] = q
            except Exception: pass

        tc_oi = tp_oi = tc_v = tp_v = 0
        for s in all_call_syms:
            if s in qmap:
                q = qmap[s]
                tc_oi += int(getattr(q, "open_interest", 0) or 0)
                tc_v  += int(getattr(q, "volume", 0) or 0)
        for s in all_put_syms:
            if s in qmap:
                q = qmap[s]
                tp_oi += int(getattr(q, "open_interest", 0) or 0)
                tp_v  += int(getattr(q, "volume", 0) or 0)

        result["total_call_oi"]  = tc_oi
        result["total_put_oi"]   = tp_oi
        result["total_call_vol"] = tc_v
        result["total_put_vol"]  = tp_v

        nearby = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:5]
        cd, pd_ = [], []
        for c in nearby:
            sk = int(_strike_of(c))
            cs, ps = strikes_map.get(_strike_of(c), (None, None))
            if cs and cs in qmap:
                q = qmap[cs]
                cd.append({"strike": sk,
                           "oi": int(getattr(q,"open_interest",0) or 0),
                           "volume": int(getattr(q,"volume",0) or 0)})
            if ps and ps in qmap:
                q = qmap[ps]
                pd_.append({"strike": sk,
                            "oi": int(getattr(q,"open_interest",0) or 0),
                            "volume": int(getattr(q,"volume",0) or 0)})
        cd.sort(key=lambda x: x["strike"], reverse=True)
        pd_.sort(key=lambda x: x["strike"], reverse=True)
        result["call_oi"] = cd
        result["put_oi"]  = pd_

        whales = []
        wide = sorted(chain, key=lambda c: abs(_strike_of(c) - price))[:10]
        for c in wide:
            sk = int(_strike_of(c))
            cs, ps = strikes_map.get(_strike_of(c), (None, None))
            for sym_opt, tp_ in ((cs, "CALL"), (ps, "PUT")):
                if not sym_opt or sym_opt not in qmap: continue
                q = qmap[sym_opt]
                vol = int(getattr(q, "volume", 0) or 0)
                oi  = int(getattr(q, "open_interest", 0) or 0)
                if vol < WHALE_MIN_VOLUME and oi < WHALE_MIN_OI: continue
                bid = float(getattr(q, "bid", 0) or 0)
                ask = float(getattr(q, "ask", 0) or 0)
                last = float(getattr(q, "last_done", 0) or getattr(q, "last", 0) or 0)
                dw = "mid"
                if ask > bid > 0:
                    sp = ask - bid
                    pos = (last - bid) / sp if sp > 0 else 0.5
                    if pos >= 0.7: dw = "buy"
                    elif pos <= 0.3: dw = "sell"
                whales.append({"strike": sk, "type": tp_, "volume": vol, "oi": oi,
                               "bid": round(bid,2), "ask": round(ask,2),
                               "last": round(last,2), "direction": dw})
        whales.sort(key=lambda w: w["volume"], reverse=True)
        result["whales"] = whales[:5]
    except Exception as e:
        print(f"[OPT] {e}", flush=True)
    return result


# ============================================================
# analyze_symbol
# ============================================================
def analyze_symbol(symbol: str) -> dict:
    df_weekly = candles_to_df(fetch_candles(symbol, "1w", 100))
    df_daily  = candles_to_df(fetch_candles(symbol, "1d", 200))
    df_4h     = candles_to_df(fetch_candles(symbol, "4h", 200))
    df_1h     = candles_to_df(fetch_candles(symbol, "1h", 200))

    def enrich(df):
        df["ema20"] = ema(df["close"], 20)
        df["ema50"] = ema(df["close"], 50)
        df["rsi"]   = rsi(df["close"], 14)
        df["atr"]   = atr(df, 14)
        df["adx"]   = adx(df, 14)
        df["vwap"]  = vwap(df)
        return df

    df_weekly = enrich(df_weekly)
    df_daily  = enrich(df_daily)
    df_4h     = enrich(df_4h)
    df_1h     = enrich(df_1h)

    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    sig = swing_signal(df_weekly, df_daily)

    card = {
        "color":       sig["color"],
        "label":       sig["label"],
        "score":       sig["score"],
        "status":      sig["status"],
        "weekly_macd": sig["weekly_macd"],
        "daily_rsi":   sig["daily_rsi"],
        "rsi_prev":    sig["rsi_prev"],
        "adx":         sig["adx"],
        "adx_ok":      sig["adx_ok"],
        "volume_ok":   sig["volume_ok"],
    }

    direction = sig["direction"] or "bullish"
    levels = compute_swing_levels(price, sig["atr"], direction)

    def tf_snap(df, label):
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
        tf_snap(df_weekly, "1W"),
        tf_snap(df_daily,  "1D"),
        tf_snap(df_4h,     "4H"),
        tf_snap(df_1h,     "1H"),
    ]

    opt = fetch_option_data(symbol, direction, price, "weekly")
    levels["strike"]  = opt.get("strike", "—")
    levels["expiry"]  = opt.get("expiry", "—")
    levels["dte"]     = opt.get("dte", "—")
    levels["premium"] = opt.get("premium", "—")

    last = df_daily.iloc[-1]
    supports = [
        round(float(df_daily["low"].iloc[-20:].min()), 2),
        round(float(df_weekly["low"].iloc[-4:].min()), 2),
    ]
    resistances = [
        round(float(df_daily["high"].iloc[-20:].max()), 2),
        round(float(df_weekly["high"].iloc[-4:].max()), 2),
    ]

    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "change": round(price - prev_close, 2),
        "changePercent": round((price - prev_close) / prev_close * 100, 2) if prev_close else 0,
        "card": card,
        "levels": levels,
        "timeframes": timeframes,
        "vwap": round(float(last["vwap"]), 2),
        "supports": supports,
        "resistances": resistances,
        "call_oi": opt.get("call_oi", []),
        "put_oi":  opt.get("put_oi", []),
        "total_call_oi":  opt.get("total_call_oi", 0),
        "total_put_oi":   opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol":  opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def analyze_cached(symbol):
    key = symbol.upper().strip()
    now = time.time()
    if key in _analyze_cache:
        ts, data = _analyze_cache[key]
        if now - ts < _ANALYZE_TTL:
            return data
    data = analyze_symbol(symbol)
    _analyze_cache[key] = (now, data)
    return data


def build_alert_message(data):
    sym = data["symbol"]; price = data["price"]
    card = data.get("card", {}); lv = data.get("levels", {})
    color = card.get("color", "gray")
    if color not in ("green", "red"): return ""
    header = f"🟢 <b>إشارة CALL</b> — {sym}" if color == "green" else f"🔴 <b>إشارة PUT</b> — {sym}"

    return f"""{header}

💪 قوة الإشارة: <b>{card.get('score', 0)}%</b>
💰 السعر: <b>${price}</b>

📊 <b>التحليل:</b>
  • MACD أسبوعي: <b>{card.get('weekly_macd','—')}</b>
  • RSI يومي: <b>{card.get('daily_rsi','—')}</b>
  • ADX: <b>{card.get('adx','—')}</b> ({'✅' if card.get('adx_ok') else '❌'})
  • Volume: {'✅' if card.get('volume_ok') else '❌'}

📋 <b>العقد:</b>
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


async def watchlist_checker():
    global _sent_alerts, _last_color
    await asyncio.sleep(90)

    while True:
        try:
            symbols = list(_watchlist)
            if symbols:
                print(f"[WATCH] checking {len(symbols)}", flush=True)

            for sym in symbols:
                try:
                    data = await asyncio.to_thread(analyze_symbol, sym)
                    _analyze_cache[sym.upper()] = (time.time(), data)

                    card = data.get("card", {})
                    color = card.get("color", "gray")

                    prev_color = _last_color.get(sym.upper())
                    _last_color[sym.upper()] = color

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
                        print(f"[WATCH] alert → {sym} {color}", flush=True)

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
        print("[STARTUP] ready", flush=True)
    except Exception as e:
        print(f"[STARTUP] error: {e}", flush=True)

    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try:
            send_telegram_alert("🚀 <b>محلل الأسهم</b> — النظام يعمل")
        except Exception: pass

    async def keepalive():
        while True:
            await asyncio.sleep(300)
            try: get_ctx().quote(["AAPL.US"])
            except Exception: pass

    ka = asyncio.create_task(keepalive())
    wc = asyncio.create_task(watchlist_checker())

    yield

    ka.cancel(); wc.cancel()
    global _quote_ctx
    _quote_ctx = None


app = FastAPI(title="Stock Analyzer", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"],
                   allow_methods=["*"], allow_headers=["*"])


@app.get("/api/status")
def status():
    try:
        q = get_ctx().quote(["AAPL.US"])
        if q:
            return {"connected": True, "price": str(q[0].last_done), "symbol": q[0].symbol}
        return {"connected": False}
    except Exception as e:
        return JSONResponse(status_code=503, content={"connected": False, "error": str(e)})


@app.get("/api/health")
def health():
    return {"status": "healthy"}


@app.get("/api/test-telegram")
def test_telegram():
    ok = send_telegram_alert("✅ <b>اختبار ناجح</b>")
    return {"sent": ok, "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


@app.get("/api/watchlist")
def get_wl():
    return {"symbols": sorted(list(_watchlist))}


@app.get("/api/analyze/{symbol}")
def analyze(symbol: str):
    try:
        sym_clean = symbol.upper().strip()
        _watchlist.add(sym_clean)
        return analyze_cached(sym_clean)
    except Exception as e:
        return JSONResponse(status_code=500, content={
            "error": str(e), "type": type(e).__name__,
            "traceback": traceback.format_exc().split("\n")[-10:],
        })


@app.get("/api/remove/{symbol}")
def remove_from_watchlist(symbol: str):
    sym = symbol.upper().strip()
    _watchlist.discard(sym)
    return {"ok": True, "watchlist": sorted(list(_watchlist))}


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
                        "timestamp": last.timestamp.isoformat()}
        except Exception: pass
        q = ctx.quote([sym])
        if q:
            return {"symbol": symbol.upper(), "price": float(q[0].last_done),
                    "timestamp": q[0].timestamp.isoformat()}
        return JSONResponse(status_code=404, content={"error": "no data"})
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e)})


class ConnectionManager:
    def __init__(self): self.active = {}
    async def connect(self, symbol, ws):
        await ws.accept()
        self.active.setdefault(symbol, []).append(ws)
    def disconnect(self, symbol, ws):
        if symbol in self.active and ws in self.active[symbol]:
            self.active[symbol].remove(ws)
    async def broadcast(self, symbol, message):
        for ws in list(self.active.get(symbol, [])):
            try: await ws.send_json(message)
            except Exception: self.disconnect(symbol, ws)


manager = ConnectionManager()


def _on_quote(symbol, event):
    global _event_loop
    if _event_loop is None: return
    try:
        asyncio.run_coroutine_threadsafe(
            manager.broadcast(symbol.replace(".US", ""),
                              {"symbol": symbol.replace(".US", ""),
                               "price": float(event.last_done)}),
            _event_loop)
    except Exception: pass


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
        except Exception: pass
    try:
        while True:
            await asyncio.wait_for(ws.receive_text(), timeout=90)
    except (WebSocketDisconnect, asyncio.TimeoutError):
        manager.disconnect(symbol, ws)


@app.get("/")
def index(): return FileResponse("index.html")

@app.get("/style.css")
def css(): return FileResponse("style.css")

@app.get("/app.js")
def js(): return FileResponse("app.js")


if __name__ == "__main__":
    import uvicorn
    uvicorn.run("main:app", host="0.0.0.0", port=PORT)
