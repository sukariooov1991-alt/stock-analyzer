"""
analysis.py — Longbridge Options Radar
الاستراتيجية: Sweep + Virgin FVG + تأكيد 1H
Multi-Timeframe: 1W → 1D → 4H → 1H
"""
import numpy as np
import pandas as pd


# ============================================================
# المؤشرات الأساسية
# ============================================================
def ema(s, n):
    return s.ewm(span=n, adjust=False).mean()


def rsi(s, n=14):
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1/n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1/n, adjust=False).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df, n=14):
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat([h - l, (h - pc).abs(), (l - pc).abs()], axis=1).max(axis=1)
    return tr.ewm(alpha=1/n, adjust=False).mean()


def adx(df, n=14):
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1/n, adjust=False).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1/n, adjust=False).mean()


def vwap(df):
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def rvol(df, n=20):
    """حجم الشمعة المكتملة الأخيرة ÷ متوسط n شمعة سابقة"""
    if len(df) < n + 1:
        return 1.0
    avg = df["volume"].iloc[-n-1:-1].mean()
    return float(df["volume"].iloc[-1] / avg) if avg > 0 else 1.0


def macd(s, fast=12, slow=26, signal=9):
    ema_fast = s.ewm(span=fast, adjust=False).mean()
    ema_slow = s.ewm(span=slow, adjust=False).mean()
    macd_line = ema_fast - ema_slow
    signal_line = macd_line.ewm(span=signal, adjust=False).mean()
    hist = macd_line - signal_line
    return macd_line, signal_line, hist


# ============================================================
# Swing Highs / Lows (2 قبل + 2 بعد)
# ============================================================
def swing_highs(df, k=2):
    h = df["high"].values
    result = []
    for i in range(k, len(h) - k):
        if all(h[i] > h[i-j] for j in range(1, k+1)) and \
           all(h[i] > h[i+j] for j in range(1, k+1)):
            result.append(i)
    return result


def swing_lows(df, k=2):
    l = df["low"].values
    result = []
    for i in range(k, len(l) - k):
        if all(l[i] < l[i-j] for j in range(1, k+1)) and \
           all(l[i] < l[i+j] for j in range(1, k+1)):
            result.append(i)
    return result


# ============================================================
# تحديد الاتجاه (Trend)
# ============================================================
def trend_status(df, use_ema200=False):
    """
    up:     Close > EMA50 + بنية صاعدة (قمة أعلى + قاع أعلى)
    down:   Close < EMA50 + بنية هابطة
    neutral: غير ذلك
    """
    if len(df) < 60:
        return {"status": "neutral", "reason": "بيانات غير كافية"}

    close = df["close"]
    e50 = ema(close, 50)
    last_close = float(close.iloc[-1])
    last_ema50 = float(e50.iloc[-1])

    # بنية السعر
    sh_idx = swing_highs(df, 2)
    sl_idx = swing_lows(df, 2)

    higher_highs = False
    higher_lows = False
    lower_highs = False
    lower_lows = False

    if len(sh_idx) >= 2:
        if df["high"].iloc[sh_idx[-1]] > df["high"].iloc[sh_idx[-2]]:
            higher_highs = True
        else:
            lower_highs = True

    if len(sl_idx) >= 2:
        if df["low"].iloc[sl_idx[-1]] > df["low"].iloc[sl_idx[-2]]:
            higher_lows = True
        else:
            lower_lows = True

    above_ema = last_close > last_ema50

    # شروط إضافية (اختياري)
    ema200_ok_up = True
    ema200_ok_down = True
    if use_ema200 and len(df) >= 200:
        e200 = ema(close, 200)
        ema200_ok_up = last_ema50 > float(e200.iloc[-1])
        ema200_ok_down = last_ema50 < float(e200.iloc[-1])

    if above_ema and (higher_highs or higher_lows) and ema200_ok_up:
        return {"status": "up", "reason": "Close > EMA50 + بنية صاعدة"}
    elif not above_ema and (lower_highs or lower_lows) and ema200_ok_down:
        return {"status": "down", "reason": "Close < EMA50 + بنية هابطة"}
    else:
        return {"status": "neutral", "reason": "لا اتجاه واضح"}


