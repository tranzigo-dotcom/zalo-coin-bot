#!/usr/bin/env python3
"""
Zalo Coin Bot — mỗi ngày 21:00 (giờ VN) gửi 1 mã coin kèm phân tích, chart,
xác suất chạm +5% trong 24h tới, và tự chấm điểm kèo hôm trước.

Cách dùng:
    python bot.py chatid     # lấy chat_id sau khi bạn nhắn tin cho bot
    python bot.py analyze    # quét thị trường, chọn coin, vẽ chart -> data/pending.json
    python bot.py send       # chờ tới 21:00, chấm kèo cũ, gửi kèo mới qua Zalo
Biến môi trường: ZALO_BOT_TOKEN, ZALO_CHAT_ID, (tùy chọn) IMGBB_API_KEY, MODE=test|daily
"""
import base64
import datetime as dt
import json
import os
import sys
import time
from pathlib import Path

import numpy as np
import pandas as pd
import requests

# ----------------------------- Cấu hình ------------------------------------
VN = dt.timezone(dt.timedelta(hours=7))
ROOT = Path(__file__).resolve().parent
DATA = ROOT / "data"
CHARTS = ROOT / "charts"
PICKS = DATA / "picks.csv"
PENDING = DATA / "pending.json"
FINISHED = DATA / "finished.txt"

TP_PCT = float(os.getenv("TP_PCT", "5"))          # mục tiêu chốt lời (%)
SL_PCT = float(os.getenv("SL_PCT", "5"))          # cắt lỗ (%)
FEE_PCT = float(os.getenv("FEE_PCT", "0.1"))      # phí mỗi chiều (%)
TEST_DAYS = int(os.getenv("TEST_DAYS", "30"))     # số ngày thử nghiệm
SEND_HOUR = int(os.getenv("SEND_HOUR", "21"))     # giờ gửi (giờ VN)
STAKE_USD = float(os.getenv("STAKE_USD", "100"))  # vốn giả định mỗi kèo
MIN_QUOTE_VOL = float(os.getenv("MIN_QUOTE_VOL", "20000000"))  # thanh khoản tối thiểu 24h (USDT)
UNIVERSE_SIZE = int(os.getenv("UNIVERSE_SIZE", "80"))
BT_DAYS = int(os.getenv("BT_DAYS", "45"))         # số ngày backtest walk-forward
MODE = os.getenv("MODE", "daily").strip() or "daily"

TOKEN = os.getenv("ZALO_BOT_TOKEN", "").strip()
CHAT_ID = os.getenv("ZALO_CHAT_ID", "").strip()
IMGBB_KEY = os.getenv("IMGBB_API_KEY", "").strip()
ZALO_API = "https://bot-api.zaloplatforms.com"

BINANCE_BASES = ["https://data-api.binance.vision", "https://api.binance.com", "https://api-gcp.binance.com"]
EXCLUDE = {"USDC", "FDUSD", "TUSD", "USDP", "DAI", "BUSD", "EUR", "AEUR", "EURI", "USDE", "PYUSD",
           "XUSD", "USD1", "BFUSD", "RLUSD", "USDS", "WBTC", "WBETH", "BETH", "PAXG", "XAUT", "GBP", "TRY"}

SESSION_HOURS_UTC = (SEND_HOUR - 7) % 24          # 21h VN = 14h UTC: mốc chia "ngày giao dịch"
BARS_PER_SESSION = 12                             # 12 nến 2h = 24h

FEATS = ["ret1", "ret3", "ret7", "ret14", "dist_ema20", "dist_ema50", "ema_trend", "rsi",
         "macd_hist", "macd_hist_chg", "atr_pct", "range_pct", "vol_ratio", "close_pos",
         "hi20_dist", "hit_rate30", "btc_ret1", "btc_ret7", "btc_dist_ema50"]
FEAT_VI = {
    "ret1": "đà tăng 24h", "ret3": "đà tăng 3 ngày", "ret7": "đà tăng 7 ngày", "ret14": "đà tăng 14 ngày",
    "dist_ema20": "vị trí giá so với EMA20", "dist_ema50": "vị trí giá so với EMA50",
    "ema_trend": "xu hướng EMA20/EMA50", "rsi": "RSI", "macd_hist": "động lượng MACD",
    "macd_hist_chg": "MACD đang cải thiện", "atr_pct": "biên độ dao động lớn (dễ chạm +5%)",
    "range_pct": "phiên gần nhất biến động mạnh", "vol_ratio": "khối lượng tăng so với trung bình",
    "close_pos": "giá đang ở vùng cao của phiên", "hi20_dist": "gần đỉnh 20 ngày",
    "hit_rate30": "thường xuyên chạm +5%/ngày gần đây", "btc_ret1": "BTC 24h thuận lợi",
    "btc_ret7": "BTC 7 ngày thuận lợi", "btc_dist_ema50": "BTC trên EMA50",
}


