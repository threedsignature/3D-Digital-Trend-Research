#!/usr/bin/env python3
"""
Etsy 3D Dijital Ürün Trend Avcısı
---------------------------------
Yeni listelenen ama hızla favori/görüntülenme toplayan 3D dijital ürünleri
(STL, OBJ, FBX, Blender vb.) resmi Etsy Open API v3 ile bulur.

Kurulum:
    pip install requests
    export ETSY_API_KEY="keystring"          # Windows: set ETSY_API_KEY=...
    # Etsy hesabınız "keystring:shared_secret" formatı istiyorsa:
    # export ETSY_API_KEY="keystring:sharedsecret"

Kullanım:
    python etsy_trend_hunter.py                       # varsayılan: son 30 gün
    python etsy_trend_hunter.py --max-age 14 --top 40
    python etsy_trend_hunter.py -k "articulated dragon stl" -k "tabletop terrain stl"
    python etsy_trend_hunter.py --no-shops            # mağaza sorgularını atla

İPUCU: Scripti günde 2-4 kez çalıştırın (cron / Görev Zamanlayıcı). Her çalıştırma
bir "snapshot" kaydeder; sonraki çalıştırmalarda favori ARTIŞ HIZI hesaplanır
ve bu, en güçlü rağbet sinyalidir.

NOT: Etsy, ilan bazında satış adedini herkese açık vermez. Bu yüzden satışı
şu vekil sinyallerle tahmin ediyoruz: favori hızı, görüntülenme hızı,
favori/görüntülenme oranı ve mağazanın toplam satış/yaş oranı.
"""
import argparse
import csv
import math
import os
import sqlite3
import sys
import time
from datetime import datetime, timezone

import requests

API = "https://openapi.etsy.com/v3/application"
DB_PATH = "etsy_trends.db"

DEFAULT_KEYWORDS = [
    "stl file 3d print",
    "3d printable stl",
    "3d model stl",
    "stl digital download",
    "printable miniature stl",
    "tabletop terrain stl",
    "articulated stl",
    "3d print planter stl",
    "3d print organizer stl",
    "blender 3d model",
    "fbx 3d asset",
    "obj 3d model",
    "low poly 3d model",
    "3mf file",
    "cosplay 3d print file",
    "keychain stl",
]


# ----------------------------------------------------------------- API katmanı
def make_session():
    key = os.environ.get("ETSY_API_KEY")
    if not key:
        sys.exit("HATA: ETSY_API_KEY ortam değişkeni tanımlı değil.")
    s = requests.Session()
    s.headers.update({"x-api-key": key, "Accept": "application/json"})
    return s


def api_get(sess, path, params=None, retries=4):
    url = f"{API}{path}"
    for attempt in range(retries):
        r = sess.get(url, params=params, timeout=30)
        if r.status_code == 200:
            time.sleep(0.12)  # ~8 istek/sn, Etsy limiti 10 qps
            return r.json()
        if r.status_code in (429, 500, 502, 503):
            time.sleep(2 ** attempt)
            continue
        if r.status_code in (401, 403):
            sys.exit(f"Yetki hatası {r.status_code}: API anahtarını/onayını kontrol edin.\n{r.text[:300]}")
        print(f"  ! {r.status_code} {path}: {r.text[:150]}", file=sys.stderr)
        return None
    return None


def search_keyword(sess, kw, pages, cutoff_ts):
    """En yeni ilanlardan başlayıp cutoff'tan eskiye düşünce durur."""
    out = []
    for p in range(pages):
        data = api_get(sess, "/listings/active", {
            "keywords": kw,
            "limit": 100,
            "offset": p * 100,
            "sort_on": "created",
            "sort_order": "desc",
        })
        if not data or not data.get("results"):
            break
        batch = data["results"]
        out.extend(batch)
        oldest = min(l.get("original_creation_timestamp") or l.get("creation_timestamp", 0) for l in batch)
        if oldest < cutoff_ts:
            break
    return out


def get_shop(sess, shop_id):
    return api_get(sess, f"/shops/{shop_id}")