# ============================================================
# FVG — Fair Value Gap
# ============================================================
def detect_fvgs(df, atr_series, min_pct=0.30, min_atr_mult=0.50):
    """
    يكتشف كل FVG في الشموع.
    - FVG صاعدة: low[i] > high[i-2] → المنطقة بين high[i-2] و low[i]
    - FVG هابطة: high[i] < low[i-2] → المنطقة بين high[i] و low[i-2]
    يرجع قائمة بكل الفجوات مع حالتها.
    """
    fvgs = []
    n = len(df)
    for i in range(2, n):
        c0 = df.iloc[i-2]
        c2 = df.iloc[i]
        atr_i = atr_series.iloc[i] if i < len(atr_series) else 0

        # فجوة صاعدة
        if c2["low"] > c0["high"]:
            top, bot = c2["low"], c0["high"]
            size = top - bot
            size_pct = size / c0["high"] * 100 if c0["high"] > 0 else 0
            if size_pct >= min_pct and size >= min_atr_mult * atr_i:
                # فحص اللمس
                touched = False
                for j in range(i+1, n):
                    if df.iloc[j]["low"] <= top and df.iloc[j]["high"] >= bot:
                        touched = True
                        break
                fvgs.append({
                    "type": "bullish",
                    "top": float(top),
                    "bottom": float(bot),
                    "size": float(size),
                    "size_pct": round(size_pct, 3),
                    "created_index": i,
                    "touched": touched,
                    "virgin": not touched,
                })

        # فجوة هابطة
        if c2["high"] < c0["low"]:
            top, bot = c0["low"], c2["high"]
            size = top - bot
            size_pct = size / bot * 100 if bot > 0 else 0
            if size_pct >= min_pct and size >= min_atr_mult * atr_i:
                touched = False
                for j in range(i+1, n):
                    if df.iloc[j]["low"] <= top and df.iloc[j]["high"] >= bot:
                        touched = True
                        break
                fvgs.append({
                    "type": "bearish",
                    "top": float(top),
                    "bottom": float(bot),
                    "size": float(size),
                    "size_pct": round(size_pct, 3),
                    "created_index": i,
                    "touched": touched,
                    "virgin": not touched,
                })

    return fvgs


# ============================================================
# Liquidity Sweep
# ============================================================
def detect_sweep(df, level, direction, min_pct=0.15):
    """
    direction = "low": سحب سيولة من قاع (bullish)
        - الشمعة الحالية (الأخيرة): low < level (تجاوز ≥ 0.15%) + close > level
    direction = "high": سحب سيولة من قمة (bearish)
        - الشمعة الحالية: high > level (تجاوز ≥ 0.15%) + close < level
    """
    if len(df) < 2:
        return None

    r = df.iloc[-1]  # الشمعة الأخيرة المكتملة

    if direction == "low":
        if r["low"] < level and r["close"] > level:
            pct = (level - r["low"]) / level * 100
            if pct >= min_pct:
                return {
                    "type": "bullish",
                    "index": len(df) - 1,
                    "sweep": float(r["low"]),
                    "level": float(level),
                    "percent": round(pct, 3),
                }

    if direction == "high":
        if r["high"] > level and r["close"] < level:
            pct = (r["high"] - level) / level * 100
            if pct >= min_pct:
                return {
                    "type": "bearish",
                    "index": len(df) - 1,
                    "sweep": float(r["high"]),
                    "level": float(level),
                    "percent": round(pct, 3),
                }

    return None


