import json
from collections import defaultdict, Counter
rows = [json.loads(l) for l in open("verdict_log.jsonl", encoding="utf-8")]
print("toplam kayit:", len(rows))
print("tekil run_id:", len(set(r["run_id"] for r in rows)))
print("tekil urun:", len(set(r["product_id"] for r in rows)))

g = defaultdict(list)
for r in rows:
    g[r["product_id"]].append(r["final_verdict"])

print("\n--- VARYANS: birden fazla kez gorulen urunler ---")
tutarsiz = 0
for pid, vs in sorted(g.items()):
    if len(vs) > 1:
        c = Counter(vs)
        mark = "  <-- TUTARSIZ" if len(c) > 1 else ""
        if len(c) > 1:
            tutarsiz += 1
        print(f"{pid:42.42s} x{len(vs):<3} {dict(c)}{mark}")
print(f"\n{tutarsiz} urun koslar-arasi TUTARSIZ")

print("\n--- first_pass -> final SAPMASI ---")
sapma = Counter()
for r in rows:
    fp, fin, by = r.get("first_pass_verdict"), r.get("final_verdict"), r.get("recheck_triggered_by")
    if fp != fin:
        sapma[f"{fp} -> {fin} (by {by})"] += 1
for k, v in sapma.most_common():
    print(f"  {v}x  {k}")

print("\n--- final_verdict dagilimi ---")
print(dict(Counter(r["final_verdict"] for r in rows)))