def log(*a):
    print(f"[{dt.datetime.now(VN):%H:%M:%S}]", *a, flush=True)


# ----------------------------- Binance -------------------------------------
_base_ok = None


def bget(path, params=None):
    global _base_ok
    bases = [_base_ok] + [b for b in BINANCE_BASES if b != _base_ok] if _base_ok else BINANCE_BASES
    last = None
    for base in bases:
        for attempt in range(3):
            try:
                r = requests.get(base + path, params=params, timeout=20)
                if r.status_code == 200:
                    _base_ok = base
                    return r.json()
                last = f"{base} HTTP {r.status_code}"
                if r.status_code in (418, 429):
                    time.sleep(5 * (attempt + 1))
                    continue
                break
            except requests.RequestException as e:
                last = f"{base} {e}"
                time.sleep(1)
    raise RuntimeError(f"Không lấy được dữ liệu Binance: {last}")


def klines(symbol, interval="2h", pages=3, start_ms=None, end_ms=None):
    rows, end = [], end_ms
    for _ in range(pages):
        p = {"symbol": symbol, "interval": interval, "limit": 1000}
        if end:
            p["endTime"] = end
        if start_ms:
            p["startTime"] = start_ms
        k = bget("/api/v3/klines", p)
        if not k:
            break
        rows = k + rows
        if start_ms or len(k) < 1000:
            break
        end = k[0][0] - 1
    df = pd.DataFrame(rows, columns=["open_time", "open", "high", "low", "close", "volume", "close_time",
                                     "qvol", "trades", "tb", "tq", "ig"])
    df = df[["open_time", "open", "high", "low", "close", "qvol", "close_time"]].astype(float)
    df["open_time"] = pd.to_datetime(df.open_time, unit="ms", utc=True)
    df["close_time"] = pd.to_datetime(df.close_time, unit="ms", utc=True)
    return df.drop_duplicates("open_time").sort_values("open_time").reset_index(drop=True)


def select_universe(tickers):
    rows = []
    for t in tickers:
        s = t["symbol"]
        if not s.endswith("USDT"):
            continue
        base = s[:-4]
        if base in EXCLUDE or float(t.get("quoteVolume", 0)) < MIN_QUOTE_VOL or int(t.get("count", 0)) == 0:
            continue
        rows.append((float(t["quoteVolume"]), s))
    uni = [s for _, s in sorted(rows, reverse=True)[:UNIVERSE_SIZE]]
    if "BTCUSDT" not in uni:
        uni.insert(0, "BTCUSDT")
    return uni


# ----------------------------- Xử lý dữ liệu -------------------------------
def sessions(bars, now=None):
    """Gộp nến 2h thành 'ngày giao dịch' kết thúc lúc 21:00 giờ VN."""
    now = now or pd.Timestamp.now(tz="UTC")
    b = bars.copy()
    b["label"] = (b.open_time - pd.Timedelta(hours=SESSION_HOURS_UTC)).dt.floor("D") + pd.Timedelta(days=1)
    b["label"] = b.label.dt.tz_localize(None)
    g = b.groupby("label")
    s = pd.DataFrame({"open": g.open.first(), "high": g.high.max(), "low": g.low.min(),
                      "close": g.close.last(), "qvol": g.qvol.sum(), "n": g.size(),
                      "last_close_time": g.close_time.max()})
    s["complete"] = (s.n == BARS_PER_SESSION) & (s.last_close_time <= now)
    while len(s) and s.n.iloc[0] < BARS_PER_SESSION:      # bỏ phiên đầu bị thiếu
        s = s.iloc[1:]
    if len(s) and s.n.iloc[-1] < 6:                        # phiên mới vừa bắt đầu -> bỏ
        s = s.iloc[:-1]
    return s, b


def rsi(c, n=14):
    d = c.diff()
    up = d.clip(lower=0).ewm(alpha=1 / n, adjust=False).mean()
    dn = (-d.clip(upper=0)).ewm(alpha=1 / n, adjust=False).mean()
    return 100 - 100 / (1 + up / dn.replace(0, np.nan))