# ============================================================
# تسلسل الدخول على 1H
# ============================================================
def check_entry_sequence(df_1h, atr_1h):
    """
    يبحث عن تسلسل الدخول الكامل على 1H:
    Sweep ← Virgin FVG ← عودة ← تأكيد إغلاق
    
    يرجع dict: {found, direction, sweep, fvg, entry, stop, target, rr, confirm_index}
    """
    if len(df_1h) < 30:
        return {"found": False}

    fvgs = detect_fvgs(df_1h, atr_1h)
    sh_idx = swing_highs(df_1h, 2)
    sl_idx = swing_lows(df_1h, 2)

    # البحث من الأحدث للأقدم
    for direction in ["bullish", "bearish"]:
        if direction == "bullish":
            # ابحث عن Sweep من قاع
            if not sl_idx:
                continue
            # آخر 3 قيعان محورية
            for sl in reversed(sl_idx[-3:]):
                if sl >= len(df_1h) - 5:  # القاع قريب جداً من النهاية
                    continue
                level = float(df_1h["low"].iloc[sl])
                # افحص الشموع بعد هذا القاع بحثاً عن sweep
                for k in range(sl + 1, len(df_1h)):
                    sweep = detect_sweep(df_1h.iloc[:k+1], level, "low")
                    if not sweep:
                        continue
                    # وجدنا Sweep — ابحث عن Virgin FVG صاعدة خلال 3 شموع
                    sweep_idx = k
                    for fvg in reversed(fvgs):
                        if fvg["type"] != "bullish":
                            continue
                        ci = fvg["created_index"]
                        if ci < sweep_idx or ci > sweep_idx + 3:
                            continue
                        # تأكد من أنها virgin (لم تُلمس قبل الـ Sweep)
                        touched_before = False
                        for j in range(ci + 1, sweep_idx):
                            if df_1h.iloc[j]["low"] <= fvg["top"] and \
                               df_1h.iloc[j]["high"] >= fvg["bottom"]:
                                touched_before = True
                                break
                        if touched_before:
                            continue

                        # افحص العودة والتأكيد بعد الـ FVG
                        for j in range(ci + 1, len(df_1h)):
                            rj = df_1h.iloc[j]
                            # العودة: السعر لمس الفجوة
                            if rj["low"] <= fvg["top"] and rj["high"] >= fvg["bottom"]:
                                # التأكيد: إغلاق فوق الحد الأعلى للفجوة
                                if rj["close"] > fvg["top"]:
                                    entry = float(rj["close"])
                                    stop = float(sweep["sweep"]) - 0.10 * float(atr_1h.iloc[-1])
                                    risk = entry - stop
                                    if risk <= 0:
                                        continue
                                    # الهدف: أقرب Swing High فوق entry
                                    target = None
                                    for sh in reversed(sh_idx):
                                        h = float(df_1h["high"].iloc[sh])
                                        if h > entry + 0.01:
                                            target = h
                                            break
                                    if target is None:
                                        # احتياط: أعلى 50 شمعة
                                        target = float(df_1h["high"].iloc[-50:].max())
                                    reward = target - entry
                                    rr = reward / risk if risk > 0 else 0

                                    return {
                                        "found": True,
                                        "direction": "bullish",
                                        "sweep": sweep,
                                        "fvg": fvg,
                                        "entry": round(entry, 2),
                                        "stop": round(stop, 2),
                                        "target": round(target, 2),
                                        "rr": round(rr, 2),
                                        "confirm_index": j,
                                    }
                                else:
                                    # اللمس بدون تأكيد → أوقف هذا المسار
                                    break

        else:  # bearish
            if not sh_idx:
                continue
            for sh in reversed(sh_idx[-3:]):
                if sh >= len(df_1h) - 5:
                    continue
                level = float(df_1h["high"].iloc[sh])
                for k in range(sh + 1, len(df_1h)):
                    sweep = detect_sweep(df_1h.iloc[:k+1], level, "high")
                    if not sweep:
                        continue
                    sweep_idx = k
                    for fvg in reversed(fvgs):
                        if fvg["type"] != "bearish":
                            continue
                        ci = fvg["created_index"]
                        if ci < sweep_idx or ci > sweep_idx + 3:
                            continue
                        touched_before = False
                        for j in range(ci + 1, sweep_idx):
                            if df_1h.iloc[j]["low"] <= fvg["top"] and \
                               df_1h.iloc[j]["high"] >= fvg["bottom"]:
                                touched_before = True
                                break
                        if touched_before:
                            continue

                        for j in range(ci + 1, len(df_1h)):
                            rj = df_1h.iloc[j]
                            if rj["low"] <= fvg["top"] and rj["high"] >= fvg["bottom"]:
                                if rj["close"] < fvg["bottom"]:
                                    entry = float(rj["close"])
                                    stop = float(sweep["sweep"]) + 0.10 * float(atr_1h.iloc[-1])
                                    risk = stop - entry
                                    if risk <= 0:
                                        continue
                                    target = None
                                    for sl in reversed(sl_idx):
                                        lo = float(df_1h["low"].iloc[sl])
                                        if lo < entry - 0.01:
                                            target = lo
                                            break
                                    if target is None:
                                        target = float(df_1h["low"].iloc[-50:].min())
                                    reward = entry - target
                                    rr = reward / risk if risk > 0 else 0

                                    return {
                                        "found": True,
                                        "direction": "bearish",
                                        "sweep": sweep,
                                        "fvg": fvg,
                                        "entry": round(entry, 2),
                                        "stop": round(stop, 2),
                                        "target": round(target, 2),
                                        "rr": round(rr, 2),
                                        "confirm_index": j,
                                    }
                                else:
                                    break

    return {"found": False}


