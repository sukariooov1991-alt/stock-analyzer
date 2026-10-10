"""
main.py — Longbridge Options Radar
استراتيجية Cascade Break & Retest — بحث يدوي
FastAPI + Longbridge + Telegram + Gemini (async)
"""
import os
import time
import asyncio
import traceback
import requests
import re
from contextlib import asynccontextmanager
from datetime import datetime, timezone, date as date_cls
from zoneinfo import ZoneInfo

from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse, FileResponse

from longbridge.openapi import (
    Config, QuoteContext, Period, AdjustType,
    TradeSessions, SubType, PushQuote,
)

from market_time import candles_to_df
from analysis import atr, scan_setup

PORT = int(os.environ.get("PORT", 10000))
_lb_config = Config.from_apikey_env()

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN", "")
TELEGRAM_CHAT_ID   = os.environ.get("TELEGRAM_CHAT_ID", "")

GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY", "")
GEMINI_MODELS  = ["gemini-3.8-flash", "gemini-3.5-flash-lite",
                  "gemini-3.1-flash-lite", "gemini-3.6-flash"]
GEMINI_TIMEOUT = 12

_quote_ctx: QuoteContext | None = None
_event_loop: asyncio.AbstractEventLoop | None = None
_subscribed: set[str] = set()
_sent_alerts: set[str] = set()
_watchlist: set[str] = set()
_analyze_cache: dict[str, tuple[float, dict]] = {}
_ai_cache: dict[str, tuple[float, str]] = {}
_AI_CACHE_TTL = 1800
_ANALYZE_TTL = 25

DTE_MIN = 5
DTE_MAX = 20
DTE_TARGET = 10
OTM_TARGET = 0.02

WHALE_MIN_VOLUME = 3000
WHALE_MIN_OI     = 5000
NEARBY_STRIKES = 5
OPTION_QUOTE_BATCH = 50
VALID_COLORS = ("green", "red")


def get_ctx():
    global _quote_ctx
    if _quote_ctx is None:
        _quote_ctx = QuoteContext(_lb_config)
    return _quote_ctx


def norm(symbol):
    s = symbol.strip().upper().replace("-", ".")
    return s if "." in s else f"{s}.US"


def send_telegram_alert(message):
    if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID:
        return False
    try:
        url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
        r = requests.post(url, json={"chat_id": TELEGRAM_CHAT_ID, "text": message,
                                     "parse_mode": "HTML", "disable_web_page_preview": True},
                          timeout=10)
        return r.status_code == 200
    except Exception as e:
        print(f"[TELEGRAM] {e}", flush=True)
        return False


# ===== Gemini =====
def _build_ai_prompt(data):
    sym = data.get("symbol", "")
    card = data.get("card", {}) or {}
    lv = data.get("levels", {}) or {}
    bi = data.get("break_info", {}) or {}
    tfs = data.get("timeframes", []) or []
    tf_lines = []
    for t in tfs:
        if t.get("broke_resistance"): st = "مخترق صعوداً"
        elif t.get("broke_support"):  st = "مكسور هبوطاً"
        else: st = "محايد"
        tf_lines.append(f"- {t.get('label')}: مقاومة {t.get('resistance')} / دعم {t.get('support')} / {st}")

    color = card.get("color", "gray")
    if color == "green":
        dir_txt = "CALL (صاعد) — إشارة مؤكدة"
    elif color == "red":
        dir_txt = "PUT (هابط) — إشارة مؤكدة"
    elif color == "yellow":
        dir_txt = f"انتظار — {card.get('label', 'قيد المراقبة')}"
    else:
        dir_txt = "لا إشارة حالياً"

    stage_txt = {"daily_break_weekly": "إغلاق يومي خارج قمة/قاع الأسبوع السابق",
                 "4h_break_daily": "إغلاق 4H خارج قمة/قاع اليوم السابق"}.get(card.get("stage"), "—")

    return f"""أنت محلل فني محترف. حلّل الوضع الحالي لهذا السهم في 3-4 أسطر عربية فقط.
ركّز على: قوة الزخم، جودة الاختراق (إن وُجد)، الثبات، السياق، والمخاطرة.
إذا كانت الحالة "انتظار" أو "لا إشارة" — اشرح ما ينتظره السوق ومتى يُفعَّل.
لا Markdown ولا رموز ولا عناوين.

البيانات:
- الرمز: {sym}
- الحالة: {dir_txt}
- المرحلة: {stage_txt}
- مستوى الاختراق: {lv.get('level_broken')}
- الدخول: {lv.get('entry')} | الوقف: {lv.get('stop')} | الهدف: {lv.get('target1')}
- R:R: {lv.get('rr')}
- RVOL: {bi.get('rvol')}
- النمط: {lv.get('pattern')}
- السعر: {data.get('price')}

الفريمات:
{chr(10).join(tf_lines)}

3-4 أسطر فقط.
"""


