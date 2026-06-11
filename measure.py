#!/usr/bin/env python3
"""US->TR Fırsat Motoru — ÖLÇÜM HARNESS (ground-truth).

Aynı ürünleri N kez koşturur ve SENİN etiketinle (ground_truth.json) karşılaştırır.
Raporlar:
  - DOĞRU          : motor kararı = senin etiketin
  - YANLIŞ-PASS     : motor PASS dedi, sen DROP dedin  (EN TEHLİKELİ — boşa klon)
  - yanlış-DROP     : motor DROP dedi, sen PASS dedin  (ölen fırsat)
  - punt (İNCELE)   : motor karar veremedi (insana attı)
  - TUTARSIZ        : N koşuda aynı ürün farklı karar verdi (varyans)

Notion'a YAZMAZ. Gerekli env: OPENROUTER_API_KEY, SERPER_API_KEY.
Kullanım:  python measure.py            (N=3)
           python measure.py --runs 5
"""
import sys, json, time, argparse
from collections import defaultdict, Counter

import us_tr_motor as eng

GT_FILE = "ground_truth.json"
POOL_CACHE = "measure_pool.json"


def build_pool(refresh=False):
    """AppSumo havuzu — CACHE'Lİ. İlk koşuda çeker ve measure_pool.json'a dondurur; sonraki koşular
    cache'ten okur (hız + tekrarlanabilirlik). Etiketli deal AppSumo'dan düşse bile cache'te kalır ->
    benchmark erimez. Tazelemek için: --refresh-pool (eski cache'le BİRLEŞTİRİR, kaybetmez)."""
    if not refresh:
        try:
            pool = json.load(open(POOL_CACHE, encoding="utf-8"))
            if pool:
                print(f"Havuz cache'ten: {len(pool)} ürün ({POOL_CACHE}; tazelemek için --refresh-pool)")
                return pool
        except Exception:
            pass
    pool = {}
    for q in eng.APPSUMO_QUERIES:
        print(f"  appsumo çekiliyor: {q} ...", flush=True)
        for d in eng.fetch_appsumo(q):
            if d.get("pid") and d["pid"] not in pool:
                pool[d["pid"]] = d
        time.sleep(0.5)
    try:  # eski cache'le birleştir: AppSumo'dan düşen etiketli ürünler kaybolmasın
        old = json.load(open(POOL_CACHE, encoding="utf-8"))
        for pid, d in old.items():
            pool.setdefault(pid, d)
    except Exception:
        pass
    json.dump(pool, open(POOL_CACHE, "w", encoding="utf-8"), ensure_ascii=False, indent=1)
    print(f"Havuz kaydedildi: {len(pool)} ürün -> {POOL_CACHE}")
    return pool


def engine_status(p):
    """Motorun tek koşudaki STATUS kararı (Notion'suz): moat -> gap -> decide_status."""
    vectors, _ = eng.gate_moat(p)
    if not vectors:
        return "DROP"
    gap, _, _ = eng.gate_tr_gap(p)
    status, _ = eng.decide_status(p["pid"], vectors, gap, True)
    return status


def load_labels():
    try:
        gt = json.load(open(GT_FILE, encoding="utf-8"))
    except Exception as e:
        sys.exit(f"{GT_FILE} okunamadı ({e}). Önce etiketle.")
    items = gt["items"] if isinstance(gt, dict) and "items" in gt else gt
    labels = {}
    for r in items:
        h = str(r.get("human", "")).strip().upper()
        if h in ("PASS", "DROP"):
            labels[r["pid"]] = h
    return labels


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--runs", type=int, default=3, help="her ürün kaç kez koşulsun (varsayılan 3)")
    ap.add_argument("--refresh-pool", action="store_true",
                    help="AppSumo havuzunu yeniden çek (cache'le birleştirir)")
    args = ap.parse_args()
    N = max(1, args.runs)

    for k, v in [("OPENROUTER_API_KEY", eng.OPENROUTER_KEY), ("SERPER_API_KEY", eng.SERPER_KEY)]:
        if not v:
            sys.exit(f"Eksik env: {k}")

    labels = load_labels()
    if not labels:
        sys.exit(f"{GT_FILE} içinde PASS/DROP ile etiketli ürün yok. 'human' alanını doldur.")

    print(f"Etiketli ürün: {len(labels)} | koşu sayısı N={N}")
    pool = build_pool(refresh=args.refresh_pool)
    missing = [pid for pid in labels if pid not in pool]
    if missing:
        print(f"UYARI: AppSumo'da bulunamadı (deal bitmiş olabilir), atlanıyor: {missing}")
    pids = [pid for pid in labels if pid in pool]
    if not pids:
        sys.exit("Etiketli ürünlerin hiçbiri AppSumo havuzunda yok.")

    # N koşu — her koşu ÖNCESİ kanonik profil cache TEMİZLENİR ki koşular-arası varyans gerçek görünsün.
    runs = defaultdict(list)
    for i in range(1, N + 1):
        eng._TR_PROFILE_CACHE.clear()
        print(f"\n=== KOŞU {i}/{N} ===")
        for pid in pids:
            st = engine_status(pool[pid])
            runs[pid].append(st)
            print(f"  {pid:42.42s} {st}")

    # ---------- RAPOR ----------
    print("\n" + "=" * 74)
    print(f"{'ÜRÜN':40s} {'İNSAN':5s} {'KOŞULAR':22s} {'TUT.':5s} SONUÇ")
    print("-" * 74)
    correct = false_pass = false_drop = punt = inconsistent = 0
    for pid in pids:
        human, rs = labels[pid], runs[pid]
        maj = Counter(rs).most_common(1)[0][0]
        consistent = len(set(rs)) == 1
        inconsistent += 0 if consistent else 1
        if maj == human:
            tag, correct = "DOĞRU", correct + 1
        elif maj == "PASS" and human == "DROP":
            tag, false_pass = "!! YANLIŞ-PASS", false_pass + 1
        elif maj == "DROP" and human == "PASS":
            tag, false_drop = "yanlış-DROP", false_drop + 1
        else:
            tag, punt = f"punt (insan:{human})", punt + 1
        print(f"{pid:40.40s} {human:5s} {'/'.join(rs):22.22s} {'evet' if consistent else 'HAYIR':5s} {tag}")

    n = len(pids)
    pct = lambda x: f"{100 * x // n}%" if n else "0%"
    print("-" * 74)
    print(f"Toplam etiketli & havuzda: {n}")
    print(f"  DOĞRU             : {correct}/{n} ({pct(correct)})")
    print(f"  YANLIŞ-PASS       : {false_pass}   <- en tehlikeli (boşa klon)")
    print(f"  yanlış-DROP       : {false_drop}   <- ölen fırsat")
    print(f"  punt (İNCELE)     : {punt}   <- karar veremedi")
    print(f"  TUTARSIZ (N koşu) : {inconsistent}/{n}   <- varyans")

    out = {"runs": N, "results": {pid: {"human": labels[pid], "runs": runs[pid]} for pid in pids},
           "summary": {"n": n, "correct": correct, "false_pass": false_pass,
                       "false_drop": false_drop, "punt": punt, "inconsistent": inconsistent}}
    json.dump(out, open("measure_results.json", "w", encoding="utf-8"), ensure_ascii=False, indent=2)
    print("\nDetay -> measure_results.json")


if __name__ == "__main__":
    main()
