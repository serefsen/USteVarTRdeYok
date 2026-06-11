#!/usr/bin/env python3
"""TR'de muadili VAR mı YOK mu? — tek ürün, BOL malzeme + Gemini 3.5 kararı.

v2 — recall revizyonu (stage2_test sınavında bilinen 5 rakipten 0 yakalanınca):
1) SORGU ÇEŞİTLİLİĞİ: "yapay zeka X" tek-kalıbı yasak; pazarın jargonu (canlı destek,
   chatbot...), İngilizce pazar terimleri zorunlu; "yerli/türk" en fazla 1 sorguda
   (gl=tr zaten TR getiriyor; kelime SERP'i listicle'a büküyor).
2) ADAY HASADI + HEDEFLİ DOĞRULAMA: snippet'lerde ADI GEÇEN yerli ürün adayları
   çıkarılır (listicle içindekiler dahil), her adaya kendi adıyla hedefli arama +
   ana sayfa metni. Piyango -> sistematik tarama.
3) Malzeme bütçesi 18k -> 30k; llm_json artık 1 kez yeniden dener.

Gerekli env: OPENROUTER_API_KEY, SERPER_API_KEY
Kullanım:  python gemini_check.py                         (varsayılan: personeo testi)
           python gemini_check.py --ad "X" --tagline "..." --desc "..."
"""
import os, sys, json, argparse, re
import requests

OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
SERPER_KEY     = os.getenv("SERPER_API_KEY", "")
MODEL = "google/gemini-3.5-flash"
MATERIAL_CAP = 30000

# --- bilinen test vakası: personeo (cevabı biliyoruz -> Enocta Personas = VAR) ---
DEFAULT = {
    "ad": "personeo.ai",
    "tagline": "AI roleplay platform — practice real-life conversations with AI personas",
    "desc": ("Roleplay with AI personas in realistic, real-time VOICE scenarios to sharpen "
             "communication soft skills: angry customer, skeptical buyer, interviewer, manager "
             "coaching an employee. Pick a scenario and persona, talk to the AI by voice, get "
             "personalized feedback and a performance score. Scenario library, progress tracking, "
             "create your own scenarios."),
}


def openrouter(messages, model=MODEL, max_tokens=2000, temperature=0):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": model, "messages": messages, "max_tokens": max_tokens, "temperature": temperature},
        timeout=180,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def llm_json(prompt, max_tokens=4000):
    """JSON iste. Gemini Flash düşünen model: dar bütçede düşünme tokenleri çıktıyı yer ->
    boş/yarım JSON. O yüzden bütçe geniş; parse hatasında 2. deneme bütçeyi x2 yapar ve
    ham çıktının başı basılır (teşhis kör kalmasın)."""
    for attempt in (1, 2):
        txt = ""
        try:
            q = prompt if attempt == 1 else (
                prompt + "\n\nÖNEMLİ: Önceki yanıt geçersiz/yarım JSON'du. SADECE geçerli, eksiksiz JSON döndür.")
            txt = openrouter([{"role": "user", "content": q}], MODEL, max_tokens * attempt)
            clean = re.sub(r"```json|```", "", txt).strip()
            m = re.search(r"[\{\[].*[\}\]]", clean, re.S)
            return json.loads(m.group(0) if m else clean)
        except Exception as e:
            print(f"  ! llm hata (deneme {attempt}/2): {e} | ham[:120]={txt[:120]!r}")
    return None


def serper(query, n=8):
    try:
        r = requests.post("https://google.serper.dev/search",
                          headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
                          json={"q": query, "gl": "tr", "hl": "tr", "num": n}, timeout=30)
        r.raise_for_status()
        return r.json().get("organic", [])
    except Exception as e:
        print(f"  ! arama hatası ({query}): {e}")
        return []


def _dom(url):
    return re.sub(r"^https?://(www\.)?", "", (url or "").lower()).split("/")[0]