def _call_gemini_once(prompt, model):
    if not GEMINI_API_KEY: return None
    try:
        url = f"https://generativelanguage.googleapis.com/v1beta/models/{model}:generateContent?key={GEMINI_API_KEY}"
        payload = {"contents": [{"role": "user", "parts": [{"text": prompt}]}],
                   "generationConfig": {"temperature": 0.7, "maxOutputTokens": 260, "topP": 0.95}}
        r = requests.post(url, json=payload, timeout=GEMINI_TIMEOUT)
        if r.status_code != 200:
            print(f"[GEMINI] {model} HTTP {r.status_code}: {r.text[:200]}", flush=True)
            return None
        d = r.json()
        if d.get("promptFeedback", {}).get("blockReason"): return None
        cands = d.get("candidates") or []
        if not cands: return None
        parts = (cands[0].get("content") or {}).get("parts") or []
        text = "".join(p.get("text", "") for p in parts).strip()
        return text or None
    except Exception as e:
        print(f"[GEMINI] {model} exception: {e}", flush=True)
        return None


def get_ai_analysis(data):
    sym = (data.get("symbol") or "").upper().strip()
    if not GEMINI_API_KEY or not sym: return None
    now = time.time()
    cached = _ai_cache.get(sym)
    if cached and now - cached[0] < _AI_CACHE_TTL: return cached[1]
    prompt = _build_ai_prompt(data)
    for model in GEMINI_MODELS:
        text = _call_gemini_once(prompt, model)
        if text:
            _ai_cache[sym] = (now, text)
            return text
    return None


PERIOD_MAP = {"1h": Period.Min_60, "4h": Period.Min_240,
              "1d": Period.Day, "1w": Period.Week}

_candle_cache = {}
_CANDLE_TTL = {"1h": 300, "4h": 900, "1d": 3600, "1w": 3600}
_CANDLE_CACHE_MAX = 5000


def fetch_candles(symbol, timeframe, count=200):
    tf = timeframe.lower()
    key = f"{symbol.upper()}:{tf}:{count}"
    now = time.time()
    ttl = _CANDLE_TTL.get(tf, 60)
    if key in _candle_cache:
        ts, cached = _candle_cache[key]
        if now - ts < ttl and len(cached) >= count:
            return cached
    ctx = get_ctx()
    p = PERIOD_MAP.get(tf)
    if p is None:
        raise ValueError(f"فريم غير مدعوم: {timeframe}")
    candles = ctx.candlesticks(norm(symbol), p, count, AdjustType.NoAdjust,
                                trade_sessions=TradeSessions.Intraday)
    if len(_candle_cache) > _CANDLE_CACHE_MAX:
        oldest = sorted(_candle_cache.items(), key=lambda x: x[1][0])[:1000]
        for k, _ in oldest: _candle_cache.pop(k, None)
    _candle_cache[key] = (now, candles)
    return candles


def get_current_price(symbol):
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        q = ctx.quote([sym])
        if q: return float(q[0].last_done)
    except Exception: pass
    return None


# ===== Options =====
def _strike_of(c):
    for a in ("strike_price", "strike", "price"):
        v = getattr(c, a, None)
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


def _empty_option_result():
    return {"strike": "—", "expiry": "—", "dte": "—", "premium": "—", "delta": None,
            "call_oi": [], "put_oi": [], "total_call_oi": 0, "total_put_oi": 0,
            "total_call_vol": 0, "total_put_vol": 0, "whales": [],
            "filter_pass": False, "filter_reason": ""}


def _quote_one(ctx, sym_opt):
    try:
        oqs = ctx.option_quote([sym_opt])
        if not oqs: return None
        oq = oqs[0]
        last = None
        for a in ("last_done", "last", "price"):
            v = getattr(oq, a, None)
            if v is not None:
                try: last = float(v); break
                except (TypeError, ValueError): continue
        return {"last": last, "bid": float(getattr(oq, "bid", 0) or 0),
                "ask": float(getattr(oq, "ask", 0) or 0), "raw": oq}
    except Exception: return None


