#!/usr/bin/env python3
"""İkinci aşama RECALL sınavı — measure'ın 3 yanlış-PASS ürününü Gemini'den geçirir.
Cevap anahtarı bilinen set: ikinci aşama bunları yakalıyor mu, ölçüyoruz (devir #1).
Gerekli env: OPENROUTER_API_KEY, SERPER_API_KEY. Kullanım: python stage2_test.py
"""
import json
import gemini_check as gc

# pid -> bildiğimiz gerçek TR rakipleri (cevap anahtarı)
EXPECTED = {
    "ai-smarttalk": ["supsis", "mavibot"],
    "personeoai":   ["enocta", "evre", "synaps"],
    "motionvid":    [],  # bilinen rakip yok ([U] etiket); Gemini ne bulacak, merak konusu
}

pool = json.load(open("measure_pool.json", encoding="utf-8"))
report = {}
for pid, expected in EXPECTED.items():
    p = pool.get(pid)
    if not p:
        print(f"!! {pid} havuzda yok, atlandı"); continue
    prod = {"ad": p["name"], "tagline": p.get("tagline", ""), "desc": p.get("desc", "")}
    print("\n" + "=" * 64 + f"\nÜRÜN: {pid} ({p['name']})")
    material, nhits = gc.gather(prod)
    print(f"-> {nhits} sonuç, {len(material)} karakter malzeme; Gemini'ye soruluyor...")
    res = gc.verdict(prod, material) or {}
    found = [str(r.get("ad", "")) + " " + str(r.get("domain", "")) for r in res.get("rakipler", [])]
    found_l = " | ".join(found).lower()
    caught = [e for e in expected if e in found_l]
    missed = [e for e in expected if e not in found_l]
    print(f"VAR MI : {res.get('var_mi')}")
    for f in found:
        print(f"  bulundu: {f}")
    print(f"SINAV  : yakalanan={caught or '-'}  KAÇAN={missed or '-'}")
    report[pid] = {"var_mi": res.get("var_mi"), "bulunan": found,
                   "yakalanan": caught, "kacan": missed,
                   "kanit": res.get("en_guclu_kanit"), "gerekce": res.get("gerekce")}

json.dump(report, open("stage2_test_result.json", "w", encoding="utf-8"),
          ensure_ascii=False, indent=2)
print("\nDetay -> stage2_test_result.json")