def features(s):
    c, h, l, v = s.close, s.high, s.low, s.qvol
    f = pd.DataFrame(index=s.index)
    for n in (1, 3, 7, 14):
        f[f"ret{n}"] = c.pct_change(n)
    e20, e50 = c.ewm(span=20, adjust=False).mean(), c.ewm(span=50, adjust=False).mean()
    f["dist_ema20"], f["dist_ema50"], f["ema_trend"] = c / e20 - 1, c / e50 - 1, e20 / e50 - 1
    f["rsi"] = (rsi(c) - 50) / 50          # chuẩn hoá về [-1, 1]
    macd = c.ewm(span=12, adjust=False).mean() - c.ewm(span=26, adjust=False).mean()
    hist = (macd - macd.ewm(span=9, adjust=False).mean()) / c
    f["macd_hist"], f["macd_hist_chg"] = hist, hist.diff()
    tr = pd.concat([h - l, (h - c.shift()).abs(), (l - c.shift()).abs()], axis=1).max(axis=1)
    f["atr_pct"] = tr.rolling(14).mean() / c
    f["range_pct"] = (h - l) / c
    f["vol_ratio"] = np.log(v / v.rolling(20).mean())
    f["close_pos"] = ((c - l) / (h - l).replace(0, np.nan)).fillna(0.5)
    f["hi20_dist"] = c / h.rolling(20).max() - 1
    hit = (h / c.shift() - 1 >= TP_PCT / 100).astype(float)
    f["hit_rate30"] = hit.rolling(30).mean()
    f["ema20"], f["ema50"] = e20, e50
    # nhãn: phiên kế tiếp có chạm +TP% so với giá đóng phiên này không
    y = (h.shift(-1) / c - 1 >= TP_PCT / 100).astype(float)
    nxt_ok = s.complete.shift(-1).fillna(False).astype(bool)
    f["y"] = y.where(nxt_ok)
    f["next_high"] = (h.shift(-1) / c - 1).where(nxt_ok)
    return f


def simulate(path, entry):
    """Mô phỏng: vào giá entry, chốt lời +TP, cắt lỗ -SL (cùng nến => tính lỗ cho chắc), hết 24h thì đóng."""
    tp, sl = entry * (1 + TP_PCT / 100), entry * (1 - SL_PCT / 100)
    for hi, lo in zip(path.high, path.low):
        if lo <= sl:
            return "SL", -SL_PCT - 2 * FEE_PCT
        if hi >= tp:
            return "TP", TP_PCT - 2 * FEE_PCT
    if len(path) == 0:
        return "NA", 0.0
    return "CLOSE", (path.close.iloc[-1] / entry - 1) * 100 - 2 * FEE_PCT


def fit_model(train):
    from sklearn.linear_model import LogisticRegression
    from sklearn.pipeline import make_pipeline
    from sklearn.preprocessing import StandardScaler
    m = make_pipeline(StandardScaler(), LogisticRegression(C=0.3, max_iter=500))
    m.fit(train[FEATS].values, train.y.values.astype(int))
    return m


# ----------------------------- Phân tích -----------------------------------
def n_picks():
    return len(pd.read_csv(PICKS)) if PICKS.exists() else 0