def fetch_option_data(symbol, direction, price, strategy="swing"):
    """
    يختار أقرب Strike:
      - bullish: من أقرب 5 CALL فوق السعر
      - bearish: من أقرب 5 PUT تحت السعر
      - الأقرب إلى 2% OTM
      - بدون فلتر سعر/سبريد
    """
    ctx = get_ctx()
    sym = norm(symbol)
    result = _empty_option_result()
    try:
        raw_dates = ctx.option_chain_expiry_date_list(sym)
        if not raw_dates:
            result["filter_reason"] = "no_dates"; return result
        today = datetime.now(ZoneInfo("America/New_York")).date()
        parsed = []
        for d in raw_dates:
            if isinstance(d, date_cls):
                if d > today: parsed.append(d)
            elif isinstance(d, str):
                try:
                    dd = datetime.strptime(d[:10], "%Y-%m-%d").date()
                    if dd > today: parsed.append(dd)
                except Exception: continue
        valid = [(d, (d - today).days) for d in parsed if DTE_MIN <= (d - today).days <= DTE_MAX]
        if not valid:
            result["filter_reason"] = "no_valid_expiry"; return result
        exp_date, dte = min(valid, key=lambda x: abs(x[1] - DTE_TARGET))
        result["expiry"] = exp_date.strftime("%b %d").upper()
        result["dte"] = dte
        chain = ctx.option_chain_info_by_date(sym, exp_date)
        if not chain:
            result["filter_reason"] = "no_chain"; return result

        base_match = re.match(r'^([A-Z.]+)', sym.replace(".US", ""))
        base_sym = base_match.group(1) if base_match else sym.replace(".US", "")
        yy, mm, dd = exp_date.strftime("%y"), exp_date.strftime("%m"), exp_date.strftime("%d")
        prefix = f"{base_sym}{yy}{mm}{dd}"
        def build_call_sym(sk): return f"{prefix}C{str(int(round(sk*1000))).zfill(8)}.US"
        def build_put_sym(sk):  return f"{prefix}P{str(int(round(sk*1000))).zfill(8)}.US"

        is_call = (direction == "bullish")

        calls_above = sorted(
            [c for c in chain if _call_of(c) and _strike_of(c) > price],
            key=lambda c: _strike_of(c)
        )[:NEARBY_STRIKES]
        puts_below = sorted(
            [c for c in chain if _put_of(c) and _strike_of(c) < price],
            key=lambda c: -_strike_of(c)
        )[:NEARBY_STRIKES]

        target_price = price * (1 + OTM_TARGET) if is_call else price * (1 - OTM_TARGET)
        cands = calls_above if is_call else puts_below

        if not cands:
            result["filter_reason"] = "no_call_strike" if is_call else "no_put_strike"
        else:
            best = min(cands, key=lambda c: abs(_strike_of(c) - target_price))
            strike = _strike_of(best)
            opt_sym = _call_of(best) if is_call else _put_of(best)
            opt_type = "C" if is_call else "P"
            result["strike"] = f"{opt_type} {int(strike)}"
            if opt_sym:
                q = _quote_one(ctx, opt_sym)
                if q:
                    last = q["last"]; ask = q["ask"]
                    premium = ask if (ask and ask > 0) else last
                    if premium is not None:
                        result["premium"] = round(premium, 2)
                        result["filter_pass"] = True
                    if hasattr(q["raw"], "delta"):
                        try: result["delta"] = round(float(q["raw"].delta), 3)
                        except Exception: pass
                else:
                    result["filter_reason"] = "no_quote"

        # OI / Whales
        strikes_map = {}
        for c in chain:
            sk = _strike_of(c)
            if sk <= 0: continue
            cs = _call_of(c) or build_call_sym(sk)
            ps = _put_of(c) or build_put_sym(sk)
            strikes_map[sk] = (cs, ps)

        calls_disp = sorted([s for s in strikes_map if s > price])[:NEARBY_STRIKES]
        puts_disp = sorted([s for s in strikes_map if s < price], reverse=True)[:NEARBY_STRIKES]
        display = sorted(set(calls_disp + puts_disp))

        all_syms = set()
        for sk in display:
            cs, ps = strikes_map.get(sk, (None, None))
            if cs: all_syms.add(cs)
            if ps: all_syms.add(ps)
        all_syms = list(all_syms)
        qmap = {}
        for i in range(0, len(all_syms), OPTION_QUOTE_BATCH):
            try:
                qs = ctx.option_quote(all_syms[i:i+OPTION_QUOTE_BATCH])
                if qs:
                    for q in qs: qmap[q.symbol] = q
            except Exception: pass

        tc_oi = tp_oi = tc_v = tp_v = 0
        cd = []
        for sk in sorted(display, reverse=True):
            cs, ps = strikes_map.get(sk, (None, None))
            if cs and cs in qmap:
                q = qmap[cs]
                oi = int(getattr(q, "open_interest", 0) or 0)
                vol = int(getattr(q, "volume", 0) or 0)
                tc_oi += oi; tc_v += vol
                cd.append({"strike": int(sk), "oi": oi, "volume": vol})
            if ps and ps in qmap:
                q = qmap[ps]
                oi = int(getattr(q, "open_interest", 0) or 0)
                vol = int(getattr(q, "volume", 0) or 0)
                tp_oi += oi; tp_v += vol
        pd_ = []
        for sk in sorted(display, reverse=True):
            cs, ps = strikes_map.get(sk, (None, None))
            if ps and ps in qmap:
                q = qmap[ps]
                pd_.append({"strike": int(sk),
                            "oi": int(getattr(q, "open_interest", 0) or 0),
                            "volume": int(getattr(q, "volume", 0) or 0)})
        result["total_call_oi"] = tc_oi; result["total_put_oi"] = tp_oi
        result["total_call_vol"] = tc_v; result["total_put_vol"] = tp_v
        result["call_oi"] = cd; result["put_oi"] = pd_

        whales = []
        for sk in display:
            cs, ps = strikes_map.get(sk, (None, None))
            for sym_opt, tp_ in ((cs, "CALL"), (ps, "PUT")):
                if not sym_opt or sym_opt not in qmap: continue
                q = qmap[sym_opt]
                vol = int(getattr(q, "volume", 0) or 0)
                oi = int(getattr(q, "open_interest", 0) or 0)
                if vol < WHALE_MIN_VOLUME and oi < WHALE_MIN_OI: continue
                bid = float(getattr(q, "bid", 0) or 0)
                ask = float(getattr(q, "ask", 0) or 0)
                lw = float(getattr(q, "last_done", 0) or getattr(q, "last", 0) or 0)
                dw = "mid"
                if ask > bid > 0:
                    sp = ask - bid
                    pos = (lw - bid)/sp if sp > 0 else 0.5
                    if pos >= 0.7: dw = "buy"
                    elif pos <= 0.3: dw = "sell"
                whales.append({"strike": int(sk), "type": tp_, "volume": vol, "oi": oi,
                               "bid": round(bid,2), "ask": round(ask,2),
                               "last": round(lw,2), "direction": dw})
        whales.sort(key=lambda w: w["volume"], reverse=True)
        result["whales"] = whales[:5]
    except Exception as e:
        print(f"[OPT] {symbol} {e}", flush=True)
        result["filter_reason"] = f"exception: {e}"
    return result


