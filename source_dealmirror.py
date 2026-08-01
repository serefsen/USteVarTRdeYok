"""
source_dealmirror.py — DealMirror LTD adapter (US->TR motoru icin).

Uretilen urun dict'i (fetch_appsumo ile BIREBIR ayni sekil):
    {"pid": "dealmirror-<slug>", "name": "...", "tagline": "...",
     "desc": "...", "site": "https://dealmirror.com/product/<slug>/", "paid": True}

Lokal test (Seref'in makinesi, venv aktif):
    pip install beautifulsoup4
    python source_dealmirror.py --limit 5

run() entegrasyonu (havuz toplama dongusu):
    from source_dealmirror import fetch_dealmirror
    seen = set(load_seen())                       # zaten varsa
    pool = fetch_appsumo(...) + fetch_dealmirror(max_products=60, skip_pids=seen)
    # ardindan mevcut pid-tekillestirme mantigi calisir

Notlar:
- pid = "dealmirror-<slug>"  -> kaynaklar arasi cakisma olmaz.
- Kategori "en yeni basta" sirali; max_products kucuk tutulup skip_pids verilirse
  her gun sadece YENI urunlerin sayfasi cekilir (ucuz artimli hasat).
- "site" simdilik DealMirror urun URL'i. Gercek dis site cozumu (enrichment) v2.
- Kategori filtresi (VPN/hosting vb. eleme) ADAPTER'IN ISI DEGIL -> gate_moat karar verir.
- DIKKAT: bu dosya sandbox'tan canli test EDILEMEDI (ag kisiti dealmirror.com'a izin
  vermiyor). Ilk lokal kosu gercek dogrulama; desc kalitesi zayif gelirse bir urun
  sayfasinin HTML'ini yapistir, secicileri birebir ayarlarim.
"""

import re
import json
import time
import argparse
import requests
from bs4 import BeautifulSoup

BASE = "https://dealmirror.com"
CAT = BASE + "/product-category/software/"
HEADERS = {
    "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/121 Safari/537.36"),
    "Accept-Language": "en-US,en;q=0.9",
}
TIMEOUT = 25
SLEEP = 0.6  # nazik ol, rate-limit yeme
_PROD = re.compile(r'/product/([^/?#"]+)')


def _norm(s):
    return re.sub(r"\s+", " ", (s or "")).strip()


def _split_name(full):
    """'Meget: All-in-One Video Downloader' -> ('Meget', 'All-in-One Video Downloader')"""
    full = _norm(full)
    for sep in (":", "—", "–", "|", " - "):
        if sep in full:
            head, tail = full.split(sep, 1)
            head, tail = head.strip(), tail.strip()
            if 1 <= len(head) <= 45 and tail:
                return head, tail
    return full, ""


def _collect_cards(max_products, verbose):
    """Kategori sayfalarindan (ucuz) slug + baslik topla. Resim linki bos metinli olur,
    H2 baslik linki dolu -> her slug icin EN UZUN metni baslik kabul et."""
    titles, order, page = {}, [], 1
    while True:
        url = CAT if page == 1 else f"{CAT}page/{page}/"
        try:
            r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        except Exception as e:
            if verbose:
                print(f"[cat] s.{page} hata: {e}")
            break
        if r.status_code != 200:
            if verbose:
                print(f"[cat] s.{page} HTTP {r.status_code} — son")
            break
        soup = BeautifulSoup(r.text, "html.parser")
        before = len(order)
        for a in soup.find_all("a", href=True):
            m = _PROD.search(a["href"])
            if not m:
                continue
            slug = m.group(1)
            txt = a.get_text(" ", strip=True)
            if slug not in titles:
                titles[slug] = txt
                order.append(slug)
            elif len(txt) > len(titles[slug]):
                titles[slug] = txt
        new = len(order) - before
        if verbose:
            print(f"[cat] s.{page}: +{new} urun (toplam {len(order)})")
        if new == 0:
            break
        if max_products and len(order) >= max_products:
            break
        page += 1
        time.sleep(SLEEP)
    slugs = order[:max_products] if max_products else order
    return [{"slug": s, "url": f"{BASE}/product/{s}/", "title": titles[s]} for s in slugs]