def analyze():
    DATA.mkdir(exist_ok=True)
    CHARTS.mkdir(exist_ok=True)
    if MODE == "daily" and (FINISHED.exists() or n_picks() >= TEST_DAYS):
        log("Đã đủ số ngày thử nghiệm — không chọn kèo mới.")
        PENDING.unlink(missing_ok=True)
        return
    now = pd.Timestamp.now(tz="UTC")
    uni = select_universe(bget("/api/v3/ticker/24hr"))
    log(f"Quét {len(uni)} coin thanh khoản cao nhất")
    coins = {}
    for sym in uni:
        try:
            s, b = sessions(klines(sym), now)
            if len(s) >= 90:
                coins[sym] = (s, b)
        except Exception as e:
            log("bỏ qua", sym, e)
        time.sleep(0.05)
    if "BTCUSDT" not in coins:
        raise RuntimeError("Không có dữ liệu BTC")

    btc_s = coins["BTCUSDT"][0]
    bf = features(btc_s)
    mkt = pd.DataFrame({"btc_ret1": bf.ret1, "btc_ret7": bf.ret7, "btc_dist_ema50": bf.dist_ema50})
    today = btc_s.index[-1]

    frames = []
    for sym, (s, _) in coins.items():
        f = features(s).join(mkt, how="left")
        f["symbol"], f["close"], f["high"], f["low"] = sym, s.close, s.high, s.low
        frames.append(f.iloc[50:])
    df = pd.concat(frames).rename_axis("date").reset_index()
    df = df.dropna(subset=FEATS)
    df[FEATS] = df[FEATS].clip(-10, 10)

    # --- Backtest walk-forward: mỗi ngày chỉ dùng dữ liệu quá khứ để chọn top-1
    known_dates = sorted(df.loc[df.y.notna(), "date"].unique())
    bt = []
    for d in known_dates[-BT_DAYS:]:
        tr = df[(df.date < d) & df.y.notna()]
        te = df[(df.date == d) & df.y.notna()]
        if len(tr) < 500 or te.empty:
            continue
        p = fit_model(tr).predict_proba(te[FEATS].values)[:, 1]
        top = te.iloc[int(np.argmax(p))]
        s, b = coins[top.symbol]
        nxt = s.index[s.index.get_loc(d) + 1]
        res, pnl = simulate(b[b.label == nxt], top.close)
        # đối chứng: trung bình nếu chọn ngẫu nhiên 1 coin
        rnd = []
        for _, r in te.iterrows():
            ss, bb = coins[r.symbol]
            rnd.append(simulate(bb[bb.label == nxt], r.close)[1])
        bt.append({"date": str(pd.Timestamp(d).date()), "symbol": top.symbol, "prob": float(p.max()),
                   "hit": int(top.y), "pnl": pnl, "rand_hit": float(te.y.mean()), "rand_pnl": float(np.mean(rnd))})
    btd = pd.DataFrame(bt)

    # --- Mô hình cho hôm nay
    train = df[df.y.notna()]
    model = fit_model(train)
    now_rows = df[df.date == today].copy()
    if now_rows.empty:
        raise RuntimeError("Không có dữ liệu phiên hôm nay")
    now_rows["prob"] = model.predict_proba(now_rows[FEATS].values)[:, 1]
    now_rows = now_rows.sort_values("prob", ascending=False)
    pick = now_rows.iloc[0]
    sym = pick.symbol
    s, b = coins[sym]

    scaler, lr = model.named_steps["standardscaler"], model.named_steps["logisticregression"]
    z = (pick[FEATS].values.astype(float) - scaler.mean_) / scaler.scale_
    contrib = sorted(zip(FEATS, lr.coef_[0] * z), key=lambda x: -x[1])
    reasons = [FEAT_VI[k] for k, v in contrib if v > 0][:3]

    recent = df[df.date >= known_dates[-30]] if len(known_dates) >= 30 else df
    fng = None
    try:
        j = requests.get("https://api.alternative.me/fng/?limit=1", timeout=10).json()["data"][0]
        fng = {"value": int(j["value"]), "label": j["value_classification"]}
    except Exception:
        pass
    breadth = now_rows.ret1.gt(0).sum()

    chart = draw_chart(sym, s, pick.close)
    pending = {
        "date": str(today.date()), "symbol": sym, "prob": float(pick.prob),
        "price_ref": float(pick.close), "reasons": reasons,
        "ret1": float(pick.ret1), "ret7": float(pick.ret7), "rsi": float(rsi(s.close).iloc[-1]),
        "dist_ema20": float(pick.dist_ema20), "dist_ema50": float(pick.dist_ema50),
        "ema_trend": float(pick.ema_trend), "macd_hist_chg": float(pick.macd_hist_chg),
        "macd_hist": float(pick.macd_hist), "vol_ratio": float(np.exp(pick.vol_ratio)),
        "atr_pct": float(pick.atr_pct), "coin_hit30": float(pick.hit_rate30),
        "support": float(s.low.iloc[-14:].min()), "resist": float(s.high.iloc[-14:].max()),
        "market_hit": float(recent.y.mean()),
        "btc": {"price": float(btc_s.close.iloc[-1]), "ret1": float(bf.ret1.iloc[-1]),
                "ret7": float(bf.ret7.iloc[-1]), "above_ema50": bool(bf.dist_ema50.iloc[-1] > 0)},
        "fng": fng, "breadth": int(breadth), "n_coins": int(len(now_rows)),
        "others": [(r.symbol, float(r.prob)) for _, r in now_rows.iloc[1:4].iterrows()],
        "bt": None if btd.empty else {
            "n": len(btd), "hits": int(btd.hit.sum()), "avg_pnl": float(btd.pnl.mean()),
            "sum_pnl": float(btd.pnl.sum()), "rand_hit": float(btd.rand_hit.mean()),
            "rand_pnl": float(btd.rand_pnl.mean())},
        "chart": chart.name,
    }
    PENDING.write_text(json.dumps(pending, ensure_ascii=False, indent=2))
    log(f"Chọn {sym} — xác suất {pick.prob:.0%}. Top: {[(a, round(b_, 2)) for a, b_ in pending['others']]}")