# ===== Analyze =====
def analyze_symbol(symbol):
    q = get_ctx().quote([norm(symbol)])[0]
    price = float(q.last_done)
    prev_close = float(q.prev_close)

    df_weekly = candles_to_df(fetch_candles(symbol, "1w", 150), "1w")
    df_daily  = candles_to_df(fetch_candles(symbol, "1d", 400), "1d")
    df_4h     = candles_to_df(fetch_candles(symbol, "4h", 200), "4h")
    df_1h     = candles_to_df(fetch_candles(symbol, "1h", 200), "1h")

    scan = scan_setup(df_weekly, df_daily, df_4h, df_1h)

    tfs = scan.get("timeframes", [])
    tfs_by = {t.get("label"): t for t in tfs}
    def tf_trend(t):
        if not t: return "neutral"
        if t.get("broke_resistance"): return "up"
        if t.get("broke_support"): return "down"
        return "neutral"
    trend_w = tf_trend(tfs_by.get("1W"))
    trend_d = tf_trend(tfs_by.get("1D"))
    trend_4h = tf_trend(tfs_by.get("4H"))

    levels = scan.get("levels", {}) or {}

    direction = "bearish" if scan.get("direction") == "put" else "bullish"
    opt = fetch_option_data(symbol, direction, price, "swing")

    levels["strike"]  = opt.get("strike", "—")
    levels["expiry"]  = opt.get("expiry", "—")
    levels["dte"]     = opt.get("dte", "—")
    levels["premium"] = opt.get("premium", "—")

    card = {
        "color": scan["color"],
        "label": scan["label"],
        "status": scan["status"],
        "direction": scan.get("direction"),
        "stage": scan.get("breakout_stage"),
        "trend_w": trend_w,
        "trend_d": trend_d,
        "trend_4h": trend_4h,
        "rvol": scan.get("break_info", {}).get("rvol", 0),
        "rr": levels.get("rr", 0),
        "score": 0,
    }

    sr = scan.get("support_resistance", {})

    return {
        "symbol": symbol.upper(),
        "price": round(price, 2),
        "prevClose": round(prev_close, 2),
        "change": round(price - prev_close, 2),
        "changePercent": round((price - prev_close)/prev_close*100, 2) if prev_close else 0,
        "card": card,
        "levels": levels,
        "timeframes": tfs,
        "strategy_boxes": scan.get("strategy_boxes", []),
        "support_resistance": sr,
        "break_info": scan.get("break_info", {}),
        "call_oi": opt.get("call_oi", []),
        "put_oi":  opt.get("put_oi", []),
        "total_call_oi":  opt.get("total_call_oi", 0),
        "total_put_oi":   opt.get("total_put_oi", 0),
        "total_call_vol": opt.get("total_call_vol", 0),
        "total_put_vol":  opt.get("total_put_vol", 0),
        "whales": opt.get("whales", []),
        "filter_pass": opt.get("filter_pass", False),
        "filter_reason": opt.get("filter_reason", ""),
        "timestamp": datetime.now(timezone.utc).isoformat(),
    }