# ---------------------------------------------------------------- Veritabanı
def init_db():
    con = sqlite3.connect(DB_PATH)
    con.executescript("""
    CREATE TABLE IF NOT EXISTS listings(
        listing_id INTEGER PRIMARY KEY, title TEXT, url TEXT, shop_id INTEGER,
        price REAL, currency TEXT, created INTEGER, tags TEXT);
    CREATE TABLE IF NOT EXISTS snapshots(
        listing_id INTEGER, ts INTEGER, favorers INTEGER, views INTEGER);
    CREATE INDEX IF NOT EXISTS ix_snap ON snapshots(listing_id, ts);
    CREATE TABLE IF NOT EXISTS shops(
        shop_id INTEGER PRIMARY KEY, name TEXT, sold INTEGER, reviews INTEGER,
        created INTEGER, fetched INTEGER);
    """)
    return con


def previous_snapshot(con, listing_id, now, min_gap_sec=3600):
    row = con.execute(
        "SELECT ts, favorers, views FROM snapshots WHERE listing_id=? AND ts<=? "
        "ORDER BY ts DESC LIMIT 1", (listing_id, now - min_gap_sec)).fetchone()
    return row


# --------------------------------------------------------------------- Mantık
def is_digital_3d(l):
    if l.get("listing_type") != "download" and not l.get("is_digital"):
        return False
    text = (l.get("title", "") + " " + " ".join(l.get("tags") or [])).lower()
    hints = ("stl", "3d", "obj", "fbx", "blend", "3mf", "gcode", "printable", "print file")
    return any(h in text for h in hints)


def price_of(l):
    p = l.get("price") or {}
    try:
        return p["amount"] / p["divisor"], p.get("currency_code", "")
    except Exception:
        return 0.0, ""


def score_listing(l, prev, shop, now):
    created = l.get("original_creation_timestamp") or l.get("creation_timestamp") or now
    age_days = max((now - created) / 86400, 0.25)
    fav = l.get("num_favorers", 0) or 0
    views = l.get("views", 0) or 0

    fav_per_day = fav / age_days
    views_per_day = views / age_days
    fav_rate = fav / views if views else 0.0

    delta_fav_day = 0.0
    if prev:
        hours = max((now - prev[0]) / 3600, 1)
        delta_fav_day = max(fav - prev[1], 0) / hours * 24

    shop_boost = 0.0
    if shop:
        shop_age = max((now - (shop.get("created") or now)) / 86400, 7)
        shop_boost = (shop.get("sold") or 0) / shop_age  # satış/gün (mağaza geneli)

    score = (
        fav_per_day * 1.0
        + views_per_day * 0.05
        + fav_rate * 20          # yüksek favori/görüntülenme = güçlü talep
        + delta_fav_day * 2.0    # gerçek zamanlı ivme
        + shop_boost * 0.5
    )
    return {
        "age_days": round(age_days, 1),
        "fav_per_day": round(fav_per_day, 2),
        "views_per_day": round(views_per_day, 1),
        "fav_rate_%": round(fav_rate * 100, 1),
        "delta_fav_day": round(delta_fav_day, 2),
        "shop_sales_per_day": round(shop_boost, 2),
        "score": round(score, 2),
    }


