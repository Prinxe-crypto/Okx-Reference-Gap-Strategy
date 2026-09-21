#!/usr/bin/env python3
"""
OKX x KALSHI 15-MIN  "DUAL DIRECTION"  PAPER TRADER  (no real orders, ever)

Your strategy, rebuilt for OKX + Kalshi with REAL order-book depth:
  Combo A = Kalshi DOWN + OKX UP      Combo B = Kalshi UP + OKX DOWN
  Buy the same number of shares on both legs when the combined price is under
  a threshold. Payout = $1 per leg that wins, so:
     venues agree on the outcome  -> exactly one leg wins  -> $1 per pair
     venues disagree (the "gap")  -> both win ($2) or both lose ($0)

Every cycle it walks BOTH real books and logs, per asset and combo:
  best combined price, pairs fillable under 80/90/95/98/100c, and the average
  cost + slippage for 10/50/100/250 pairs. If a threshold is met it opens a
  SIMULATED position at the walked (slippage-included) price, then settles it
  from the real results of both venues.

Setup (Colab):
    !pip -q install requests
    os.environ["OKX_API_KEY"/"OKX_API_SECRET"/"OKX_API_PASSPHRASE"] = ...  (read-only key)
Run:
    !python okx_kalshi_dual_paper_v2.py --loop 20 --dir /content/drive/MyDrive/okx_kalshi_paper
Results so far:
    !python okx_kalshi_dual_paper_v2.py --summary --dir /content/drive/MyDrive/okx_kalshi_paper

UNTESTED against the live APIs (built from your existing scripts + docs). If a
call fails, run with --debug and send me the output.
"""
import argparse, base64, csv, hashlib, hmac, json, os, sys, time
from datetime import datetime, timedelta, timezone
import requests

KALSHI = "https://external-api.kalshi.com/trade-api/v2"
OKX = "https://www.okx.com"
ASSETS = {"BTC": "KXBTC15M", "ETH": "KXETH15M", "SOL": "KXSOL15M"}   # asset -> Kalshi series
THRESHOLDS = (0.80, 0.90, 0.95, 0.98, 1.00)
SIZES = (10, 50, 100, 250)          # pairs (shares per leg)
KEY = os.getenv("OKX_API_KEY", ""); SECRET = os.getenv("OKX_API_SECRET", ""); PASS = os.getenv("OKX_API_PASSPHRASE", "")
S = requests.Session(); S.headers.update({"Accept": "application/json"})
DEBUG = False


def utcnow():
    return datetime.now(timezone.utc)


# ----------------------------------------------------------------- HTTP
def jget(url, params=None, tries=3):
    for a in range(tries):
        try:
            r = S.get(url, params=params, timeout=15)
        except requests.RequestException:
            time.sleep(1.5 * (a + 1)); continue
        if r.status_code == 429:
            time.sleep(1.5 * (a + 1)); continue
        if r.status_code >= 400:
            if DEBUG: print("HTTP", r.status_code, url, r.text[:200])
            return None
        try:
            return r.json()
        except ValueError:
            return None
    return None


def okx_get(path, params=None):
    qs = "?" + "&".join(f"{k}={v}" for k, v in params.items()) if params else ""
    n = utcnow()
    ts = n.strftime("%Y-%m-%dT%H:%M:%S.") + f"{n.microsecond // 1000:03d}Z"
    sig = base64.b64encode(hmac.new(SECRET.encode(), f"{ts}GET{path}{qs}".encode(), hashlib.sha256).digest()).decode()
    h = {"OK-ACCESS-KEY": KEY, "OK-ACCESS-SIGN": sig, "OK-ACCESS-TIMESTAMP": ts, "OK-ACCESS-PASSPHRASE": PASS}
    j = requests.get(OKX + path + qs, headers=h, timeout=10).json()
    if DEBUG: print(path, qs, "->", str(j)[:300])
    if j.get("code") != "0":
        raise RuntimeError(f"{path}: {j.get('code')} {j.get('msg')}")
    return j["data"]


# ------------------------------------------------------------ OKX side
def okx_window(inst):
    """BTC-UPDOWN-15MIN-260921-0615-0630 (times UTC+8) -> (start_utc, end_utc)"""
    try:
        d, st, en = inst.split("-")[-3:]
        tz = timezone(timedelta(hours=8)); day = datetime.strptime(d, "%y%m%d")
        a = day.replace(hour=int(st[:2]), minute=int(st[2:]), tzinfo=tz)
        b = day.replace(hour=int(en[:2]), minute=int(en[2:]), tzinfo=tz)
        if b <= a: b += timedelta(days=1)
        return a.astimezone(timezone.utc), b.astimezone(timezone.utc)
    except Exception:
        return None, None