def draw_chart(sym, s, ref):
    import matplotlib
    matplotlib.use("Agg")
    import matplotlib.pyplot as plt
    n = 70
    d = s.iloc[-n:]
    full = s.close
    e20 = full.ewm(span=20, adjust=False).mean().iloc[-n:]
    e50 = full.ewm(span=50, adjust=False).mean().iloc[-n:]
    r = rsi(full).iloc[-n:]
    tp, sl = ref * (1 + TP_PCT / 100), ref * (1 - SL_PCT / 100)
    x = np.arange(len(d))
    up = (d.close >= d.open).values
    col = np.where(up, "#16a34a", "#dc2626")

    fig, (a1, a2, a3) = plt.subplots(3, 1, figsize=(11, 8), sharex=True,
                                     gridspec_kw={"height_ratios": [5, 1.3, 1.5], "hspace": 0.05})
    a1.vlines(x, d.low, d.high, color=col, linewidth=1)
    a1.bar(x, (d.close - d.open).abs().clip(lower=d.close * 1e-4), bottom=np.minimum(d.open, d.close),
           color=col, width=0.65)
    a1.plot(x, e20.values, color="#f59e0b", lw=1.3, label="EMA20")
    a1.plot(x, e50.values, color="#3b82f6", lw=1.3, label="EMA50")
    for lvl, c_, lab in [(tp, "#16a34a", f"Chốt lời +{TP_PCT:g}%"), (ref, "#6b7280", "Giá tham chiếu"),
                         (sl, "#dc2626", f"Cắt lỗ -{SL_PCT:g}%")]:
        a1.axhline(lvl, color=c_, ls="-.", lw=1)
        a1.text(len(d) - 0.5, lvl, f" {lab}: {fmt_price(lvl)}", color=c_, va="center", fontsize=8)
    a1.set_title(f"{sym} — nến ngày (chốt 21h giờ VN)", fontsize=12, loc="left")
    a1.legend(loc="upper left", fontsize=8, frameon=False)
    a1.grid(ls=":", alpha=0.5)
    a2.bar(x, d.qvol / 1e6, color=col, width=0.65, alpha=0.7)
    a2.set_ylabel("KL (tr USDT)", fontsize=8)
    a2.grid(ls=":", alpha=0.5)
    a3.plot(x, r.values, color="#8b5cf6", lw=1.2)
    a3.axhline(70, color="#9ca3af", ls="--", lw=0.7)
    a3.axhline(30, color="#9ca3af", ls="--", lw=0.7)
    a3.set_ylim(0, 100)
    a3.set_ylabel("RSI14", fontsize=8)
    a3.grid(ls=":", alpha=0.5)
    ticks = x[::10]
    a3.set_xticks(ticks)
    a3.set_xticklabels([pd.Timestamp(t).strftime("%d/%m") for t in d.index[::10]], fontsize=8)
    a1.set_xlim(-1, len(d) + 9)
    path = CHARTS / f"{dt.datetime.now(VN):%Y%m%d}_{sym}.png"
    fig.savefig(path, dpi=110, bbox_inches="tight", facecolor="white")
    plt.close(fig)
    return path


# ----------------------------- Zalo ----------------------------------------
def zalo(method, payload=None):
    if not TOKEN:
        raise RuntimeError("Thiếu ZALO_BOT_TOKEN")
    r = requests.post(f"{ZALO_API}/bot{TOKEN}/{method}", json=payload or {}, timeout=40)
    try:
        j = r.json()
    except ValueError:
        raise RuntimeError(f"Zalo {method}: HTTP {r.status_code} {r.text[:200]}")
    if not j.get("ok"):
        raise RuntimeError(f"Zalo {method} lỗi {j.get('error_code')}: {j.get('description')}")
    return j.get("result")


def send_text(text):
    chunks, cur = [], ""
    for line in text.split("\n"):
        if len(cur) + len(line) + 1 > 1900:
            chunks.append(cur)
            cur = ""
        cur += line + "\n"
    if cur.strip():
        chunks.append(cur)
    for c in chunks:
        zalo("sendMessage", {"chat_id": CHAT_ID, "text": c.strip()})
        time.sleep(1)


