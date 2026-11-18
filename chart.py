"""Draws a trade chart (TradingView light look) from the candles the scanner sends with each new trade."""
import io
from datetime import datetime, timedelta, timezone

import matplotlib
matplotlib.use("Agg")
import matplotlib.pyplot as plt
from matplotlib.patches import Rectangle
from zoneinfo import ZoneInfo

UP, DOWN = "#089981", "#F23645"
GRID, AXIS, TEXT = "#EEF0F3", "#B2B5BE", "#131722"
RF_COL, LVL_COL = "#E0A100", "#9598A1"
UK = ZoneInfo("Europe/London")


def _fmt(x: float, pip: float) -> str:
    dec = 2 if pip >= 0.01 else 5 if pip < 0.001 else 3
    return f"{x:,.{dec}f}"


def draw(d: dict) -> bytes:
    rows = [tuple(float(v) for v in c.split(",")) for c in str(d["candles"]).split(";") if c.count(",") == 3]
    pip = float(d.get("pip") or 0.1)
    tf = int(str(d.get("tf", "15")) or 15)
    e, sl, tp, rf = (float(d[k]) for k in ("entry", "sl", "tp", "rf"))
    lvl = float(d["lvl"]) if d.get("lvl") not in (None, "") else None
    t0 = int(d.get("t0") or 0)
    n = len(rows)
    ahead = 22

    fig, ax = plt.subplots(figsize=(12, 6.2), dpi=110)
    fig.patch.set_facecolor("white")
    ax.set_facecolor("white")
    for i, (o, h, l, c) in enumerate(rows):
        col = UP if c >= o else DOWN
        ax.plot([i, i], [l, h], color=col, linewidth=1, zorder=2)
        ax.add_patch(Rectangle((i - 0.35, min(o, c)), 0.7, max(abs(c - o), pip * 0.3), facecolor=col, edgecolor=col, linewidth=0.8, zorder=3))

    x0, x1 = n - 1, n - 1 + ahead
    ax.add_patch(Rectangle((x0, min(e, sl)), ahead, abs(e - sl), facecolor=DOWN, alpha=0.16, edgecolor="none", zorder=1))
    ax.add_patch(Rectangle((x0, min(e, tp)), ahead, abs(e - tp), facecolor=UP, alpha=0.16, edgecolor="none", zorder=1))
    ax.plot([x0, x1], [e, e], color="#787B86", linewidth=1.4, zorder=4)
    ax.plot([x0, x1], [sl, sl], color=DOWN, linewidth=1.4, zorder=4)
    ax.plot([x0, x1], [tp, tp], color=UP, linewidth=1.4, zorder=4)
    ax.plot([x0, x1], [rf, rf], color=RF_COL, linewidth=1.2, linestyle=(0, (5, 4)), zorder=4)
    if lvl is not None:
        ax.plot([max(0, n - 40), x0], [lvl, lvl], color=LVL_COL, linewidth=1.1, linestyle=(0, (2, 3)), zorder=2)
        ax.text(max(0, n - 40) + 0.5, lvl, "Failed break", fontsize=8.5, color=LVL_COL, va="bottom")

    lows = [r[2] for r in rows] + [sl, tp]
    highs = [r[1] for r in rows] + [sl, tp]
    pad = (max(highs) - min(lows)) * 0.08
    ax.set_ylim(min(lows) - pad, max(highs) + pad)
    ax.set_xlim(-1, x1 + 8)

    # price tags on the right, like TradingView
    for y, col, word in ((tp, UP, "TP"), (rf, RF_COL, "Risk free"), (e, "#787B86", "Entry"), (sl, DOWN, "SL")):
        ax.text(x1 + 0.6, y, f" {word}  {_fmt(y, pip)} ", fontsize=8.5, color="white", va="center", ha="left",
                bbox=dict(boxstyle="square,pad=0.25", facecolor=col, edgecolor="none"), zorder=6)

    # time axis in UK time
    if t0:
        step = max(1, n // 6)
        ticks = list(range(0, n, step))
        ax.set_xticks(ticks)
        labels = []
        for i in ticks:
            t = datetime.fromtimestamp(t0 / 1000, tz=timezone.utc) + timedelta(minutes=tf * i)
            labels.append(t.astimezone(UK).strftime("%H:%M" if tf < 60 else "%d %b %H:%M"))
        ax.set_xticklabels(labels, fontsize=8.5, color="#787B86")
    else:
        ax.set_xticks([])
    ax.yaxis.tick_right()
    ax.tick_params(axis="y", labelsize=8.5, colors="#787B86", length=0)
    ax.tick_params(axis="x", length=0)
    ax.grid(color=GRID, linewidth=0.8, zorder=0)
    for side in ("top", "left", "bottom", "right"):
        ax.spines[side].set_color(GRID)

    tfn = {5: "5m", 15: "15m", 30: "30m", 60: "1h"}.get(tf, f"{tf}m")
    side = "BUY" if d.get("side") == "buy" else "SELL"
    ax.text(0.01, 0.97, f"{d.get('symbol', '')} · {tfn} · SDX Failed Break · {side}", transform=ax.transAxes,
            fontsize=11, color=TEXT, va="top", fontweight="bold")
    ax.text(0.5, 0.5, "SDX", transform=ax.transAxes, fontsize=90, color="#131722", alpha=0.05,
            ha="center", va="center", fontweight="bold", zorder=0)
    fig.tight_layout()
    buf = io.BytesIO()
    fig.savefig(buf, format="png", facecolor="white")
    plt.close(fig)
    return buf.getvalue()


def demo_candles(price: float, side: str, pip: float, n: int = 60):
    """Made-up candles for the example trade: a swing, a pullback, a run back into it,
    a failed break (wick past, close back inside), then the entry candle closing at `price`.
    Returns (candles, swing level, SL at the wick tip)."""
    import random
    rnd = random.Random(11)
    d = -1 if side == "sell" else 1          # trade direction
    u = -d                                    # direction price travels into the swing
    step = 9 * pip
    path = []
    c = 0.0
    for i in range(n - 2):
        if i < 26:
            drift = u * 0.9
        elif i < 42:
            drift = -u * 0.8
        else:
            drift = u * 0.75
        o = c
        c = o + drift * step + rnd.uniform(-0.8, 0.8) * step
        h, l = max(o, c) + rnd.random() * step * 0.5, min(o, c) - rnd.random() * step * 0.5
        path.append([o, h, l, c])
    # the swing: extreme of the first leg; keep the run back just short of it
    swing = max(r[1] for r in path[:30]) if u == 1 else min(r[2] for r in path[:30])
    for r in path[30:]:
        if u == 1:
            r[1] = min(r[1], swing - step * 0.4); r[3] = min(r[3], r[1]); r[0] = min(r[0], r[1])
        else:
            r[2] = max(r[2], swing + step * 0.4); r[3] = max(r[3], r[2]); r[0] = max(r[0], r[2])
    o = path[-1][3]
    fb_close = swing - u * step * 0.8
    tip = swing + u * step * 1.6
    fb = [o, max(o, fb_close, tip), min(o, fb_close, tip), fb_close]
    en_close = fb_close - u * step * 1.4
    en = [fb_close, max(fb_close, en_close) + (step * 0.3 if u == -1 else step * 0.2), min(fb_close, en_close) - (step * 0.2 if u == -1 else step * 0.3), en_close]
    rows = path + [fb, en]
    shift = price - en_close
    rows = [[v + shift for v in r] for r in rows]
    swing += shift
    sl = (max(fb[1], en[1]) if u == 1 else min(fb[2], en[2])) + shift
    return ";".join(",".join(f"{v:.5f}" for v in r) for r in rows), swing, sl