# ============================================================
# نظام النقاط (100 نقطة)
# ============================================================
def calculate_score(trend_w, trend_d, trend_4h, sweep, fvg, confirmed, rvol_val):
    """
    التوزيع:
    1W = 25 | 1D = 20 | 4H = 20
    Sweep = 8 | FVG = 7 | Retest+Confirm = 10
    RVOL = 10
    """
    score = 0

    # 1W
    if trend_w == "up" or trend_w == "down":
        score += 25

    # 1D
    if trend_d == "up" or trend_d == "down":
        score += 20

    # 4H
    if trend_4h == "up" or trend_4h == "down":
        score += 20

    # Sweep
    if sweep:
        score += 8

    # FVG
    if fvg:
        score += 7

    # Retest + Confirm
    if confirmed:
        score += 10

    # RVOL (متدرج)
    if rvol_val >= 2.0:
        score += 10
    elif rvol_val >= 1.5:
        score += 7
    elif rvol_val >= 1.0:
        score += 3
    else:
        score += 0

    return score


# ============================================================
# تصنيف البطاقة
# ============================================================
def classify_setup(setup):
    """
    CONFIRMED: تسلسل كامل + RR ≥ 2 + Score ≥ 70
    WAIT:      اتجاه موجود لكن لا تسلسل كامل
    GRAY:      لا اتجاه
    """
    if not setup.get("sequence_found"):
        if setup["trend_w"] in ("up", "down"):
            return {"status": "wait", "color": "yellow", "label": "انتظار"}
        return {"status": "gray", "color": "gray", "label": "لم تجتز"}

    # التسلسل موجود — افحص الشروط
    if setup["rr"] < 2.0:
        return {"status": "wait", "color": "yellow", "label": "انتظار (RR)"}
    if setup["score"] < 70:
        return {"status": "wait", "color": "yellow", "label": "انتظار"}

    if setup["direction"] == "bullish":
        return {"status": "call", "color": "green", "label": "تأكيد CALL"}
    else:
        return {"status": "put", "color": "red", "label": "تأكيد PUT"}


# ============================================================
# المستويات
# ============================================================
def compute_levels(entry, stop, target):
    """يرجع dict بالمستويات"""
    risk = abs(entry - stop)
    reward = abs(target - entry)
    rr = reward / risk if risk > 0 else 0
    return {
        "entry": round(entry, 2),
        "stop": round(stop, 2),
        "target1": round(target, 2),
        "target2": round(target, 2),   # للمستقبل
        "rr": round(rr, 2),
    }


# ============================================================
# الدالة الرئيسية — تُستدعى من main.py
# ============================================================
def scan_setup(df_weekly, df_daily, df_4h, df_1h):
    """
    يرجع dict كامل يحتوي على:
    - trend_w / trend_d / trend_4h
    - sweep / fvg / rr / score
    - color / label / status
    - direction / entry / stop / target
    - rvol
    """
    atr_1h = atr(df_1h, 14)

    # 1) الاتجاهات
    tw = trend_status(df_weekly, use_ema200=False)
    td = trend_status(df_daily, use_ema200=True)
    t4 = trend_status(df_4h, use_ema200=False)

    # 2) التسلسل على 1H
    seq = check_entry_sequence(df_1h, atr_1h)

    # 3) RVOL
    rv = rvol(df_1h, 20)

    # 4) الاتجاه العام
    direction = None
    if seq["found"]:
        direction = seq["direction"]

    # 5) التسجيل
    score = calculate_score(
        trend_w=tw["status"],
        trend_d=td["status"],
        trend_4h=t4["status"],
        sweep=seq.get("sweep"),
        fvg=seq.get("fvg"),
        confirmed=seq.get("confirm_index") is not None,
        rvol_val=rv,
    )

    # 6) تحديد اللون والحالة
    setup_for_classify = {
        "sequence_found": seq["found"],
        "trend_w": tw["status"],
        "direction": direction,
        "score": score,
        "rr": seq.get("rr", 0),
    }
    card = classify_setup(setup_for_classify)

    # 7) المستويات
    levels = {}
    if seq["found"]:
        levels = compute_levels(seq["entry"], seq["stop"], seq["target"])
    else:
        levels = {"entry": None, "stop": None, "target1": None, "target2": None, "rr": 0}

    return {
        "trend_w": tw,
        "trend_d": td,
        "trend_4h": t4,
        "sequence": seq,
        "sweep": seq.get("sweep"),
        "fvg": seq.get("fvg"),
        "rvol": round(rv, 2),
        "score": score,
        "color": card["color"],
        "label": card["label"],
        "status": card["status"],
        "direction": direction,
        "levels": levels,
        "atr_1h": round(float(atr_1h.iloc[-1]), 2),
    }