def okx_series_15m():
    out = {}
    for s in okx_get("/api/v5/public/event-contract/series"):
        sid = s.get("seriesId", "")
        if sid.upper().endswith("UPDOWN-15MIN"):
            out[sid.split("-")[0].upper()] = sid
    return out


def okx_live_inst(series_id, now):
    try:
        ms = okx_get("/api/v5/public/event-contract/markets", {"seriesId": series_id, "state": "live"})
    except RuntimeError:
        ms = okx_get("/api/v5/public/event-contract/markets", {"seriesId": series_id})
    for m in ms:
        a, b = okx_window(m.get("instId", ""))
        if a and a <= now < b:
            return m["instId"], b
    return None, None


def okx_book(inst):
    d = okx_get("/api/v5/market/books", {"instId": inst, "sz": 20})
    b = d[0] if d else {"bids": [], "asks": []}
    asks = sorted((float(x[0]), float(x[1])) for x in b.get("asks", []))
    bids = sorted(((float(x[0]), float(x[1])) for x in b.get("bids", [])), reverse=True)
    return {"up": asks, "down": sorted((round(1 - p, 4), q) for p, q in bids)}   # DOWN costs 1 - bid


def parse_okx_outcome(m):
    for k in ("outcome", "result", "settleOutcome", "settlementOutcome", "winner", "settleResult"):
        v = str(m.get(k, "")).strip().lower()
        if v in ("up", "yes", "1", "true"): return "up"
        if v in ("down", "no", "0", "false"): return "down"
    return None


def okx_result(series_id, inst):
    for st in ("settled", "expired", "closed", None):
        try:
            ms = okx_get("/api/v5/public/event-contract/markets", {"seriesId": series_id, **({"state": st} if st else {})})
        except Exception:
            continue
        for m in ms:
            if m.get("instId") == inst:
                with open("okx_settle_debug.jsonl", "a") as f: f.write(json.dumps(m) + "\n")
                r = parse_okx_outcome(m)
                if r: return r
    return None


# --------------------------------------------------------- Kalshi side
def _lv(levels):
    out = []
    for lv in levels or []:
        try: p, q = float(lv[0]), float(lv[1])
        except (TypeError, ValueError, IndexError): continue
        if p > 1.0: p /= 100.0            # cents -> dollars
        if q > 0: out.append((p, q))
    return out


def parse_kalshi_book(j):
    ob = j.get("orderbook_fp") or j.get("orderbook") or {}
    yes_bids = _lv(ob.get("yes_dollars") or ob.get("yes"))
    no_bids = _lv(ob.get("no_dollars") or ob.get("no"))
    # Kalshi lists BIDS only: buying UP (yes) lifts NO bids at 1 - price, and vice versa
    return {"up": sorted((round(1 - p, 4), q) for p, q in no_bids),
            "down": sorted((round(1 - p, 4), q) for p, q in yes_bids)}


def kalshi_book(ticker):
    j = jget(f"{KALSHI}/markets/{ticker}/orderbook")
    return parse_kalshi_book(j) if j else None


def kalshi_live(series, okx_end):
    j = jget(f"{KALSHI}/markets", {"series_ticker": series, "status": "open", "limit": 10})
    for m in (j or {}).get("markets", []):
        try: ct = datetime.fromisoformat(m["close_time"].replace("Z", "+00:00"))
        except Exception: continue
        if abs((ct - okx_end).total_seconds()) <= 60:
            return m["ticker"]
    return None


def kalshi_result(ticker):
    j = jget(f"{KALSHI}/markets/{ticker}")
    m = (j or {}).get("market") or {}
    if m.get("status") not in ("finalized", "settled") or m.get("result") not in ("yes", "no"):
        return None
    return "up" if m["result"] == "yes" else "down"


# ------------------------------------------------------------- the math
def walk_pairs(xa, ya, max_pairs=None, thresh=None):
    """Buy equal shares on both legs, cheapest first. xa/ya: ascending [(price,size)].
    Stops when the MARGINAL combined price >= thresh, or max_pairs reached. -> (pairs, cost)"""
    i = j = 0
    if not xa or not ya: return 0.0, 0.0
    rx, ry = xa[0][1], ya[0][1]; pairs = cost = 0.0
    while i < len(xa) and j < len(ya):
        m = xa[i][0] + ya[j][0]
        if thresh is not None and m >= thresh: break
        t = min(rx, ry)
        if max_pairs is not None: t = min(t, max_pairs - pairs)
        if t <= 1e-9: break
        pairs += t; cost += t * m; rx -= t; ry -= t
        if max_pairs is not None and pairs >= max_pairs - 1e-9: break
        if rx <= 1e-9:
            i += 1
            if i < len(xa): rx = xa[i][1]
        if ry <= 1e-9:
            j += 1
            if j < len(ya): ry = ya[j][1]
    return pairs, cost


