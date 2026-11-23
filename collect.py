#!/usr/bin/env python3
"""Hourly market-data collector (standard library only). Appends one row per source to data/*.csv.
Each source is wrapped separately: one failing feed never stops the others. Failures go to data/errors.csv."""
import csv, json, os, sys, urllib.request, urllib.parse, datetime as dt
import xml.etree.ElementTree as ET

NOW = dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")
OUT = os.path.join(os.path.dirname(os.path.abspath(__file__)), "data")
os.makedirs(OUT, exist_ok=True)

def get(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-data-collector/1.0"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return json.loads(r.read())

def append(name, header, row):
    path = os.path.join(OUT, name)
    new = not os.path.exists(path)
    with open(path, "a", newline="") as f:
        w = csv.writer(f)
        if new: w.writerow(header)
        w.writerow(row)

def err(source, e):
    append("errors.csv", ["time_utc", "source", "error"], [NOW, source, str(e)[:200]])

def prices():
    ids = "bitcoin,ethereum,solana,ripple"
    d = get(f"https://api.coingecko.com/api/v3/simple/price?ids={ids}&vs_currencies=usd&include_24hr_change=true&include_24hr_vol=true")
    row = [NOW]
    for c in ids.split(","):
        row += [d[c]["usd"], d[c].get("usd_24h_change"), d[c].get("usd_24h_vol")]
    h = ["time_utc"] + [f"{s}_{k}" for s in ("btc", "eth", "sol", "xrp") for k in ("usd", "chg24h_pct", "vol24h")]
    append("prices.csv", h, row)

def fear_greed():
    d = get("https://api.alternative.me/fng/?limit=1")["data"][0]
    append("fear_greed.csv", ["time_utc", "value", "label"], [NOW, d["value"], d["value_classification"]])

def stablecoins():
    d = get("https://stablecoins.llama.fi/stablecoincharts/all")
    last = d[-1]
    append("stablecoin_supply.csv", ["time_utc", "total_usd"], [NOW, last["totalCirculatingUSD"]["peggedUSD"]])

def okx_derivs():
    row = [NOW]; h = ["time_utc"]
    for s in ("BTC", "ETH", "SOL", "XRP"):
        inst = f"{s}-USDT-SWAP"
        f = get(f"https://www.okx.com/api/v5/public/funding-rate?instId={inst}")["data"][0]
        oi = get(f"https://www.okx.com/api/v5/public/open-interest?instType=SWAP&instId={inst}")["data"][0]
        row += [f["fundingRate"], oi["oiUsd"]]
        h += [f"{s.lower()}_funding_rate", f"{s.lower()}_open_interest_usd"]
    append("okx_derivatives.csv", h, row)

def deribit():
    out = [NOW]
    t = get("https://www.deribit.com/api/v2/public/ticker?instrument_name=BTC-PERPETUAL")["result"]
    out += [t.get("current_funding"), t.get("funding_8h"), t.get("open_interest")]
    opts = get("https://www.deribit.com/api/v2/public/get_book_summary_by_currency?currency=BTC&kind=option")["result"]
    put_oi = sum(o["open_interest"] for o in opts if o["instrument_name"].endswith("-P"))
    call_oi = sum(o["open_interest"] for o in opts if o["instrument_name"].endswith("-C"))
    ivs = [o["mark_iv"] for o in opts if o.get("mark_iv")]
    out += [put_oi, call_oi, round(put_oi / call_oi, 4) if call_oi else None,
            round(sum(ivs) / len(ivs), 2) if ivs else None]
    append("deribit_btc.csv",
           ["time_utc", "perp_funding_current", "perp_funding_8h", "perp_open_interest",
            "options_put_oi", "options_call_oi", "put_call_oi_ratio", "avg_mark_iv_pct"], out)

def fred():
    key = os.environ.get("FRED_API_KEY")
    if not key: return
    row = [NOW]
    for sid in ("DGS10", "DTWEXBGS", "VIXCLS"):
        d = get(f"https://api.stlouisfed.org/fred/series/observations?series_id={sid}&api_key={key}&file_type=json&sort_order=desc&limit=5")
        vals = [o for o in d["observations"] if o["value"] != "."]
        row += [vals[0]["value"] if vals else None, vals[0]["date"] if vals else None]
    append("fred_macro.csv", ["time_utc", "us10y_yield", "us10y_date", "dollar_index", "dollar_date", "vix", "vix_date"], row)

FEEDS = {
    "fed": "https://www.federalreserve.gov/feeds/press_all.xml",
    "sec": "https://www.sec.gov/news/pressreleases.rss",
    "coindesk": "https://www.coindesk.com/arc/outboundfeeds/rss/",
    "cointelegraph": "https://cointelegraph.com/rss",
}

def get_text(url, timeout=30):
    req = urllib.request.Request(url, headers={"User-Agent": "crypto-data-collector/1.0 (paper-trading research)"})
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()

def clean(t):
    return " ".join((t or "").split())[:300]

def news():
    """Append each NEW headline once (deduped by URL) to data/news.csv. Titles only; treat as untrusted text."""
    path = os.path.join(OUT, "news.csv")
    seen = set()
    if os.path.exists(path):
        with open(path, newline="") as f:
            seen = {r["url"] for r in csv.DictReader(f)}
    rows = []
    for src, url in FEEDS.items():
        try:
            root = ET.fromstring(get_text(url))
            for it in root.iter("item"):
                link = clean(it.findtext("link")); title = clean(it.findtext("title"))
                if link and title and link not in seen:
                    seen.add(link); rows.append([NOW, src, clean(it.findtext("pubDate")), title, link])
        except Exception as e:
            err("news_" + src, e)
    try:
        q = urllib.parse.quote('(bitcoin OR crypto OR "federal reserve" OR sanctions OR war OR tariff) sourcelang:english')
        d = json.loads(get_text(f"https://api.gdeltproject.org/api/v2/doc/doc?query={q}&mode=artlist&format=json&maxrecords=30&timespan=2h"))
        for a in d.get("articles", []):
            link = clean(a.get("url")); title = clean(a.get("title"))
            if link and title and link not in seen:
                seen.add(link); rows.append([NOW, "gdelt", clean(a.get("seendate")), title, link])
    except Exception as e:
        err("news_gdelt", e)
    if rows:
        new = not os.path.exists(path)
        with open(path, "a", newline="") as f:
            w = csv.writer(f)
            if new: w.writerow(["first_seen_utc", "source", "published", "title", "url"])
            w.writerows(rows)

POLY_QUERIES = ("Fed rate", "Bitcoin", "recession")

def polymarket():
    """Snapshot top active prediction-market odds (Polymarket public search) per query to data/polymarket.csv."""
    rows = []
    for q in POLY_QUERIES:
        d = get("https://gamma-api.polymarket.com/public-search?q=" + urllib.parse.quote(q) + "&limit_per_type=5&events_status=active")
        mk = []
        for ev in d.get("events") or []:
            for m in ev.get("markets") or []:
                if m.get("closed") or m.get("active") is False: continue
                try:
                    outs = m.get("outcomes"); prs = m.get("outcomePrices")
                    outs = json.loads(outs) if isinstance(outs, str) else outs
                    prs = json.loads(prs) if isinstance(prs, str) else prs
                    first = float(prs[0]); label = outs[0]
                except Exception:
                    continue
                vol = m.get("volumeNum")
                try: vol = float(vol if vol is not None else m.get("volume") or 0)
                except Exception: vol = 0.0
                mk.append((vol, clean(m.get("question")), label, first, clean(m.get("endDateIso") or m.get("endDate"))))
        mk.sort(reverse=True)
        for vol, qu, label, first, end in mk[:5]:
            rows.append([NOW, q, qu, label, first, vol, end])
    for r in rows:
        append("polymarket.csv", ["time_utc", "query", "question", "first_outcome", "first_outcome_price", "volume_usd", "end_date"], r)

# ---- event alerts (price / derivatives triggers; no trading) ----
ALERT_MOVE_1H = 3.0          # % move vs previous hourly row, any coin
ALERT_CHG24 = {"btc": 5.0, "eth": 7.0, "sol": 7.0, "xrp": 7.0}   # |24h change| %
ALERT_FUNDING = 0.0005       # |OKX BTC funding rate| (0.05% per 8h) = extreme crowding
ALERT_OI_DROP = 8.0          # % fall in BTC open interest vs previous row (liquidation proxy)
ALERT_COOLDOWN_H = 6         # same trigger not repeated within this many hours
ALERT_MAX_PER_DAY = 3        # alert batches per UTC day

def _rows(name):
    path = os.path.join(OUT, name)
    if not os.path.exists(path): return []
    with open(path, newline="") as f:
        return list(csv.DictReader(f))

def _t(s): return dt.datetime.strptime(s, "%Y-%m-%dT%H:%M:%SZ").replace(tzinfo=dt.timezone.utc)

def check_alerts():
    found = []                       # (key, detail)
    pr = _rows("prices.csv")
    if pr:
        cur = pr[-1]; prev = pr[-2] if len(pr) > 1 else None
        gap_ok = prev and 30 <= (_t(cur["time_utc"]) - _t(prev["time_utc"])).total_seconds() / 60 <= 150
        for c in ("btc", "eth", "sol", "xrp"):
            try:
                chg = float(cur[f"{c}_chg24h_pct"])
                if abs(chg) >= ALERT_CHG24[c]:
                    found.append((f"{c}_24h", f"{c.upper()} {chg:+.1f}% over 24h (price {float(cur[f'{c}_usd']):,.2f})"))
                if gap_ok:
                    m = (float(cur[f"{c}_usd"]) / float(prev[f"{c}_usd"]) - 1) * 100
                    if abs(m) >= ALERT_MOVE_1H:
                        found.append((f"{c}_1h", f"{c.upper()} {m:+.1f}% since the previous hourly reading"))
            except Exception:
                pass
    ok = _rows("okx_derivatives.csv")
    if ok:
        cur = ok[-1]; prev = ok[-2] if len(ok) > 1 else None
        try:
            fr = float(cur["btc_funding_rate"])
            if abs(fr) >= ALERT_FUNDING:
                found.append(("btc_funding", f"BTC funding rate {fr*100:+.3f}% per 8h (extreme {'long' if fr>0 else 'short'} crowding)"))
            if prev and 30 <= (_t(cur["time_utc"]) - _t(prev["time_utc"])).total_seconds() / 60 <= 150:
                oi = (float(cur["btc_open_interest_usd"]) / float(prev["btc_open_interest_usd"]) - 1) * 100
                if oi <= -ALERT_OI_DROP:
                    found.append(("btc_oi_drop", f"BTC open interest {oi:+.1f}% in about an hour (possible liquidation cascade)"))
        except Exception:
            pass
    if os.environ.get("FORCE_TEST_ALERT") == "1":
        found = [("test", "TEST ALERT: this is a test of the alert pipeline, not a market event")] + found
    if not found:
        return
    hist = _rows("alerts.csv")
    now = _t(NOW)
    fresh = []
    for key, detail in found:
        if key == "test":
            fresh.append((key, detail)); continue
        last = [h for h in hist if h["key"] == key]
        if last and (now - _t(last[-1]["time_utc"])).total_seconds() < ALERT_COOLDOWN_H * 3600:
            continue
        fresh.append((key, detail))
    if not fresh:
        return
    today = NOW[:10]
    batches_today = len({h["time_utc"] for h in hist if h["time_utc"].startswith(today)})
    if batches_today >= ALERT_MAX_PER_DAY and not any(k == "test" for k, _ in fresh):
        return
    for key, detail in fresh:
        append("alerts.csv", ["time_utc", "key", "detail"], [NOW, key, detail])
    body = "@JonHagwood market event flagged at " + NOW + " (UTC):\n\n" + "\n".join(f"- {d}" for _, d in fresh) + \
           "\n\nSay 'check dry run' in Claude to investigate. Paper trading only; this alert does not trade."
    with open(os.path.join(os.path.dirname(OUT), "alert_message.md"), "w") as f:
        f.write(body)

if __name__ == "__main__":
    for fn in (prices, fear_greed, stablecoins, okx_derivs, deribit, fred, news, polymarket):
        try: fn()
        except Exception as e: err(fn.__name__, e)
    try: check_alerts()
    except Exception as e: err("check_alerts", e)
