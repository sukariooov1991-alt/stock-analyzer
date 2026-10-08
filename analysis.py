"""
analysis.py
المؤشرات الفنية + منطق SMC + نظام النقاط + المستويات
"""
import numpy as np
import pandas as pd
from typing import Optional


# ============================================================
# المؤشرات الفنية
# ============================================================
def ema(s: pd.Series, n: int) -> pd.Series:
    return s.ewm(span=n, adjust=False).mean()


def rsi(s: pd.Series, n: int = 14) -> pd.Series:
    d = s.diff()
    g = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    l = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    rs = g / l.replace(0, np.nan)
    return 100 - 100 / (1 + rs)


def atr(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l, c = df["high"], df["low"], df["close"]
    pc = c.shift(1)
    tr = pd.concat(
        [h - l, (h - pc).abs(), (l - pc).abs()], axis=1
    ).max(axis=1)
    return tr.ewm(alpha=1 / n, adjust=False).mean()


def adx(df: pd.DataFrame, n: int = 14) -> pd.Series:
    h, l = df["high"], df["low"]
    up, dn = h.diff(), -l.diff()
    pdm = np.where((up > dn) & (up > 0), up, 0.0)
    ndm = np.where((dn > up) & (dn > 0), dn, 0.0)
    a = atr(df, n)
    pdi = 100 * pd.Series(pdm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    ndi = 100 * pd.Series(ndm, index=df.index).ewm(alpha=1 / n, adjust=False).mean() / a
    dx = 100 * (pdi - ndi).abs() / (pdi + ndi).replace(0, np.nan)
    return dx.ewm(alpha=1 / n, adjust=False).mean()


def vwap(df: pd.DataFrame) -> pd.Series:
    tp = (df["high"] + df["low"] + df["close"]) / 3
    return (tp * df["volume"]).cumsum() / df["volume"].cumsum().replace(0, np.nan)


def swing_highs(df: pd.DataFrame, k: int = 2) -> list:
    h = df["high"].values
    return [
        i for i in range(k, len(h) - k)
        if all(h[i] > h[i - j] for j in range(1, k + 1))
        and all(h[i] > h[i + j] for j in range(1, k + 1))
    ]


def swing_lows(df: pd.DataFrame, k: int = 2) -> list:
    l = df["low"].values
    return [
        i for i in range(k, len(l) - k)
        if all(l[i] < l[i - j] for j in range(1, k + 1))
        and all(l[i] < l[i + j] for j in range(1, k + 1))
    ]


def rvol(df: pd.DataFrame, n: int = 20) -> float:
    if len(df) < n + 1:
        return 1.0
    avg = df["volume"].iloc[-n - 1:-1].mean()
    return float(df["volume"].iloc[-1] / avg) if avg > 0 else 1.0


# ============================================================
# SMC — اكتشاف سحب السيولة
# ============================================================
def detect_sweep(df: pd.DataFrame, level: float, direction: str) -> Optional[dict]:
    """
    direction = "low"  → سحب من قاع (bullish)
    direction = "high" → سحب من قمة (bearish)
    الشروط: wick يكسر بـ 0.05%–0.5% + body يغلق فوق/تحت المستوى خلال 1-2 شمعة
    """
    n = len(df)
    for i in range(max(0, n - 3), n):
        r = df.iloc[i]
        if direction == "low" and r["low"] < level and r["close"] > level:
            pct = (level - r["low"]) / level * 100
            if 0.05 <= pct <= 0.5:
                return {
                    "index": i,
                    "sweep": float(r["low"]),
                    "level": float(level),
                    "percent": round(pct, 3),
                    "type": "bullish",
                }
        if direction == "high" and r["high"] > level and r["close"] < level:
            pct = (r["high"] - level) / level * 100
            if 0.05 <= pct <= 0.5:
                return {
                    "index": i,
                    "sweep": float(r["high"]),
                    "level": float(level),
                    "percent": round(pct, 3),
                    "type": "bearish",
                }
    return None


# ============================================================
# SMC — اكتشاف IFVG
# ============================================================
def find_ifvg(df: pd.DataFrame, atr_s: pd.Series, direction: str = "bullish") -> Optional[dict]:
    """
    bullish: bearish FVG أُغلقت شمعة فوق أعلاها بجسمها كامل → تصبح دعم (IFVG)
    bearish: bullish FVG أُغلقت شمعة تحت أدناها بجسمها كامل → تصبح مقاومة (IFVG)
    الحد الأدنى للحجم: ATR(14) × 0.5
    """
    n = len(df)
    start = max(2, n - 30)
    for i in range(n - 1, start, -1):
        c0, c2 = df.iloc[i - 2], df.iloc[i]
        a = atr_s.iloc[i]
        if direction == "bullish" and c0["low"] > c2["high"]:
            top, bot = c0["low"], c2["high"]
            if top - bot >= 0.5 * a:
                for j in range(i + 1, n):
                    rj = df.iloc[j]
                    if min(rj["open"], rj["close"]) > top:
                        return {
                            "type": "bullish",
                            "top": float(top),
                            "bottom": float(bot),
                            "ce": float((top + bot) / 2),
                            "size": float(top - bot),
                            "confirm_index": j,
                        }
        if direction == "bearish" and c0["high"] < c2["low"]:
            bot, top = c0["high"], c2["low"]
            if top - bot >= 0.5 * a:
                for j in range(i + 1, n):
                    rj = df.iloc[j]
                    if max(rj["open"], rj["close"]) < bot:
                        return {
                            "type": "bearish",
                            "top": float(top),
                            "bottom": float(bot),
                            "ce": float((top + bot) / 2),
                            "size": float(top - bot),
                            "confirm_index": j,
                        }
    return None


# ============================================================
# SMC — MSS
# ============================================================
def check_mss(df: pd.DataFrame, swing_index: Optional[int], direction: str) -> bool:
    if swing_index is None:
        return False
    level = df["high"].iloc[swing_index] if direction == "bullish" else df["low"].iloc[swing_index]
    for j in range(swing_index + 1, len(df)):
        rj = df.iloc[j]
        if direction == "bullish" and min(rj["open"], rj["close"]) > level:
            return True
        if direction == "bearish" and max(rj["open"], rj["close"]) < level:
            return True
    return False


# ============================================================
# الفلاتر الإضافية
# ============================================================
def check_momentum(df: pd.DataFrame, atr_val: float) -> bool:
    r = df.iloc[-1]
    body = abs(r["close"] - r["open"])
    avg_vol = df["volume"].iloc[-20:].mean()
    return body >= 1.2 * atr_val and r["volume"] >= 1.5 * avg_vol


def check_retest(df: pd.DataFrame, ifvg: dict) -> bool:
    """Low يلامس الفجوة والإغلاق فوق منتصفها"""
    r = df.iloc[-1]
    return r["low"] <= ifvg["top"] and r["close"] >= ifvg["ce"]


# ============================================================
# نظام النقاط
# ============================================================
def calculate_score(sweep, ifvg, trend, rth, momentum, retest) -> int:
    s = 0
    if sweep:    s += 30
    if ifvg:     s += 30
    if trend:    s += 15
    if rth:      s += 10
    if momentum: s += 10
    if retest:   s += 5
    return min(s, 100)


def classify_card(sweep, ifvg, direction: str) -> dict:
    if not sweep:
        return {"status": "gray",   "color": "gray",   "label": "لم تجتز"}
    if not ifvg:
        return {"status": "wait",   "color": "yellow", "label": "انتظار"}
    if direction == "bullish":
        return {"status": "call",   "color": "green",  "label": "تأكيد CALL"}
    return     {"status": "put",    "color": "red",    "label": "تأكيد PUT"}


# ============================================================
# المستويات
# ============================================================
def compute_levels(entry: float, sweep_level: float, ifvg_bottom: float,
                   htf_target: float, atr_val: float) -> dict:
    stop = min(sweep_level, ifvg_bottom) - 0.1 * atr_val
    risk = entry - stop
    return {
        "entry":   round(entry, 2),
        "stop":    round(stop, 2),
        "target1": round(entry + 2 * risk, 2),
        "target2": round(htf_target, 2),
        "risk":    round(risk, 2),
    }