def analyse(xa, ya):
    if not xa or not ya: return None
    best = xa[0][0] + ya[0][0]
    r = {"best_x": xa[0][0], "best_y": ya[0][0], "best_combined": round(best, 4)}
    for T in THRESHOLDS: r[f"pairs_lt_{int(round(T * 100))}"] = round(walk_pairs(xa, ya, thresh=T)[0], 1)
    for n in SIZES:
        p, c = walk_pairs(xa, ya, max_pairs=n)
        full = p >= n - 1e-6
        r[f"cost_{n}"] = round(c / p, 4) if full else ""
        r[f"slip_{n}"] = round((c / p - best) * 100, 2) if full else "INSUFFICIENT"
    return r


def payout(combo, k_out, o_out):
    return int(k_out == "down") + int(o_out == "up") if combo == "A" else int(k_out == "up") + int(o_out == "down")


# ---------------------------------------------------------- persistence
POS_COLS = ["asset", "kalshi_ticker", "okx_inst", "okx_series", "combo", "pairs", "cost_per_pair", "best_combined",
            "slip_c", "opened_at", "close_time", "kalshi_out", "okx_out", "payout", "profit_per_pair", "total_profit", "settled_at"]


def load(path):
    if not os.path.exists(path): return []
    with open(path, newline="") as f: return list(csv.DictReader(f))


def save(path, rows):
    with open(path, "w", newline="") as f:
        w = csv.DictWriter(f, fieldnames=POS_COLS, extrasaction="ignore"); w.writeheader(); w.writerows(rows)


def append_rows(path, rows):
    if not rows: return
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()), extrasaction="ignore")
        if new: w.writeheader()
        w.writerows(rows)


# --------------------------------------------------------------- cycle
def maybe_open(args, asset, kt, inst, oser, close, ka, oa, opened_keys, open_rows):
    if kt in opened_keys: return
    cands = []
    for combo, xa, ya in (("A", ka["down"], oa["up"]), ("B", ka["up"], oa["down"])):
        p, c = walk_pairs(xa, ya, thresh=args.entry)
        if p >= args.min_pairs:
            n = min(args.pairs, p); p2, c2 = walk_pairs(xa, ya, max_pairs=n)
            best = xa[0][0] + ya[0][0]
            cands.append((c2 / p2, combo, p2, best))
    if not cands: return
    cpp, combo, n, best = min(cands)
    open_rows.append({"asset": asset, "kalshi_ticker": kt, "okx_inst": inst, "okx_series": oser, "combo": combo,
                      "pairs": round(n, 1), "cost_per_pair": round(cpp, 4), "best_combined": round(best, 4),
                      "slip_c": round((cpp - best) * 100, 2), "opened_at": utcnow().isoformat(timespec="seconds"),
                      "close_time": close.isoformat()})
    opened_keys.add(kt)
    print(f"   >> PAPER OPEN {asset} combo {combo}: {n:.0f} pairs @ {cpp:.3f} (best {best:.3f}, slip {(cpp-best)*100:.1f}c)")


def settle(open_rows, closed_rows):
    keep, now = [], utcnow()
    for r in open_rows:
        close = datetime.fromisoformat(r["close_time"])
        if now < close + timedelta(seconds=90): keep.append(r); continue
        ko = kalshi_result(r["kalshi_ticker"]); oo = okx_result(r["okx_series"], r["okx_inst"])
        if ko is None or oo is None:
            if now < close + timedelta(hours=3): keep.append(r); continue
            ko, oo = ko or "unknown", oo or "unknown"
            r.update(kalshi_out=ko, okx_out=oo, payout="", profit_per_pair="", total_profit="", settled_at=now.isoformat(timespec="seconds"))
        else:
            pay = payout(r["combo"], ko, oo); ppp = pay - float(r["cost_per_pair"])
            r.update(kalshi_out=ko, okx_out=oo, payout=pay, profit_per_pair=round(ppp, 4),
                     total_profit=round(ppp * float(r["pairs"]), 2), settled_at=now.isoformat(timespec="seconds"))
            print(f"   >> SETTLED {r['asset']} {r['combo']}: Kalshi {ko}, OKX {oo}, payout {pay}, profit/pair {ppp:+.3f}")
        closed_rows.append(r)
    return keep