def chart_url(path):
    if IMGBB_KEY:
        r = requests.post("https://api.imgbb.com/1/upload",
                          data={"key": IMGBB_KEY, "image": base64.b64encode(path.read_bytes())}, timeout=60)
        return r.json()["data"]["url"]
    repo, branch = os.getenv("GITHUB_REPOSITORY"), os.getenv("GITHUB_REF_NAME", "main")
    if not repo:
        return None
    return f"https://raw.githubusercontent.com/{repo}/{branch}/charts/{path.name}"


def find_chat_id(obj):
    if isinstance(obj, dict):
        if isinstance(obj.get("chat"), dict) and obj["chat"].get("id"):
            return obj["chat"]["id"], obj.get("text", "")
        for v in obj.values():
            r = find_chat_id(v)
            if r:
                return r
    if isinstance(obj, list):
        for v in obj:
            r = find_chat_id(v)
            if r:
                return r
    return None


def cmd_chatid():
    me = zalo("getMe")
    log("Bot:", json.dumps(me, ensure_ascii=False))
    log("Đang chờ tin nhắn (hãy nhắn 1 câu bất kỳ cho bot trong Zalo)...")
    for _ in range(10):
        try:
            res = zalo("getUpdates", {"timeout": "30"})
        except RuntimeError as e:
            log(e)
            res = None
        found = find_chat_id(res)
        if found:
            log(f"✅ CHAT_ID của bạn: {found[0]}   (tin nhắn: {found[1]!r})")
            log("→ Lưu giá trị này vào GitHub Secret tên ZALO_CHAT_ID")
            return
    log("Chưa nhận được tin nhắn nào. Nhắn cho bot rồi chạy lại.")


# ----------------------------- Gửi & chấm điểm ------------------------------
def fmt_price(p):
    return f"{p:,.2f}" if p >= 100 else f"{p:,.4f}" if p >= 1 else f"{p:.6g}"


def pct(x, d=1):
    return f"{x * 100:+.{d}f}%"


def evaluate_open_picks(now_utc):
    if not PICKS.exists():
        return None
    df = pd.read_csv(PICKS)
    last_eval = None
    for i, r in df[df.outcome.isna()].iterrows():
        start = pd.Timestamp(r.entry_time).floor("15min")
        end = min(start + pd.Timedelta(hours=24), now_utc.floor("15min"))
        if end - start < pd.Timedelta(hours=20):
            continue
        k = klines(r.symbol, "15m", pages=1, start_ms=int(start.timestamp() * 1000))
        k = k[(k.open_time >= start) & (k.open_time < end)]
        if k.empty:
            continue
        res, pnl = simulate(k, r.entry)
        df.loc[i, ["max_up", "max_down", "close_chg", "outcome", "pnl"]] = [
            k.high.max() / r.entry - 1, k.low.min() / r.entry - 1, k.close.iloc[-1] / r.entry - 1, res, pnl]
        last_eval = df.loc[i].to_dict()
    df.to_csv(PICKS, index=False)
    return last_eval


def scoreboard():
    df = pd.read_csv(PICKS)
    done = df[df.outcome.notna()]
    if done.empty:
        return "", done
    eq = STAKE_USD * np.prod(1 + done.pnl / 100)
    txt = (f"📒 Tổng {len(done)} kèo: chạm +{TP_PCT:g}% {int((done.max_up >= TP_PCT / 100).sum())}/{len(done)} "
           f"({(done.max_up >= TP_PCT / 100).mean():.0%}) | lãi/lỗ cộng dồn {done.pnl.sum():+.1f}% "
           f"| {STAKE_USD:g} USD lãi kép → {eq:.1f} USD")
    return txt, done


def result_block(ev):
    icon = {"TP": "✅ Chạm chốt lời", "SL": "❌ Dính cắt lỗ", "CLOSE": "⏹ Đóng lệnh lúc 21h"}.get(ev["outcome"], "")
    return (f"📋 KẾT QUẢ KÈO HÔM QUA — {ev['symbol'].replace('USDT', '')}\n"
            f"Cao nhất {pct(ev['max_up'])} | Thấp nhất {pct(ev['max_down'])} | Giá lúc 21h {pct(ev['close_chg'])}\n"
            f"{icon} → {ev['pnl']:+.2f}% (đã trừ phí)\n")