def gen_queries(p):
    """ÇEŞİTLİ sorgular + çekirdek pazar terimi. Tek-kalıp ('yapay zeka ...') yasak."""
    prompt = f"""Bir ABD ürününün Türkiye'de yerli muadili var mı diye ARAYACAĞIM.
Ürün: {p['ad']} — {p['tagline']}
Açıklama: {p['desc']}

Türkçe web'de yerel rakipleri bulmak için 9 FARKLI arama sorgusu üret.
KURALLAR (hepsi ZORUNLU):
- "yapay zeka" ifadesi EN FAZLA 2 sorguda geçsin. Kalanlar pazarın KENDİ jargonuyla olsun
  (örn. bu kategori neyse onun terimi: "canlı destek", "chatbot", "video oluşturucu",
  "satış eğitimi simülasyonu" gibi — ürüne göre sen bul).
- "yerli", "türk", "türkiye", "türkçe" kelimeleri EN FAZLA 1 sorguda geçsin
  (arama zaten Türkiye'ye kilitli; bu kelimeler sonucu blog listelerine büküyor).
- 2 sorgu İngilizce pazar terimi içersin (TR pazarı İngilizce alıntı kullanır: "role play",
  "AI chatbot", "text to video" gibi).
- Hem dar (tam işlev) hem geniş (kategori/kullanım senaryosu) sorgular olsun.
- MARKA ADI KULLANMA. Tekrarlama.
Ayrıca ürünün çekirdek işini pazarın diliyle 2-4 kelimeyle ver ("cekirdek").

SADECE JSON: {{"cekirdek":"...","sorgular":["s1","s2",...]}}"""
    j = llm_json(prompt, 4000) or {}
    qs = [q for q in (j.get("sorgular") or []) if isinstance(q, str) and q.strip()][:9]
    core = (j.get("cekirdek") or "").strip() or (p.get("tagline") or p["ad"])[:40]
    return (qs or [f"{p['ad']} alternatif"]), core


def harvest_candidates(hits, p):
    """Snippet'lerde ADI GEÇEN olası yerli ürün/şirketleri çıkar — listicle içindekiler DAHİL.
    Recall'ın kilidi: 'en iyi 10 chatbot' sayfası rakibin adını taşır ama domain'i taşımaz."""
    snip = "\n".join(f"- {h.get('title','')} | {h.get('snippet','')} [{h.get('link','')}]"
                     for h in hits[:80])
    prompt = f"""ABD ürünü: {p['ad']} — {p['tagline']}
Aşağıda Türkçe arama sonuçları (snippet) var. Bu metinlerde ADI GEÇEN ve Türk/yerli
OLABİLECEK ürün/şirket adaylarını çıkar:
- Liste/blog sayfalarının İÇİNDE anılan adlar da DAHİL (sayfanın kendisi değil, içindeki adlar).
- Global devleri alma (Intercom, Zendesk, Canva, Synthesia gibi).
- Emin olmadıkların da girsin; eleme sonra yapılacak.
- domain'i snippet'ten biliyorsan yaz, bilmiyorsan boş bırak.
SADECE JSON dizi (en fazla 10): [{{"ad":"","domain":""}}]

SONUÇLAR:
{snip}"""
    out = llm_json(prompt, 4000)
    if not isinstance(out, list):
        return []
    return [c for c in out if isinstance(c, dict) and (c.get("ad") or "").strip()][:10]


def fetch_text(url, limit=2200):
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"})
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", r.text, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:limit]
    except Exception:
        return ""


def verify_candidates(cands, core):
    """Her adaya HEDEFLİ arama (adıyla) + bulunabilirse ana sayfa metni."""
    lines, pages, doms = [], [], set()
    for c in cands[:8]:
        ad = c["ad"].strip()
        hs = serper(f"{ad} {core}", 4)
        for h in hs:
            lines.append(f"- [aday: {ad}] {h.get('title','')} | {h.get('snippet','')} [{h.get('link','')}]")
        dom = _dom(c.get("domain") or "")
        if not dom:  # hedefli aramadan adayın kendi sitesini yakala
            key = re.sub(r"[^a-z0-9]", "", ad.lower())[:6]
            for h in hs:
                d = _dom(h.get("link"))
                if key and key in re.sub(r"[^a-z0-9]", "", d):
                    dom = d
                    break
        if dom and dom not in doms:
            doms.add(dom)
            txt = fetch_text(f"https://{dom}")
            if txt:
                pages.append(f"### {dom} (aday: {ad})\n{txt}")
    return lines, pages, doms


