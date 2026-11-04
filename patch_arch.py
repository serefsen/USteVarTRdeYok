import io, sys, ast

PATH = "us_tr_motor.py"
BAK = "us_tr_motor.py.bak_arch"
src = io.open(PATH, encoding="utf-8").read()
io.open(BAK, "w", encoding="utf-8").write(src)
orig = src
lines = src.split("\n")

def find_idx(substr, start=0):
    for k in range(start, len(lines)):
        if substr in lines[k]:
            return k
    return -1

i = find_idx("status, reason = decide_status(pid, vectors, gap, paid)")
j = find_idx("notion_write(db_id, pid, p, vectors, gap, paid, status, gerekce, competitors)", (i + 1) if i >= 0 else 0)
if i < 0 or j < 0 or j < i:
    sys.exit(f"HATA: verdikt blogu yok (i={i} j={j}) - dosya degismedi")

new_block = r'''        # YENI MIMARI: Gemini = FILTRE. VAR->DROP / YOK->PASS / BELIRSIZ->etiket. Notion'a PASS+BELIRSIZ.
        ss_res = {}
        if gap == "NO_GAP":
            status, reason = "DROP", "TR'de ayni isi ayni segmente yapan mustakil yerli oyuncu var (dogrulandi)"
        elif gap == "STRUCTURAL_GAP":
            print("  [motor bulamadi -> Gemini derin kontrol...]")
            try:
                ss_res = second_stage_verdict(p, second_stage_gather(p)) or {}
            except Exception as e:
                ss_res = {"var_mi": "HATA", "gerekce": str(e)}
            var_mi = str(ss_res.get("var_mi", "BELIRSIZ")).upper().strip()
            print("  -- Gemini --------------------------------")
            print(f"  VAR MI  : {var_mi}")
            for r in ss_res.get("rakipler", []):
                print(f"  Rakip   : {r.get('ad', '')} | {r.get('domain', '')}")
            print(f"  Kanit   : {ss_res.get('en_guclu_kanit', '?')}")
            print(f"  Gerekce : {ss_res.get('gerekce', '?')}")
            print("  ------------------------------------------")
            if var_mi == "VAR":
                status, reason = "DROP", "Gemini TR muadili DOGRULADI -> klonlanmaz"
            elif var_mi == "YOK":
                status, reason = "PASS", "Gemini derin aramada TR muadili BULAMADI -> aday (klonlamadan once dogrula)"
            else:
                status, reason = "BELIRSIZ", f"Gemini netlestiremedi ({var_mi}) -> insan"
        else:
            status, reason = "BELIRSIZ", f"TR oyuncu var/islev belirsiz -> insan | {gap_reason}"

        counts[status] = counts.get(status, 0) + 1
        print(f"  -> {status}: {reason}")

        if status in ("PASS", "BELIRSIZ"):
            gerekce = f"{status} -- duvar: {', '.join(vectors)}. {reason}"
            if ss_res:
                gerekce += (f"\nGEMINI -> {ss_res.get('var_mi', '?')}: "
                            f"{str(ss_res.get('gerekce', '') or ss_res.get('en_guclu_kanit', ''))[:600]}")
            competitors = [{"ad": r.get("ad", ""), "domain": r.get("domain", "")}
                           for r in ss_res.get("rakipler", [])]
            for d in _domains_in(gap_reason):
                competitors.append({"ad": "", "domain": d})
            notion_write(db_id, pid, p, vectors, gap, paid, status, gerekce, competitors)'''

lines = lines[:i] + new_block.split("\n") + lines[j + 1:]

for idx in range(len(lines)):
    if 'counts = {"PASS": 0,' in lines[idx]:
        lines[idx] = '    counts = {"PASS": 0, "BELIRSIZ": 0, "DROP": 0}'
        break
for idx in range(len(lines)):
    if "{processed} | PASS" in lines[idx]:
        lines[idx] = "    print(f\"\\n=== OZET: islenen {processed} | PASS {counts.get('PASS',0)} | BELIRSIZ {counts.get('BELIRSIZ',0)} | DROP {counts.get('DROP',0)}\")"
        if idx + 1 < len(lines) and "DROP {counts[" in lines[idx + 1]:
            del lines[idx + 1]
        break
for idx in range(len(lines)):
    if 'if processed >= 10 and counts["PASS"] == 0' in lines[idx]:
        lines[idx] = '    if processed >= 10 and counts.get("PASS",0) == 0 and counts.get("BELIRSIZ",0) == 0:'
        break

new_src = "\n".join(lines)
try:
    ast.parse(new_src)
except SyntaxError as e:
    io.open(PATH, "w", encoding="utf-8").write(orig)
    sys.exit(f"HATA: yamali kod parse OLMADI ({e}) - GERI ALINDI")
io.open(PATH, "w", encoding="utf-8").write(new_src)
print("YAMA UYGULANDI. Yedek:", BAK)
print("Yeni mantik: Gemini VAR->DROP / YOK->PASS / BELIRSIZ->etiket; Notion'a sadece PASS+BELIRSIZ.")