def cycle(args, okx_ser, open_rows, closed_rows):
    now = utcnow(); snaps = []
    opened = {r["kalshi_ticker"] for r in open_rows + closed_rows}
    for asset, kser in ASSETS.items():
        oser = okx_ser.get(asset)
        if not oser: continue
        inst, end = okx_live_inst(oser, now)
        if not inst: print(f"{asset}: no live OKX 15m market"); continue
        kt = kalshi_live(kser, end)
        if not kt: print(f"{asset}: no matching Kalshi market for {end:%H:%M}Z"); continue
        ka = kalshi_book(kt); oa = okx_book(inst)
        if not ka or not oa: print(f"{asset}: book fetch failed"); continue
        secs = int((end - now).total_seconds()); line = f"{asset} {secs:>4}s "
        for combo, xa, ya in (("A", ka["down"], oa["up"]), ("B", ka["up"], oa["down"])):
            r = analyse(xa, ya)
            if not r: line += f"| {combo}: no book "; continue
            r.update(ts=now.isoformat(timespec="seconds"), asset=asset, combo=combo, secs_to_close=secs, kalshi_ticker=kt, okx_inst=inst)
            snaps.append(r)
            line += f"| {combo}: best {r['best_combined']:.2f} <80c:{r['pairs_lt_80']:.0f} <90c:{r['pairs_lt_90']:.0f} slip@50:{r['slip_50']} "
        print(line)
        maybe_open(args, asset, kt, inst, oser, end, ka, oa, opened, open_rows)
    append_rows(os.path.join(args.dir, "snapshots.csv"), snaps)


def summary(args):
    rows = [r for r in load(os.path.join(args.dir, "closed_positions.csv")) if r.get("payout") not in ("", None)]
    if not rows: print("No settled positions yet."); return
    n = len(rows); tot = sum(float(r["total_profit"]) for r in rows)
    pairs = sum(float(r["pairs"]) for r in rows); cost = sum(float(r["pairs"]) * float(r["cost_per_pair"]) for r in rows)
    pay = [int(float(r["payout"])) for r in rows]
    print(f"Settled positions: {n}   pairs traded: {pairs:.0f}   cost: ${cost:.2f}")
    print(f"Payout 0 (both lost): {pay.count(0)/n:.1%}   payout 1: {pay.count(1)/n:.1%}   payout 2: {pay.count(2)/n:.1%}")
    print(f"Total paper profit: ${tot:.2f}   return on cost: {tot/cost:.1%}   avg slippage: {sum(float(r['slip_c']) for r in rows)/n:.2f}c")
    for a in sorted({r['asset'] for r in rows}):
        rr = [r for r in rows if r['asset'] == a]
        print(f"  {a}: {len(rr)} trades, profit ${sum(float(r['total_profit']) for r in rr):.2f}")


def main():
    global DEBUG
    ap = argparse.ArgumentParser()
    ap.add_argument("--loop", type=int, default=0, help="seconds between cycles (0 = once)")
    ap.add_argument("--dir", default="okx_kalshi_paper")
    ap.add_argument("--entry", type=float, default=0.80, help="open if marginal combined price < this")
    ap.add_argument("--pairs", type=float, default=50, help="target pairs (shares per leg) per paper trade")
    ap.add_argument("--min-pairs", type=float, default=5, help="minimum fillable pairs to open")
    ap.add_argument("--max-seconds", type=int, default=0, help="stop the loop after this many seconds (for GitHub Actions bursts)")
    ap.add_argument("--summary", action="store_true"); ap.add_argument("--debug", action="store_true")
    args = ap.parse_args(); DEBUG = args.debug; os.makedirs(args.dir, exist_ok=True)
    if args.summary: return summary(args)
    if not (KEY and SECRET and PASS): sys.exit("Set OKX_API_KEY / OKX_API_SECRET / OKX_API_PASSPHRASE")
    okx_ser = okx_series_15m(); print("OKX 15m series:", okx_ser)
    op, cp = os.path.join(args.dir, "open_positions.csv"), os.path.join(args.dir, "closed_positions.csv")
    t0 = time.time()
    while True:
        open_rows, closed_rows = load(op), load(cp)
        try:
            print(f"\n=== {utcnow():%H:%M:%S}Z  open={len(open_rows)} closed={len(closed_rows)} ===")
            cycle(args, okx_ser, open_rows, closed_rows)
            newly = []; open_rows = settle(open_rows, newly); closed_rows += newly
        except Exception as e:
            print("cycle error:", repr(e))
        save(op, open_rows); save(cp, closed_rows)
        if not args.loop: break
        if args.max_seconds and time.time() - t0 + args.loop > args.max_seconds: break
        time.sleep(args.loop)


if __name__ == "__main__":
    main()