def gather(p):
    queries, core = gen_queries(p)
    print("Üretilen sorgular:")
    for q in queries:
        print("  -", q)
    print("Çekirdek terim:", core)

    seen, hits = set(), []
    for q in queries:
        for h in serper(q):
            u = h.get("link", "")
            if u and u not in seen:
                seen.add(u)
                hits.append(h)

    cands = harvest_candidates(hits, p)
    if cands:
        print("Aday hasadı:", ", ".join(c["ad"] for c in cands))
    vlines, vpages, vdoms = verify_candidates(cands, core)

    snip = "\n".join(f"- {h.get('title','')} | {h.get('snippet','')} [{h.get('link','')}]"
                     for h in hits[:70])
    # genel sayfa metinleri: aday sayfalarına EK, ilk 4 farklı yeni domain
    pages, doms = list(vpages), set(vdoms)
    for h in hits:
        if len(pages) >= len(vpages) + 4:
            break
        d = _dom(h.get("link"))
        if not d or d in doms:
            continue
        doms.add(d)
        txt = fetch_text(h.get("link"))
        if txt:
            pages.append(f"### {d}\n{txt}")

    material = ("## ARAMA SONUÇLARI (snippet)\n" + snip
                + "\n\n## ADAY DOĞRULAMA (hedefli arama)\n" + ("\n".join(vlines) or "-")
                + "\n\n## SAYFA METİNLERİ\n" + "\n\n".join(pages))
    return material[:MATERIAL_CAP], len(hits)


def verdict(p, material):
    prompt = f"""ABD ürünü: {p['ad']} — {p['tagline']}
Açıklama: {p['desc']}

Aşağıda Türkçe web'den toplanmış arama malzemesi var. SADECE bu malzemeye dayan, BİLGİNDEN UYDURMA.
SORU: Türkiye'de bu ürünün ÇEKİRDEK işini yapan yerli/Türk bir oyuncu VAR mı?

Kurallar:
- Malzemede çekirdek işi yapan, adı geçen yerli bir ürün varsa -> "VAR".
- Liste/karşılaştırma sayfasında adı geçen yerli ürün de GEÇERLİ kanıttır (domain'i yoksa "belirsiz" yaz).
- Malzeme ilgisiz/zayıfsa ya da emin olamıyorsan -> "BELİRSİZ".
- "YOK" deme; ANCAK malzeme genişçe taranmış VE gerçekten hiç yerli oyuncu göstermiyorsa "YOK".
- Her rakip için: aynı çekirdek işi mi yapıyor, ve self-servis mi yoksa kurumsal-satış mı.

SADECE JSON:
{{"var_mi":"VAR|YOK|BELİRSİZ","rakipler":[{{"ad":"","domain":"","ayni_cekirdek_is":true,"self_servis_mi":"evet|hayir|belirsiz"}}],"en_guclu_kanit":"","gerekce":""}}

MALZEME:
{material}"""
    return llm_json(prompt, 6000)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ad"); ap.add_argument("--tagline"); ap.add_argument("--desc")
    a = ap.parse_args()
    for k, v in [("OPENROUTER_API_KEY", OPENROUTER_KEY), ("SERPER_API_KEY", SERPER_KEY)]:
        if not v:
            sys.exit(f"Eksik env: {k}")
    p = {"ad": a.ad, "tagline": a.tagline or "", "desc": a.desc or ""} if a.ad else DEFAULT.copy()

    print(f"Ürün: {p['ad']}\nMalzeme toplanıyor (sorgu üretimi + Serper + aday hasadı + sayfa metni)...\n")
    material, nhits = gather(p)
    print(f"\nToplam {nhits} sonuç, {len(material)} karakter malzeme. Gemini 3.5'e soruluyor...\n")
    res = verdict(p, material)
    print("=" * 64)
    if not res:
        print("Gemini geçerli JSON dönmedi (max_tokens/thinking olabilir)."); return
    print(f"VAR MI  : {res.get('var_mi')}")
    for r in res.get("rakipler", []):
        print(f"  - {r.get('ad')} ({r.get('domain')}) | ayni is: {r.get('ayni_cekirdek_is')} "
              f"| self-servis: {r.get('self_servis_mi')}")
    print(f"Kanıt   : {res.get('en_guclu_kanit')}")
    print(f"Gerekçe : {res.get('gerekce')}")
    print("=" * 64)
    json.dump(res, open("gemini_check_result.json", "w", encoding="utf-8"),
              ensure_ascii=False, indent=2)
    print("Detay -> gemini_check_result.json")


if __name__ == "__main__":
    main()
