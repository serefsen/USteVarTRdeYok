#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
AppSumo YOKLAMA — tek istek atar, ne döndüğünü raporlar.
Çalıştır:  python appsumo_yoklama.py
Sonra TÜM çıktıyı bana yapıştır.
"""
import requests, re, json

URL = "https://appsumo.com/search/?query=ai"
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                  "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "tr,en-US;q=0.9,en;q=0.8",
}

print("İstek:", URL)
try:
    r = requests.get(URL, headers=UA, timeout=30)
except Exception as e:
    raise SystemExit(f"İSTEK HATASI: {e}")

print("STATUS  :", r.status_code)
print("UZUNLUK :", len(r.text), "karakter")

low = r.text.lower()
engel = [s for s in ["just a moment", "cloudflare", "captcha", "access denied",
                     "are you human", "verify you are", "enable javascript"] if s in low]
print("ENGEL?  :", engel or "yok gibi")

# AppSumo Next.js mi? __NEXT_DATA__ içinde veri var mı?
m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
print("\n__NEXT_DATA__:", "VAR" if m else "YOK")
if m:
    try:
        data = json.loads(m.group(1))
        pp = data.get("props", {}).get("pageProps", {})
        print("pageProps anahtarları:", list(pp.keys())[:40])
        sample = json.dumps(pp, ensure_ascii=False)
        print("\n--- pageProps İLK 2500 KARAKTER ---")
        print(sample[:2500])
    except Exception as e:
        print("parse hatası:", e)
        print("ilk 600:", m.group(1)[:600])
else:
    # HTML'de ürün linki var mı?
    print("appsumo.com/products/ link sayısı:", len(re.findall(r"appsumo\.com/products/", r.text)))
    print("\n--- SAYFA İLK 800 KARAKTER ---")
    print(r.text[:800])