def analyze_cached(symbol):
    key = symbol.upper().strip()
    now = time.time()
    if key in _analyze_cache:
        ts, data = _analyze_cache[key]
        if now - ts < _ANALYZE_TTL: return data
    data = analyze_symbol(symbol)
    _analyze_cache[key] = (now, data)
    return data


def build_alert_message(data):
    sym = data["symbol"]; price = data["price"]
    card = data.get("card", {}); lv = data.get("levels", {})
    color = card.get("color", "gray")
    if color not in VALID_COLORS: return ""
    header = f"🟢 <b>إشارة CALL</b> — {sym}" if color == "green" else f"🔴 <b>إشارة PUT</b> — {sym}"
    return f"""{header}

💰 السعر: <b>${price}</b>
📊 المرحلة: <b>{card.get('stage','—')}</b>
⚖️ R:R: <b>{lv.get('rr','—')}</b>

📋 <b>العقد:</b>
  • STRIKE: <b>{lv.get('strike','—')}</b>
  • EXPIRY: <b>{lv.get('expiry','—')}</b> (DTE: {lv.get('dte','—')})
  • PREMIUM: <b>${lv.get('premium','—')}</b>

📈 ENTRY ${lv.get('entry','—')} | STOP ${lv.get('stop','—')} | TARGET ${lv.get('target1','—')}

⏰ {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}
"""


async def watchlist_checker():
    await asyncio.sleep(90)
    while True:
        try:
            for sym in list(_watchlist):
                try:
                    data = await asyncio.to_thread(analyze_symbol, sym)
                    _analyze_cache[sym.upper()] = (time.time(), data)
                    color = data.get("card", {}).get("color", "gray")
                    if color not in VALID_COLORS: continue
                    today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
                    key = f"{sym}:{color}:{today}"
                    if key in _sent_alerts: continue
                    msg = build_alert_message(data)
                    if msg:
                        await asyncio.to_thread(send_telegram_alert, msg)
                        _sent_alerts.add(key)
                except Exception as e:
                    print(f"[WATCH] {sym}: {e}", flush=True)
        except Exception as e:
            print(f"[WATCH] fatal: {e}", flush=True)
        await asyncio.sleep(300)


@asynccontextmanager
async def lifespan(app):
    global _event_loop
    _event_loop = asyncio.get_running_loop()
    try:
        ctx = get_ctx()
        ctx.set_on_quote(_on_quote)
        print("[STARTUP] ready", flush=True)
        if GEMINI_API_KEY:
            print(f"[STARTUP] Gemini enabled", flush=True)
    except Exception as e:
        print(f"[STARTUP] error: {e}", flush=True)
    if TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID:
        try: send_telegram_alert("🚀 <b>Options Radar</b> — النظام يعمل")
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