def final_report():
    df = pd.read_csv(PICKS)
    d = df[df.outcome.notna()]
    if d.empty:
        return "Không có kèo nào để tổng kết."
    hit = (d.max_up >= TP_PCT / 100)
    eq = STAKE_USD * np.prod(1 + d.pnl / 100)
    best, worst = d.loc[d.pnl.idxmax()], d.loc[d.pnl.idxmin()]
    verdict = ("👍 Chiến lược có lãi trong giai đoạn thử." if d.pnl.sum() > 0 else
               "👎 Chiến lược chưa hiệu quả trong giai đoạn thử.")
    return (f"🏁 TỔNG KẾT {len(d)} NGÀY THỬ NGHIỆM\n"
            f"- Chạm +{TP_PCT:g}% trong 24h: {hit.sum()}/{len(d)} ({hit.mean():.0%})\n"
            f"- Xác suất mô hình dự báo trung bình: {d.prob.mean():.0%} (so với thực tế {hit.mean():.0%})\n"
            f"- Chốt lời {int((d.outcome == 'TP').sum())} | Cắt lỗ {int((d.outcome == 'SL').sum())} | "
            f"Đóng 21h {int((d.outcome == 'CLOSE').sum())}\n"
            f"- Lãi/lỗ cộng dồn: {d.pnl.sum():+.1f}% | trung bình {d.pnl.mean():+.2f}%/kèo\n"
            f"- {STAKE_USD:g} USD lãi kép → {eq:.1f} USD\n"
            f"- Tốt nhất: {best.symbol} {best.pnl:+.1f}% ({best.date}) | Tệ nhất: {worst.symbol} {worst.pnl:+.1f}% ({worst.date})\n"
            f"{verdict}\nBot đã dừng gửi kèo. Muốn chạy tiếp: xoá data/finished.txt và tăng TEST_DAYS.")


def compose(p, entry, day_no, entry_time_vn):
    tp, sl = entry * (1 + TP_PCT / 100), entry * (1 - SL_PCT / 100)
    nxt = (entry_time_vn + dt.timedelta(days=1)).strftime("%d/%m")
    trend = ("tăng (giá > EMA20 > EMA50)" if p["dist_ema20"] > 0 and p["ema_trend"] > 0 else
             "giảm (giá < EMA20 < EMA50)" if p["dist_ema20"] < 0 and p["ema_trend"] < 0 else "giằng co")
    r = p["rsi"]
    rsi_txt = "quá mua" if r >= 70 else "quá bán" if r <= 30 else "mạnh" if r >= 55 else "yếu" if r <= 45 else "trung tính"
    to_res = p["resist"] / entry - 1
    lines = [
        f"📊 KÈO {'TEST' if day_no is None else f'NGÀY {day_no}/{TEST_DAYS}'} — {entry_time_vn:%d/%m/%Y %H:%M}",
        f"🪙 {p['symbol'].replace('USDT', '')}/USDT",
        f"Giá tham chiếu: {fmt_price(entry)}",
        f"🎯 Chốt lời +{TP_PCT:g}%: {fmt_price(tp)}",
        f"🛑 Cắt lỗ -{SL_PCT:g}%: {fmt_price(sl)}",
        f"⏱ Đóng lệnh: 21:00 ngày {nxt}",
        "",
        f"📈 Xác suất chạm +{TP_PCT:g}% trong 24h: {p['prob']:.0%}",
        f"- Trung bình cả thị trường 30 ngày qua: {p['market_hit']:.0%}",
        f"- Riêng coin này 30 ngày qua: {p['coin_hit30']:.0%} số ngày",
        f"- Lý do chính: {', '.join(p['reasons']) or 'tổng hợp nhiều chỉ báo'}",
        "",
        "🔎 Kỹ thuật (nến ngày chốt 21h)",
        f"- Xu hướng: {trend}; cách EMA20 {pct(p['dist_ema20'])}, EMA50 {pct(p['dist_ema50'])}",
        f"- RSI14: {r:.0f} ({rsi_txt})",
        f"- MACD: histogram {'đang tăng' if p['macd_hist_chg'] > 0 else 'đang giảm'} ({'dương' if p['macd_hist'] > 0 else 'âm'})",
        f"- Khối lượng 24h: {p['vol_ratio']:.1f}x trung bình 20 ngày",
        f"- Biên độ TB (ATR14): {p['atr_pct']:.1%}/ngày",
        f"- Biến động: 24h {pct(p['ret1'])}, 7 ngày {pct(p['ret7'])}",
        f"- Hỗ trợ (đáy 14 ngày): {fmt_price(p['support'])} | Kháng cự (đỉnh 14 ngày): {fmt_price(p['resist'])}"
        + (f" (còn {to_res:.1%})" if to_res > 0 else " (đang vượt đỉnh)"),
        "",
        "🌍 Thị trường",
        f"- BTC {fmt_price(p['btc']['price'])}: 24h {pct(p['btc']['ret1'])}, 7 ngày {pct(p['btc']['ret7'])}, "
        f"{'trên' if p['btc']['above_ema50'] else 'dưới'} EMA50",
    ]
    if p.get("fng"):
        lines.append(f"- Chỉ số Sợ hãi & Tham lam: {p['fng']['value']} ({p['fng']['label']})")
    lines.append(f"- {p['breadth']}/{p['n_coins']} coin thanh khoản cao tăng trong 24h")
    if p["others"]:
        lines.append("- Ứng viên kế tiếp: " + ", ".join(f"{s.replace('USDT', '')} {pr:.0%}" for s, pr in p["others"]))
    if p.get("bt"):
        b = p["bt"]
        lines += ["", f"🧪 Backtest {b['n']} ngày gần nhất (cùng cách chọn)",
                  f"- Chạm +{TP_PCT:g}%: {b['hits']}/{b['n']} ({b['hits'] / b['n']:.0%}) vs chọn ngẫu nhiên {b['rand_hit']:.0%}",
                  f"- Lãi/lỗ mô phỏng TB: {b['avg_pnl']:+.2f}%/kèo vs ngẫu nhiên {b['rand_pnl']:+.2f}%"]
    lines += ["", "⚠️ Tín hiệu thống kê để thử nghiệm, không phải lời khuyên đầu tư. Chỉ dùng vốn chấp nhận mất."]
    return "\n".join(lines)