# ----------------------------------------------------------------------- Ana
def main():
    ap = argparse.ArgumentParser(description="Etsy 3D dijital ürün trend avcısı")
    ap.add_argument("-k", "--keyword", action="append", help="Anahtar kelime (birden fazla verilebilir)")
    ap.add_argument("--max-age", type=int, default=30, help="En fazla kaç günlük ilanlar (vars: 30)")
    ap.add_argument("--pages", type=int, default=3, help="Anahtar kelime başına sayfa (x100 ilan)")
    ap.add_argument("--min-fav", type=int, default=3, help="Minimum favori sayısı")
    ap.add_argument("--top", type=int, default=30, help="Gösterilecek sonuç sayısı")
    ap.add_argument("--no-shops", action="store_true", help="Mağaza bilgisi sorgulama")
    ap.add_argument("--csv", default=None, help="CSV çıktı yolu")
    args = ap.parse_args()

    keywords = args.keyword or DEFAULT_KEYWORDS
    now = int(time.time())
    cutoff = now - args.max_age * 86400

    sess = make_session()
    con = init_db()

    # 1) Topla
    pool = {}
    for kw in keywords:
        print(f"→ '{kw}' aranıyor...")
        for l in search_keyword(sess, kw, args.pages, cutoff):
            created = l.get("original_creation_timestamp") or l.get("creation_timestamp", 0)
            if created < cutoff or not is_digital_3d(l):
                continue
            pool[l["listing_id"]] = l
    print(f"\n{len(pool)} yeni 3D dijital ilan bulundu.")

    # 2) Snapshot kaydet + ön eleme
    rows = []
    for lid, l in pool.items():
        price, cur = price_of(l)
        created = l.get("original_creation_timestamp") or l.get("creation_timestamp")
        con.execute("INSERT OR REPLACE INTO listings VALUES(?,?,?,?,?,?,?,?)", (
            lid, l.get("title"), l.get("url"), l.get("shop_id"), price, cur, created,
            ",".join(l.get("tags") or [])))
        prev = previous_snapshot(con, lid, now)
        con.execute("INSERT INTO snapshots VALUES(?,?,?,?)",
                    (lid, now, l.get("num_favorers", 0), l.get("views", 0)))
        if (l.get("num_favorers", 0) or 0) >= args.min_fav:
            rows.append((l, prev, price, cur))
    con.commit()

    # 3) İlk skor (mağaza bilgisi olmadan) → en iyi 60 için mağaza sorgula
    rows.sort(key=lambda r: score_listing(r[0], r[1], None, now)["score"], reverse=True)
    shop_cache = {}
    if not args.no_shops:
        print("Mağaza verileri çekiliyor...")
        for l, _, _, _ in rows[:60]:
            sid = l.get("shop_id")
            if sid in shop_cache:
                continue
            cached = con.execute("SELECT name,sold,reviews,created,fetched FROM shops WHERE shop_id=?",
                                 (sid,)).fetchone()
            if cached and now - cached[4] < 86400:
                shop_cache[sid] = {"name": cached[0], "sold": cached[1], "reviews": cached[2], "created": cached[3]}
                continue
            s = get_shop(sess, sid)
            if s:
                shop_cache[sid] = {
                    "name": s.get("shop_name"),
                    "sold": s.get("transaction_sold_count", 0),
                    "reviews": s.get("review_count", 0),
                    "created": s.get("create_date"),
                }
                con.execute("INSERT OR REPLACE INTO shops VALUES(?,?,?,?,?,?)", (
                    sid, shop_cache[sid]["name"], shop_cache[sid]["sold"],
                    shop_cache[sid]["reviews"], shop_cache[sid]["created"], now))
        con.commit()

    # 4) Nihai sıralama
    results = []
    for l, prev, price, cur in rows:
        shop = shop_cache.get(l.get("shop_id"))
        m = score_listing(l, prev, shop, now)
        results.append({
            "title": (l.get("title") or "")[:80],
            "price": f"{price:.2f} {cur}",
            "favorites": l.get("num_favorers", 0),
            "views": l.get("views", 0),
            **m,
            "shop": (shop or {}).get("name", ""),
            "shop_total_sales": (shop or {}).get("sold", ""),
            "url": (l.get("url") or "").split("?")[0],
        })
    results.sort(key=lambda r: r["score"], reverse=True)

    # 5) Yazdır
    print(f"\n{'='*100}\nEN ÇOK RAĞBET GÖREN YENİ 3D DİJİTAL ÜRÜNLER (son {args.max_age} gün)\n{'='*100}")
    for i, r in enumerate(results[:args.top], 1):
        print(f"\n#{i}  skor {r['score']}  |  {r['age_days']} gün önce  |  {r['price']}")
        print(f"    {r['title']}")
        print(f"    ❤ {r['favorites']} ({r['fav_per_day']}/gün)  👁 {r['views']} ({r['views_per_day']}/gün)  "
              f"fav/view %{r['fav_rate_%']}  ivme {r['delta_fav_day']}/gün")
        if r["shop"]:
            print(f"    mağaza: {r['shop']}  (toplam satış: {r['shop_total_sales']}, ~{r['shop_sales_per_day']}/gün)")
        print(f"    {r['url']}")

    # 6) CSV
    path = args.csv or f"trend_sonuclari_{datetime.now(timezone.utc):%Y%m%d_%H%M}.csv"
    if results:
        with open(path, "w", newline="", encoding="utf-8-sig") as f:
            w = csv.DictWriter(f, fieldnames=list(results[0].keys()))
            w.writeheader()
            w.writerows(results)
        print(f"\nCSV kaydedildi: {path}")


if __name__ == "__main__":
    main()