app = FastAPI(title="Longbridge Options Radar", lifespan=lifespan)
app.add_middleware(CORSMiddleware, allow_origins=["*"], allow_methods=["*"], allow_headers=["*"])


@app.get("/api/status")
def status():
    try:
        q = get_ctx().quote(["AAPL.US"])
        if q: return {"connected": True, "price": str(q[0].last_done), "symbol": q[0].symbol}
        return {"connected": False}
    except Exception as e:
        return JSONResponse(status_code=503, content={"connected": False, "error": str(e)})


@app.get("/api/health")
def health(): return {"status": "healthy"}


@app.get("/api/test-telegram")
def test_telegram():
    ok = send_telegram_alert("✅ <b>اختبار ناجح</b>")
    return {"sent": ok, "configured": bool(TELEGRAM_BOT_TOKEN and TELEGRAM_CHAT_ID)}


@app.get("/api/test-gemini")
def test_gemini():
    if not GEMINI_API_KEY:
        return {"ok": False, "reason": "GEMINI_API_KEY not set", "models_tried": GEMINI_MODELS}
    sample = {"symbol": "TEST", "price": 100.0,
              "card": {"color": "green", "stage": "daily_break_weekly"},
              "levels": {"entry": 100, "stop": 98, "target1": 104, "rr": 2.0,
                         "level_broken": 99, "pattern": "hammer"},
              "break_info": {"rvol": 1.8},
              "timeframes": [{"label": "1W", "support": 95, "resistance": 99, "broke_resistance": True},
                             {"label": "1D", "support": 97, "resistance": 99, "broke_resistance": True},
                             {"label": "4H", "support": 98, "resistance": 101, "broke_resistance": False},
                             {"label": "1H", "support": 99, "resistance": 102, "broke_resistance": False}]}
    text = get_ai_analysis(sample)
    return {"ok": bool(text), "text": text, "models_tried": GEMINI_MODELS}


@app.get("/api/ai/{symbol}")
async def ai_analysis(symbol):
    if not GEMINI_API_KEY: return {"ok": False, "reason": "no_key"}
    sym = symbol.upper().strip()
    if not sym: return {"ok": False, "reason": "no_symbol"}
    cached = _ai_cache.get(sym)
    if cached and time.time() - cached[0] < _AI_CACHE_TTL:
        return {"ok": True, "text": cached[1], "cached": True}
    try:
        data = await asyncio.to_thread(analyze_cached, sym)
    except Exception as e:
        return {"ok": False, "reason": f"analyze_error: {e}"}
    try:
        text = await asyncio.to_thread(get_ai_analysis, data)
    except Exception as e:
        return {"ok": False, "reason": f"ai_error: {e}"}
    return {"ok": bool(text), "text": text}


@app.get("/api/watchlist")
def get_wl(): return {"symbols": sorted(list(_watchlist))}


@app.get("/api/analyze/{symbol}")
def analyze(symbol):
    try:
        s = symbol.upper().strip()
        _watchlist.add(s)
        return analyze_cached(s)
    except Exception as e:
        return JSONResponse(status_code=500, content={"error": str(e), "type": type(e).__name__,
                                                       "traceback": traceback.format_exc().split("\n")[-10:]})


@app.get("/api/remove/{symbol}")
def remove_from_watchlist(symbol):
    _watchlist.discard(symbol.upper().strip())
    return {"ok": True, "watchlist": sorted(list(_watchlist))}


@app.get("/api/price/{symbol}")
def price_only(symbol):
    try:
        ctx = get_ctx()
        sym = norm(symbol)
        try:
            candles = ctx.candlesticks(sym, Period.Min_1, 1, AdjustType.NoAdjust, trade_sessions=TradeSessions.All)
            if candles:
                last = candles[-1]
                return {"symbol": symbol.upper(), "price": float(last.close), "timestamp": last.timestamp.isoformat()}
        except Exception: pass
        q = ctx.quote([sym])
        if q:
            return {"symbol": symbol.upper(), "price": float(q[0].last_done), "timestamp": q[0].timestamp.isoformat()}
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
                              {"symbol": symbol.replace(".US", ""), "price": float(event.last_done)}),
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