def cmd_send():
    if not CHAT_ID:
        raise RuntimeError("Thiếu ZALO_CHAT_ID — chạy chế độ chatid trước")
    test = MODE == "test"
    if not test:
        target = dt.datetime.now(VN).replace(hour=SEND_HOUR, minute=0, second=20, microsecond=0)
        wait = (target - dt.datetime.now(VN)).total_seconds()
        if 0 < wait < 3 * 3600:
            log(f"Chờ {wait / 60:.1f} phút tới {SEND_HOUR}:00...")
            time.sleep(wait)
    now_vn = dt.datetime.now(VN)
    now_utc = pd.Timestamp.now(tz="UTC")

    header = ""
    if not test:
        ev = evaluate_open_picks(now_utc)
        if ev:
            header = result_block(ev)
        sb, done = scoreboard() if PICKS.exists() else ("", None)
        if sb:
            header += sb + "\n"
        all_done = PICKS.exists() and pd.read_csv(PICKS).outcome.notna().all()
        if n_picks() >= TEST_DAYS and all_done and not FINISHED.exists():
            if header:
                send_text(header)
            send_text(final_report())
            FINISHED.write_text(now_vn.isoformat())
            log("Đã gửi tổng kết.")
            return

    if not PENDING.exists():
        if header:
            send_text(header)
        log("Không có kèo mới.")
        return
    p = json.loads(PENDING.read_text())
    entry = float(bget("/api/v3/ticker/price", {"symbol": p["symbol"]})["price"])
    day_no = None if test else n_picks() + 1
    body = compose(p, entry, day_no, now_vn)

    if header:
        send_text(header)
    send_text(body)
    url = chart_url(CHARTS / p["chart"])
    if url:
        try:
            zalo("sendPhoto", {"chat_id": CHAT_ID, "photo": url,
                               "caption": f"{p['symbol']} — chart nến ngày, EMA20/50, RSI"})
        except RuntimeError as e:
            log("Gửi ảnh lỗi:", e)
            send_text(f"📉 Chart: {url}")

    if not test:
        row = {"day": day_no, "date": now_vn.strftime("%Y-%m-%d"), "entry_time": now_utc.isoformat(),
               "symbol": p["symbol"], "entry": entry, "tp": entry * (1 + TP_PCT / 100),
               "sl": entry * (1 - SL_PCT / 100), "prob": p["prob"], "max_up": None, "max_down": None,
               "close_chg": None, "outcome": None, "pnl": None}
        df = pd.read_csv(PICKS) if PICKS.exists() else pd.DataFrame(columns=row.keys())
        pd.concat([df, pd.DataFrame([row])], ignore_index=True).to_csv(PICKS, index=False)
        PENDING.unlink(missing_ok=True)
    log(f"Đã gửi {p['symbol']} giá {entry}")


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else ""
    {"analyze": analyze, "send": cmd_send, "chatid": cmd_chatid}.get(cmd, lambda: print(__doc__))()