def _jsonld_desc(soup):
    """JSON-LD Product/Service schema'sindan temiz aciklama (varsa en guvenilir)."""
    for tag in soup.find_all("script", type="application/ld+json"):
        try:
            data = json.loads(tag.string or "")
        except Exception:
            continue
        objs = data if isinstance(data, list) else [data]
        for o in list(objs):
            if isinstance(o, dict) and isinstance(o.get("@graph"), list):
                objs += o["@graph"]
        for o in objs:
            if isinstance(o, dict) and o.get("@type") in ("Product", "Service") and o.get("description"):
                return _norm(o["description"])
    return ""


def _product_detail(url, verbose):
    """Urun sayfasindan baslik (yedek) + en zengin aciklama."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=TIMEOUT)
        if r.status_code != 200:
            return "", ""
    except Exception as e:
        if verbose:
            print(f"  urun hata: {e}")
        return "", ""
    soup = BeautifulSoup(r.text, "html.parser")

    h1 = soup.find("h1")
    h1t = h1.get_text(" ", strip=True) if h1 else ""

    cands = []
    jd = _jsonld_desc(soup)
    if jd:
        cands.append(jd)
    for n in soup.find_all("div", id=re.compile("tab-description", re.I)):
        cands.append(_norm(n.get_text(" ", strip=True)))
    for n in soup.find_all("div", class_=re.compile(r"(woocommerce-Tabs-panel|product-details|wc-tab|et_pb_wc_description)", re.I)):
        cands.append(_norm(n.get_text(" ", strip=True)))
    meta = (soup.find("meta", attrs={"name": "description"}) or
            soup.find("meta", attrs={"property": "og:description"}))
    if meta and meta.get("content"):
        cands.append(_norm(meta["content"]))

    desc = max(cands, key=len) if cands else ""
    return h1t, desc


def fetch_dealmirror(max_products=None, skip_pids=None, verbose=True):
    """DealMirror yazilim LTD'lerini motor sozlesmesi dict listesi olarak dondurur."""
    skip_pids = skip_pids or set()
    cards = _collect_cards(max_products, verbose)
    if verbose:
        print(f"[dealmirror] {len(cards)} aday kart; yeni urunlerin sayfasi cekiliyor...")
    out = []
    for i, c in enumerate(cards, 1):
        pid = f"dealmirror-{c['slug']}"
        if pid in skip_pids:
            continue  # zaten gorulmus -> sayfasini cekme (ucuz)
        h1t, desc = _product_detail(c["url"], verbose)
        name, tagline = _split_name(c["title"] or h1t)
        if not name:
            continue
        if len(desc) < 40:  # kalite > hacim: cop desc'i atla
            if verbose:
                print(f"  {c['slug']}: zayif/eksik desc — atlandi")
            continue
        out.append({
            "pid": pid,
            "name": name,
            "tagline": tagline,
            "desc": desc,
            "site": c["url"],
            "paid": True,
        })
        if verbose and i % 10 == 0:
            print(f"[dealmirror] ilerleme {i}/{len(cards)} (alinan {len(out)})")
        time.sleep(SLEEP)
    if verbose:
        print(f"[dealmirror] tamam — {len(out)} urun")
    return out


if __name__ == "__main__":
    ap = argparse.ArgumentParser(description="DealMirror adapter testi")
    ap.add_argument("--limit", type=int, default=5, help="kac urun cekilsin (test)")
    ap.add_argument("--quiet", action="store_true")
    a = ap.parse_args()
    items = fetch_dealmirror(max_products=a.limit, verbose=not a.quiet)
    print(f"\n=== {len(items)} URUN ===\n")
    for d in items:
        print(f"pid : {d['pid']}")
        print(f"ad  : {d['name']}")
        print(f"alt : {d['tagline']}")
        print(f"site: {d['site']}")
        print(f"paid: {d['paid']}")
        desc = d["desc"]
        print(f"desc: {desc[:300]}{'...' if len(desc) > 300 else ''}")
        print("-" * 64)
