#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
US -> TR Fırsat Motoru — v0 ÇEKİRDEK (tek dosya)
================================================

AKIŞ:
  AppSumo (günlük keşif, scrape — /search __NEXT_DATA__)
    -> product_id = AppSumo slug (temiz, değişmez)
    -> GATE 1: duvar ön-elemesi (ucuz LLM)   -> duvar yoksa DROP
    -> (sadece moat-pozitifler için)
       GATE 2: tr_gap  (Türkçe arama + LLM; TR oyuncusunu sınıflar: yok / aynı-segment / farklı-segment / yan-özellik / belirsiz)
       GATE 3: traction = AppSumo deal'i zaten KANITLANMIŞ ücretli talep (paid=True)
    -> durum: PASS / İNCELE / DROP
    -> Notion'a yaz

KURAL: "0 survivor = motor hatası alarmı, piyasa kararı değil." (run sonunda kontrol edilir)

----------------------------------------------------------------------
KURULUM
----------------------------------------------------------------------
  pip install requests

  Ortam değişkenleri (env):
    OPENROUTER_API_KEY
    SERPER_API_KEY  Serper.dev anahtarı (kartsız, 2500 ücretsiz sorgu)  [tr_search değiştirilebilir]
    NOTION_TOKEN    Notion entegrasyon token'ı
    NOTION_PARENT   Notion SAYFA ID'si (integration'la paylaşılmış); DB otomatik kurulur
    NOTION_DB       (ops.) Hazır DB ID'n varsa; yoksa NOTION_PARENT ver, script DB'yi yaratır
    MODEL           (ops.) OpenRouter slug, vars: anthropic/claude-sonnet-4.6
    (PH_TOKEN artık gerekmez — kaynak AppSumo.)

  Notion: DB'yi ELLE kurmana GEREK YOK. Bir boş SAYFA oluştur, o sayfayı integration'la
  paylaş (sayfada ••• -> Connections -> integration'ını ekle), sayfa ID'sini NOTION_PARENT'a koy.
  Script ilk koşuda veritabanını doğru kolonlarla
  (Ürün/Durum/Domain/URL/Duvar/tr_gap/Ücretli/Gerekçe/Kanıt) o sayfanın içinde otomatik yaratır
  ve ID'sini notion_db.json'a kaydeder.

  Çalıştır:        python us_tr_motor.py
  100 deal işle:   python us_tr_motor.py --first 100

  Günlük cron (GitHub Actions örneği):
    on: { schedule: [ { cron: "0 6 * * *" } ] }
    -> bir job: pip install requests && python us_tr_motor.py
    (TARAYICI HTML uygulaması DEĞİL — bu arka planda çalışan bir iş.)
----------------------------------------------------------------------
NOT: Asıl ayar gate'lerde. İlk gerçek koşulardan eşikleri/promptları kalibre edeceğiz.
     AppSumo scrape'i kendi (ev) IP'nden çalışır; engellenirse proxy/headless gerekebilir.
"""

import os, sys, re, json, time, argparse, hashlib
from datetime import datetime, timedelta, timezone

try:
    import requests
except ImportError:
    sys.exit("Önce: pip install requests")

# ---------------------------------------------------------------- config
PH_TOKEN       = os.getenv("PH_TOKEN", "")
OPENROUTER_KEY = os.getenv("OPENROUTER_API_KEY", "")
SERPER_KEY     = os.getenv("SERPER_API_KEY", "")
NOTION_TOKEN   = os.getenv("NOTION_TOKEN", "")
NOTION_DB      = os.getenv("NOTION_DB", "")
NOTION_PARENT  = os.getenv("NOTION_PARENT", "")  # DB'yi içine kuracağı sayfa ID'si
MODEL          = os.getenv("MODEL", "anthropic/claude-sonnet-4.6")  # geçerli bir OpenRouter slug koy
SECOND_STAGE_MODEL = "google/gemini-3.5-flash"   # PASS doğrulama: geniş malzeme + Gemini yargıcı

# AppSumo'da taranacak kategori sorguları (geniş kapsam için)
APPSUMO_QUERIES = ["ai", "marketing", "sales", "crm", "productivity", "seo",
                   "design", "analytics", "email", "automation", "no-code", "support"]

SEEN_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "seen.json")
NOTION_DB_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "notion_db.json")

# Yerel duvar vektörleri (global İngilizce aracın doğrudan veremeyeceği, yerelleştirme gerektiren engel)
MOAT_VECTORS = ["iys", "kvkk", "efatura", "odeme", "pazaryeri", "dil", "lojistik", "erisim"]
MOAT_DESC = (
    "iys=ticari ileti izni(İYS); kvkk=veri ikametgâhı/KVKK; "
    "efatura=e-fatura/e-arşiv/GİB/TR muhasebe-vergi; odeme=yerel ödeme/taksit(iyzico,papara,BKM); "
    "pazaryeri=Trendyol/Hepsiburada/N11/Çiçeksepeti entegrasyonu; dil=Türkçe NLP/ses/içerik; "
    "lojistik=yerel kargo(Yurtiçi,Aras,MNG,Sürat); "
    "erisim=Türkçe pazara erişim: hedef kitle İngilizce'ye UZAK (KOBİ/esnaf/tüketici/teknik-olmayan) ve "
    "ürünü ancak Türkçe pazarlama+arayüz+destek+yerel ödeme+güvenle benimser"
)

# ---------------------------------------------------------------- helpers
LLM_ERR_LOG = os.path.join(os.path.dirname(os.path.abspath(__file__)), "llm_errors.jsonl")


def _log_llm_error(stage, model, err, raw):
    """Parse/ağ hatasında HAM çıktıyı sakla — 'Designer Logo Kit' vakası: hata sessiz kaybolmasın."""
    try:
        with open(LLM_ERR_LOG, "a", encoding="utf-8") as f:
            f.write(json.dumps({"ts": datetime.now(timezone.utc).isoformat(), "stage": stage,
                                "model": model, "error": str(err)[:300],
                                "raw": (raw or "")[:2000]}, ensure_ascii=False) + "\n")
    except Exception:
        pass


def _chat_raw(prompt, model=None, max_tokens=900, timeout=90):
    r = requests.post(
        "https://openrouter.ai/api/v1/chat/completions",
        headers={"Authorization": f"Bearer {OPENROUTER_KEY}", "Content-Type": "application/json"},
        json={"model": model or MODEL, "max_tokens": max_tokens, "temperature": 0,
              "messages": [{"role": "user", "content": prompt}]},
        timeout=timeout,
    )
    r.raise_for_status()
    return r.json()["choices"][0]["message"]["content"]


def _parse_json(txt, want_array=False):
    txt = re.sub(r"^```(json)?|```$", "", txt.strip(), flags=re.MULTILINE).strip()
    pat = r"[\[\{].*[\]\}]" if want_array else r"\{.*\}"
    m = re.search(pat, txt, re.DOTALL)
    return json.loads(m.group(0) if m else txt)


def llm_json(prompt, max_tokens=900, model=None, want_array=False, stage="llm", timeout=90):
    """OpenRouter'a sor, SADECE JSON bekle. Parse/ağ hatasında: ham çıktıyı llm_errors.jsonl'e
    logla + sert 'SADECE JSON' uyarısıyla BİR kez daha dene. İki deneme de patlarsa None."""
    raw = None
    for attempt in (1, 2):
        try:
            q = prompt if attempt == 1 else (
                prompt + "\n\nÖNEMLİ: Önceki yanıtın geçersiz JSON'du. SADECE geçerli ve "
                         "EKSİKSİZ JSON döndür; açıklama, markdown, başka hiçbir şey yazma.")
            raw = _chat_raw(q, model, max_tokens * attempt, timeout)  # 2. deneme: bütçe x2 (yarım-JSON/kesilme vakası)
            return _parse_json(raw, want_array)
        except Exception as e:
            _log_llm_error(stage, model or MODEL, e, raw)
            print(f"   [llm hata deneme {attempt}/2 | {stage}] {e} | ham[:100]={(raw or '')[:100]!r}")
            raw = None
    return None


def llm_json_any(prompt, max_tokens=600):
    """llm_json gibi ama hem {..} hem [..] çıktısını yakalar. Sorgu listesi üretimi için."""
    return llm_json(prompt, max_tokens, want_array=True, stage="llm_any")


def fetch_page(url, limit=2500):
    """URL'den ham metin çek (best-effort; JS-ağır siteler kısmi gelir)."""
    try:
        r = requests.get(url, timeout=15, headers={"User-Agent": "Mozilla/5.0"}, allow_redirects=True)
        html = r.text
        html = re.sub(r"<(script|style)[^>]*>.*?</\1>", " ", html, flags=re.S | re.I)
        text = re.sub(r"<[^>]+>", " ", html)
        return re.sub(r"\s+", " ", text).strip()[:limit]
    except Exception:
        return ""


def _ss_dom(url):
    return re.sub(r"^https?://(www\.)?", "", (url or "").lower()).split("/")[0]


def second_stage_gather(p):
    """v2 — gemini_check laboratuvarında kanıtlanan tasarım (stage2_test sınavı: eski hat bilinen
    rakiplerden 0 yakalıyordu; bu hat Supsis + EVRE'yi adıyla yakaladı):
    1) ÇEŞİTLİLİK-KURALLI sorgular: 'yapay zeka' tek-kalıbı sınırlı, 'yerli/türk' kelimesi sınırlı
       (SERP'i listicle'a büküyor), İngilizce pazar terimi zorunlu.
    2) ADAY HASADI: snippet'lerde ADI GEÇEN olası yerli ürünler — listicle içindekiler DAHİL.
    3) HEDEFLİ DOĞRULAMA: her adaya kendi adıyla arama + ana sayfa metni. Piyango -> sistematik.
    Malzeme bütçesi 30k."""
    gen_prompt = f"""Bir ABD ürününün Türkiye'de yerli muadili var mı diye ARAYACAĞIM.
Ürün: {p['name']} — {p.get('tagline','')}
Açıklama: {p.get('desc','')[:800]}

Türkçe web'de yerel rakipleri bulmak için 9 FARKLI arama sorgusu üret.
KURALLAR (hepsi ZORUNLU):
- "yapay zeka" ifadesi EN FAZLA 2 sorguda geçsin; kalanlar pazarın KENDİ jargonuyla
  (kategorisi neyse onun terimi: "canlı destek", "chatbot", "video oluşturucu" gibi — ürüne göre bul).
- "yerli", "türk", "türkiye", "türkçe" kelimeleri EN FAZLA 1 sorguda geçsin (arama zaten TR'ye kilitli).
- 2 sorgu İngilizce pazar terimi içersin (TR pazarı İngilizce alıntı kullanır: "role play", "AI chatbot" gibi).
- Hem dar (tam işlev) hem geniş (kategori/kullanım senaryosu). MARKA ADI KULLANMA. Tekrarlama.
Ayrıca ürünün çekirdek işini pazarın diliyle 2-4 kelimeyle ver ("cekirdek").
SADECE JSON: {{"cekirdek":"...","sorgular":["s1","s2",...]}}"""
    j = llm_json(gen_prompt, 4000, stage="ss_query") or {}
    qs = [q for q in (j.get("sorgular") or []) if isinstance(q, str) and q.strip()][:9]
    core = (j.get("cekirdek") or "").strip() or (p.get("tagline") or p["name"])[:40]
    if not qs:
        qs = [f"{p.get('tagline', p['name'])} Türkiye", f"{p['name']} alternatif"]

    seen_urls, hits = set(), []
    for q in qs:
        for h in tr_search(q, k=7):
            u = h.get("url", "")
            if u and u not in seen_urls:
                seen_urls.add(u); hits.append(h)

    def _line(h, pre=""):
        return f"- {pre}{h.get('title','')} | {h.get('snippet','')} [{h.get('url','')}]"

    # 2) ADAY HASADI
    cand_prompt = f"""ABD ürünü: {p['name']} — {p.get('tagline','')}
Aşağıda Türkçe arama sonuçları (snippet) var. Bu metinlerde ADI GEÇEN ve Türk/yerli OLABİLECEK
ürün/şirket adaylarını çıkar:
- Liste/blog sayfalarının İÇİNDE anılan adlar da DAHİL (sayfanın kendisi değil, içindeki adlar).
- Global devleri alma (Intercom, Zendesk, Canva, Synthesia gibi).
- Emin olmadıkların da girsin; eleme sonra yapılacak.
- domain'i snippet'ten biliyorsan yaz, bilmiyorsan boş bırak.
SADECE JSON dizi (en fazla 8): [{{"ad":"","domain":""}}]

SONUÇLAR:
{chr(10).join(_line(h) for h in hits[:80])}"""
    out = llm_json(cand_prompt, 4000, want_array=True, stage="ss_harvest")
    cands = ([c for c in out if isinstance(c, dict) and (c.get("ad") or "").strip()][:8]
             if isinstance(out, list) else [])
    if cands:
        print(f"   [2. aşama] aday hasadı: {', '.join(c['ad'] for c in cands)}")

    # 3) HEDEFLİ DOĞRULAMA
    vlines, pages, doms = [], [], set()
    for c in cands:
        ad = c["ad"].strip()
        hs = tr_search(f"{ad} {core}", k=4)
        vlines += [_line(h, f"[aday: {ad}] ") for h in hs]
        dom = _ss_dom(c.get("domain") or "")
        if not dom:  # hedefli aramadan adayın kendi sitesini yakala
            key = re.sub(r"[^a-z0-9]", "", ad.lower())[:6]
            for h in hs:
                d = _ss_dom(h.get("url"))
                if key and key in re.sub(r"[^a-z0-9]", "", d):
                    dom = d
                    break
        if dom and dom not in doms:
            doms.add(dom)
            txt = fetch_page(f"https://{dom}", 2000)
            if txt:
                pages.append(f"### {dom} (aday: {ad})\n{txt}")

    n_cand_pages = len(pages)  # genel sayfalar: aday sayfalarına EK ilk 4 yeni domain
    for h in hits:
        if len(pages) >= n_cand_pages + 4:
            break
        d = _ss_dom(h.get("url"))
        if not d or d in doms:
            continue
        doms.add(d)
        txt = fetch_page(h.get("url"), 2000)
        if txt:
            pages.append(f"### {d}\n{txt}")

    material = ("## ARAMA SONUÇLARI\n" + "\n".join(_line(h) for h in hits[:70])
                + "\n\n## ADAY DOĞRULAMA (hedefli arama)\n" + ("\n".join(vlines) or "-")
                + "\n\n## SAYFA METİNLERİ\n" + "\n\n".join(pages))
    return material[:30000]


def second_stage_verdict(p, material):
    """Gemini 3.5: bol malzemeye bak, TR rakip var mı yok mu söyle."""
    prompt = f"""ABD ürünü: {p['name']} — {p.get('tagline','')}
Açıklama: {p.get('desc','')[:600]}

Türkçe web'den toplanmış arama malzemesi aşağıda. SADECE bu malzemeye dayan, BİLGİNDEN UYDURMA.
SORU: Türkiye'de bu ürününün ÇEKİRDEK işini yapan yerli/Türk oyuncu VAR mı?
- Malzemede yerli ürün adı geçiyorsa → "VAR". ANCAK dolaylı/farklı-segment/farklı-kategori rakip için "BELİRSİZ" de, "VAR" deme.
- Liste/karşılaştırma sayfasında adı geçen yerli ürün de geçerli kanıttır (domain'i malzemede yoksa domain alanına "belirsiz" yaz).
- Malzeme yetersiz/ilgisizse → "BELİRSİZ".
- Malzeme kapsamlı ama hiç yerli oyuncu yoksa → "YOK".
- VAR diyebilmek için malzemeden doğrudan alıntı zorunlu; bellekten bilgi ekleme.
SADECE JSON: {{"var_mi":"VAR|YOK|BELİRSİZ","rakipler":[{{"ad":"","domain":""}}],"en_guclu_kanit":"","gerekce":""}}
MALZEME:\n{material}"""
    return llm_json(prompt, 4000, model=SECOND_STAGE_MODEL, stage="gemini2", timeout=180)


def run_second_stage(p):
    """Motor PASS üretince çağrılır. Gemini doğrulaması → (var_mi, detay) döner."""
    try:
        material = second_stage_gather(p)
        res = second_stage_verdict(p, material)
        if not res:
            return "HATA", "Gemini yanıt vermedi"
        var_mi = str(res.get("var_mi", "BELİRSİZ")).upper()
        rakipler = ", ".join(r.get("ad", r.get("domain", "?")) for r in res.get("rakipler", []))
        kanit = str(res.get("en_guclu_kanit", ""))[:200]
        return var_mi, f"rakipler:[{rakipler}] | kanit:{kanit}"
    except Exception as e:
        return "HATA", str(e)


_SEARCH_FAILS = 0  # tr_search hata sayacı; gate_tr_gap 'arama çöktü' durumunda STRUCTURAL_GAP'i İNCELE'ye düşürür


def tr_search(query, k=6):
    """Türkçe web araması. Serper.dev (Google sonuçları). Sağlayıcı DEĞİŞTİRİLEBİLİR.
    HATA ≠ BOŞ SONUÇ: hata sayaca işlenir; 'rakip bulunamadı' kanıtı o koşuda güvenilmez sayılır."""
    global _SEARCH_FAILS
    try:
        r = requests.post(
            "https://google.serper.dev/search",
            headers={"X-API-KEY": SERPER_KEY, "Content-Type": "application/json"},
            json={"q": query, "gl": "tr", "hl": "tr", "num": k},
            timeout=30,
        )
        r.raise_for_status()
        res = r.json().get("organic", []) or []
        return [{"title": x.get("title", ""), "url": x.get("link", ""),
                 "snippet": x.get("snippet", "")} for x in res[:k]]
    except Exception as e:
        _SEARCH_FAILS += 1
        print(f"   [arama hata] {e}")
        return []


TWO_LEVEL_TLDS = {"co.uk", "org.uk", "ac.uk", "com.tr", "org.tr", "net.tr", "co.in",
                  "com.au", "co.nz", "co.jp", "com.br", "com.mx", "co.za", "com.sg"}

def canonical_root_domain(url):
    """product_id: kök domain (sistem-üretimli, değişmez). Çözemezse None -> İNCELE."""
    if not url:
        return None
    u = url.strip().lower()
    u = re.sub(r"^https?://", "", u)
    u = re.sub(r"^www\.", "", u)
    host = u.split("/")[0].split("?")[0].split(":")[0].strip()
    # PH redirect / kısaltıcıları gerçek domain değildir
    if not host or host in ("producthunt.com", "ph.co", "bit.ly", "t.co"):
        return None
    parts = host.split(".")
    if len(parts) >= 3 and ".".join(parts[-2:]) in TWO_LEVEL_TLDS:
        return ".".join(parts[-3:])
    return ".".join(parts[-2:]) if len(parts) >= 2 else host


def resolve_url(u):
    """PH 'website' alanı genelde producthunt.com/r/ YÖNLENDİRMESİDİR; gerçek hedef URL'ye çöz."""
    if not u:
        return ""
    ua = {"User-Agent": "Mozilla/5.0 (compatible; us-tr-motor/1.0)"}
    for method in ("head", "get"):
        try:
            r = getattr(requests, method)(u, allow_redirects=True, timeout=15,
                                          headers=ua, stream=(method == "get"))
            if r.url:
                return r.url
        except Exception:
            continue
    return u


# ---------------------------------------------------------------- kaynak: AppSumo
APPSUMO_UA = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
              "Accept-Language": "tr,en-US;q=0.9,en;q=0.8"}

def fetch_appsumo(query, limit=40):
    """AppSumo /search sayfasındaki __NEXT_DATA__ -> fallbackData[].deals[] listesini çeker."""
    url = f"https://appsumo.com/search/?query={query}"
    try:
        r = requests.get(url, headers=APPSUMO_UA, timeout=40)
        r.raise_for_status()
    except Exception as e:
        print(f"   [appsumo hata: {query}] {e}")
        return []
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', r.text, re.DOTALL)
    if not m:
        print(f"   [appsumo: {query}] __NEXT_DATA__ yok")
        return []
    try:
        data = json.loads(m.group(1))
        fb = data.get("props", {}).get("pageProps", {}).get("fallbackData", []) or []
        deals = []
        for block in fb:
            deals += block.get("deals", []) or []
    except Exception as e:
        print(f"   [appsumo parse: {query}] {e}")
        return []
    out = []
    for d in deals[:limit]:
        out.append({
            "pid": d.get("slug") or str(d.get("id", "")),
            "name": d.get("public_name", ""),
            "tagline": d.get("card_description", ""),
            "desc": d.get("card_description", ""),
            "site": "https://appsumo.com" + (d.get("get_absolute_url") or ""),
            "codes": d.get("codes_remaining"),
            "status": d.get("browse_deal_status", ""),
        })
    return out


# ---------------------------------------------------------------- kaynak: Product Hunt (artık kullanılmıyor)
def fetch_new_ph_products(days, first):
    since = (datetime.now(timezone.utc) - timedelta(days=days)).strftime("%Y-%m-%dT%H:%M:%SZ")
    # NOT: PH şeması değişmişse alan adlarını (website/url) güncelle. (api.producthunt.com/v2/api/graphql)
    q = """
    { posts(order: NEWEST, first: %d, postedAfter: "%s") {
        edges { node { id name tagline description url website votesCount } } } }
    """ % (first, since)
    try:
        r = requests.post(
            "https://api.producthunt.com/v2/api/graphql",
            headers={"Authorization": f"Bearer {PH_TOKEN}", "Content-Type": "application/json"},
            json={"query": q}, timeout=60,
        )
        r.raise_for_status()
        edges = r.json()["data"]["posts"]["edges"]
    except Exception as e:
        sys.exit(f"PH çekilemedi: {e}")
    out = []
    for ed in edges:
        n = ed["node"]
        out.append({
            "ph_id": n.get("id"),
            "name": n.get("name", ""),
            "tagline": n.get("tagline", ""),
            "desc": (n.get("description") or "")[:600],
            "site": resolve_url(n.get("website") or ""),
            "votes": n.get("votesCount", 0),
        })
    return out


# ---------------------------------------------------------------- GATE 1: duvar
def _norm_text(s):
    return re.sub(r"\s+", " ", (s or "").lower()).strip()


def _citation_grounded(cit, blob):
    """Alıntı ürün metninde gerçekten geçiyor mu? Önce birebir (normalize), olmazsa
    kelime-örtüşmesi >= 0.8 (LLM alıntıyı hafifçe bozabilir; tamamen uydurmayı yine yakalar)."""
    c = _norm_text(cit).strip('"\u201c\u201d\u2018\u2019\'')
    if not c:
        return False
    if c in blob:
        return True
    toks = [t for t in re.findall(r"[a-z0-9çğıöşüâî]+", c) if len(t) > 3]
    if not toks:
        return False
    return sum(1 for t in toks if t in blob) / len(toks) >= 0.8


def gate_moat(p):
    """v2 — TANIK+YARGIÇ (gate_tr_gap ile aynı felsefe; devir #3, A varyantı: kanıt ürün metninden, arama yok).
    Eski hali tek çıplak LLM kararıydı -> 34/40 blanket 'duvar yok' üretti. Şimdi:
    LLM = tanık: her ADAY vektör için yapısal olgu + ürün metninden BİREBİR alıntı çıkarır, KARAR VERMEZ.
    Python = yargıç: alıntı metinde doğrulanır; teknik vektör yapısal zorunlulukla, erisim kitle tipiyle geçer.
    Dönen: (kabul edilen vektörler, vektör-bazlı denetim izi)."""
    blob = _norm_text(f"{p['name']} {p.get('tagline','')} {p.get('desc','')}")
    prompt = f"""Ürün metnine bak: aşağıdaki 8 yerel-duvar vektöründen hangileri ADAY olabilir?
Yerel duvar = global İngilizce aracın TR'de tek başına kapatamayacağı yapısal engel.
Vektörler: {MOAT_DESC}.

TANIK MODUNDASIN — KARAR VERME, kanıt çıkar. Eleme Python'da yapılacak:
- Aday OLABİLECEK her vektör için bir claim yaz. Emin olmasan da yaz; zayıf claim'i kural eler.
- citation: ürün metninden BİREBİR kısa alıntı. UYDURMA — metinde geçmeyen alıntı claim'i otomatik geçersiz kılar.
- structural_fact: tek cümle — bu vektörü duvar yapan YAPISAL durum.
- yapisal_zorunluluk (teknik vektörler: iys/kvkk/efatura/odeme/pazaryeri/dil/lojistik):
  true SADECE ürün o Türk sistemine teknik ENTEGRE olmak zorundaysa ya da (dil için) çekirdek işlev
  dile/sese bağımlıysa. Konu benzerliği YETMEZ ("muhasebe/CSV" -> efatura DEĞİL). erisim için bu alanı false bırak.
- kitle_tipi (sadece erisim): "ing_uzak" SADECE hedef kitle İngilizce'ye uzaksa (KOBİ, esnaf, tüketici,
  teknik-olmayan). Geliştirici/pazarlamacı/ajans/SEO'cu/SaaS kurucusu = "ing_rahat".
- Hiç aday yoksa claims boş liste.

Ürün: {p['name']} — {p['tagline']}
Açıklama: {p['desc']}

SADECE JSON:
{{"claims":[{{"vector":"...","structural_fact":"...","citation":"...","yapisal_zorunluluk":false,"kitle_tipi":"ing_uzak|ing_rahat"}}]}}"""
    j = llm_json(prompt, 900, stage="moat") or {}
    accepted, audit = [], []
    for c in (j.get("claims") or []):
        v = c.get("vector")
        if v not in MOAT_VECTORS or v in accepted:
            continue
        if not _citation_grounded(c.get("citation") or "", blob):
            audit.append(f"{v}:RED[alıntı metinde yok]")
            continue
        fact = _norm_text(c.get("structural_fact"))[:90]
        if v == "erisim":
            ok = c.get("kitle_tipi") == "ing_uzak"
            audit.append(f"{v}:{'KABUL' if ok else 'RED[kitle ing_rahat]'}({fact})")
        else:
            ok = _b(c.get("yapisal_zorunluluk"))
            audit.append(f"{v}:{'KABUL' if ok else 'RED[yapısal zorunluluk yok]'}({fact})")
        if ok:
            accepted.append(v)
    reason = " | ".join(audit) if audit else "aday vektör claim'i yok"
    return accepted, reason


# ---------------------------------------------------------------- GATE 2: tr_gap (rakip-yokluğu)
def tr_query_gen(p):
    """LLM = TANIK: ürünün çekirdek işlevini kısa Türkçe isim öbeğine çevirir (çıkarım). Sorgular SABİT
    şablondan üretilir — deterministik retrieval (panel C). Marka adı yasak (isim çakışması)."""
    prompt = f"""Bu ürünün çekirdek işlevini 2-4 kelimelik TEK bir Türkçe isim öbeğine çevir.
MARKA ADI KULLANMA. Fiil değil, kısa isim öbeği.
Örnek: "yapay zeka toplantı notu", "abonelik faturalama", "whatsapp chatbot", "yapay zeka içerik üretme".
Ürün: {p['name']} — {p['tagline']}
Açıklama: {p['desc']}
SADECE JSON: {{"islev": "..."}}"""
    j = llm_json(prompt, 80) or {}
    islev = (j.get("islev") or "").strip().strip('"') or (p.get("tagline") or p["name"])
    return [f"{islev} yazılımı Türkiye", f"yerli {islev} aracı", f"{islev} fiyatlandırma KOBİ"]


def is_local_tr(dom, hits):
    """Adı verilen rakip GERÇEKTEN Türk/yerel mi? (Yabancı bir firma TR aramasında çıkıp 'yerli'
    sanılabilir — Alrite/alrite.io vakası: Macar.) .tr -> kesin yerel. Değilse, SADECE o domaine ait
    gerçek snippet'lere bakarak (hafıza değil) LLM 'evet/hayir/belirsiz' der.
    Dönen: (karar, gerekce). 'evet' DIŞINDAKİ her şey = yerel doğrulanmadı."""
    dom = (dom or "").lower()
    if not dom:
        return "belirsiz", "domain yok"
    if dom.endswith(".tr"):
        return "evet", ".tr alan adı"
    snips = [f"{h.get('title','')} | {h.get('snippet','')}"
             for h in hits if dom in (h.get("url", "") or "").lower()]
    if not snips:
        return "belirsiz", "domaine ait snippet bulunamadı"
    blob = "\n".join(snips[:5])[:1200]
    prompt = f"""Bu firma Türk/yerel bir şirket mi, yoksa TR arama sonuçlarında çıkan YABANCI bir firma mı?
SADECE alan adına ve aşağıdaki arama sonucu metnine bak; emin değilsen 'belirsiz' de, TAHMİN ETME.
İpuçları: .com.tr/.tr çoğunlukla Türk; Türkçe içerik/Türkiye merkez = yerel; yabancı dil (ör. Macarca, yalnız İngilizce) + yabancı menşe = yabancı.
ALAN ADI: {dom}
METİN:
{blob}
SADECE JSON: {{"yerel":"evet|hayir|belirsiz","gerekce":"tek cümle"}}"""
    j = llm_json(prompt, 200) or {}
    karar = j.get("yerel") if j.get("yerel") in ("evet", "hayir", "belirsiz") else "belirsiz"
    return karar, (j.get("gerekce") or "")


# ---- TR girişim ekosistemi kaynakları (recall recheck için — panel kararı) ----
TR_ECOSYSTEM_SITES = ["webrazzi.com", "egirisim.com", "startupmarket.co", "shiftdelete.net"]
ECOSYSTEM_NEWS_HOSTS = set(TR_ECOSYSTEM_SITES) | {
    "apps.apple.com", "play.google.com", "linkedin.com", "youtube.com", "medium.com",
    "wikipedia.org", "tr.wikipedia.org", "cbinsights.com", "crunchbase.com",
    "facebook.com", "instagram.com", "x.com", "twitter.com",
}


def _host(url):
    return re.sub(r"^https?://(www\.)?", "", str(url or "").lower()).split("/")[0]


def _is_eco_news(host):
    return any(host == d or host.endswith("." + d) for d in ECOSYSTEM_NEWS_HOSTS)


_TR_PROFILE_CACHE = {}  # kanonik domain -> intrinsic profil (koşu-içi; aynı oyuncu her üründe AYNI profili alır)


def _b(x):
    """LLM 'true'/'false'/'unknown' ya da bool döndürebilir; SADECE kesin TRUE'yu True say."""
    return x is True or (isinstance(x, str) and x.strip().lower() == "true")


def extract_tr_players(name, tagline, hits):
    """LLM = TANIK (yargıç DEĞİL). SERP'ten TR oyuncularını ve YAPILANDIRILMIŞ KANITLARINI çıkarır.
    Karar/etiket vermez. Her oyuncu: url + gözlenen olgular."""
    serp = "\n".join(f"- {h['title']} | {h['url']} | {h['snippet'][:160]}" for h in hits[:14]) or "(sonuç yok)"
    prompt = f"""Aşağıdaki ARAMA SONUÇLARINDAN, '{name}' ({tagline}) ürününün ÇEKİRDEK işini yapan TÜRK/yerli oyuncuları çıkar.
KARAR VERME, SINIFLANDIRMA YAPMA — yalnız her oyuncu için GÖZLENEN OLGULARI doldur.
Bir oyuncuyu YALNIZ url'si sonuçlarda BİREBİR geçiyorsa ekle; geçmiyorsa UYDURMA. Oyuncu yoksa boş liste.

Her oyuncu için alanlar:
- url: arama sonuçlarından birebir kopyala
- does_core_work: ürünle AYNI çekirdek işi mi yapıyor? true/false
- has_public_pricing: açık/şeffaf fiyat sayfası görünüyor mu? true/false/unknown
- has_self_serve_signup: kendi kendine kayıt/ücretsiz deneme var mı (satış-temaslı DEĞİL)? true/false/unknown
- target_buyer: kanıta dayalı — açık fiyat+self kayıt => "smb_self_serve"; "demo iste/teklif al/kurumsal satış" => "enterprise_sales"; belirsiz => "unknown"

ARAMA SONUÇLARI:
{serp}
SADECE JSON: {{"players":[{{"name":"","url":"","does_core_work":true,"has_public_pricing":"unknown","has_self_serve_signup":"unknown","target_buyer":"unknown"}}]}}"""
    j = llm_json(prompt, 800) or {}
    return j.get("players") or []


def canon_profiles(players, hits):
    """KANONİK PROFİL (panel): aynı domain her üründe AYNI intrinsic profili alır -> Tiny Talk/Charla
    aynı Mavibot'a aynı segmenti verir. intrinsic alanlar domain bazlı cache'lenir; does_core_work ürüne özel."""
    out = []
    for pl in players:
        dom = _host(pl.get("url"))
        if not dom:
            continue
        prof = _TR_PROFILE_CACHE.get(dom)
        if prof is None:
            tb = pl.get("target_buyer")
            prof = {"has_pricing": _b(pl.get("has_public_pricing")),
                    "has_signup": _b(pl.get("has_self_serve_signup")),
                    "target_buyer": tb if tb in ("smb_self_serve", "enterprise_sales") else "unknown",
                    "is_local": is_local_tr(dom, hits)[0]}
            _TR_PROFILE_CACHE[dom] = prof
        out.append({"dom": dom, "name": pl.get("name") or dom,
                    "does_core_work": _b(pl.get("does_core_work")), **prof})
    return out


def rule_engine_gap(players, ev):
    """PYTHON = YARGIÇ (LLM değil). Çıkarılan kanıtlardan kararı verir. Dönen: (verdict, reason, chosen).
    Tek otomatik PASS yolu STRUCTURAL_GAP; SEGMENT (self-servis kanıtı yok) -> INCOMPLETE (insan)."""
    serp_urls = " ".join(r.get("url", "").lower() for e in ev for r in e["results"])
    cand = [pl for pl in players
            if pl["dom"] in serp_urls and pl.get("is_local") == "evet" and pl.get("does_core_work")]
    if not cand:
        return "STRUCTURAL_GAP", "kanıt: çekirdek işi yapan grounded+yerel oyuncu yok", None
    for pl in cand:
        if (not _is_eco_news(pl["dom"])) and pl.get("has_pricing") and pl.get("has_signup") \
                and pl.get("target_buyer") == "smb_self_serve":
            return "NO_GAP", f"kanıt: {pl['dom']} kendi sitesinde açık fiyat + self-servis kayıt + aynı iş", pl
    doms = ", ".join(dict.fromkeys(pl["dom"] for pl in cand))
    return ("INCOMPLETE",
            f"kanıt: yerli oyuncu(lar) var ({doms}) ama self-servis/KOBİ kanıtı doğrulanamadı (SEGMENT_UNVERIFIED) -> insan",
            cand[0])


def local_discovery_recheck(function_queries):
    """STRUCTURAL_GAP iddiasını TR girişim EKOSİSTEMİNDE hedefli doğrular (genel 'yerli' araması DEĞİL).
    Global SEO listeleri yerli girişimi gömdüğü için işlev anahtarını ekosistem kaynaklarıyla kısıtlar.
    Karar VERMEZ; sadece aday SERP + ev döndürür."""
    site_filter = "(" + " OR ".join(f"site:{s}" for s in TR_ECOSYSTEM_SITES) + ")"
    rhits, rev = [], []
    for q in function_queries[:2]:
        dq = f"{q} {site_filter}"
        res = tr_search(dq, 6)
        rev.append({"query": dq, "searched_at": datetime.now(timezone.utc).isoformat(),
                    "results": [{"url": x["url"], "title": x["title"]} for x in res]})
        rhits += res
        time.sleep(0.4)
    return rhits, rev


def live_product_check(name):
    """Recheck adayı CANLI bir self-servis/ödemeli ürün mü? (Panel: Webrazzi haberi tek başına kanıt değil —
    ölü/pivot girişim yanlış DROP üretmesin.) Ürünün KENDİ alan adında pricing/signup izi arar.
    Dönen: (True, domain) veya (False, '')."""
    res = tr_search(f"{name} fiyat pricing kayıt ol üyelik giriş", 6)
    key = re.sub(r"[^a-z0-9]", "", (name or "").lower())[:8]
    for h in res:
        host = _host(h.get("url"))
        if not host or _is_eco_news(host):
            continue
        domkey = re.sub(r"[^a-z0-9]", "", host)
        text = (h.get("title", "") + " " + h.get("snippet", "")).lower()
        if key and (key in domkey or (name or "").lower() in text):
            return True, host
    return False, ""


# ---- SEGMENT_GAP recheck kaynağı (STRUCTURAL'dan FARKLI — panel: PR/haber değil, self-servis/KOBİ kanıtı) ----
TR_MARKET_SITES = ["ikas.com", "ticimax.com", "ideasoft.com.tr", "tsoft.com.tr"]
RUN_ID = datetime.now(timezone.utc).strftime("run_%Y%m%d_%H%M%S")


def segment_discovery_recheck(function_queries):
    """SEGMENT_GAP ('oyuncu var ama farklı segment') iddiasını self-servis/KOBİ kanıt havuzunda doğrular.
    STRUCTURAL recheck'ten FARKLI kaynak: PR/haber değil — fiyat/paket odaklı SERP + TR e-ticaret app marketleri.
    Karar VERMEZ; aday SERP + ev döndürür."""
    mkt = "(" + " OR ".join(f"site:{s}" for s in TR_MARKET_SITES) + ")"
    qbase = function_queries[0] if function_queries else ""
    seg_queries = [f'{qbase} (fiyatlandırma OR paketler OR "ücretsiz plan")', f"{qbase} {mkt}"]
    if len(function_queries) > 1:
        seg_queries.append(f"{function_queries[1]} (fiyatlandırma OR ücretsiz)")
    rhits, rev = [], []
    for dq in seg_queries:
        res = tr_search(dq, 6)
        rev.append({"query": dq, "searched_at": datetime.now(timezone.utc).isoformat(),
                    "results": [{"url": x["url"], "title": x["title"]} for x in res]})
        rhits += res
        time.sleep(0.4)
    return rhits, rev


def _sha(items):
    return hashlib.sha256("\n".join(sorted(s for s in items if s)).encode("utf-8")).hexdigest()[:12]


def log_verdict(product_id, first_pass, recheck_by, final_verdict, queries, hits):
    """Varyans audit (panel): aynı query_set_hash + farklı verdict = sınıflandırıcı varyansı;
    farklı serp_url_set_hash = retrieval varyansı. verdict_log.jsonl'e satır ekler."""
    entry = {"run_id": RUN_ID, "product_id": product_id,
             "query_set_hash": _sha(queries),
             "serp_url_set_hash": _sha([h.get("url", "") for h in hits]),
             "first_pass_verdict": first_pass, "recheck_triggered_by": recheck_by,
             "final_verdict": final_verdict, "ts": datetime.now(timezone.utc).isoformat()}
    try:
        with open("verdict_log.jsonl", "a", encoding="utf-8") as f:
            f.write(json.dumps(entry, ensure_ascii=False) + "\n")
    except Exception:
        pass


def gate_tr_gap(p):
    global _SEARCH_FAILS
    fails_before = _SEARCH_FAILS
    name = p["name"]
    queries = tr_query_gen(p)
    hits, ev = [], []
    for q in queries:
        res = tr_search(q, 5)
        ev.append({"query": q, "searched_at": datetime.now(timezone.utc).isoformat(),
                   "results": [{"url": x["url"], "title": x["title"]} for x in res]})
        hits += res
        time.sleep(0.4)

    # İLK GEÇİŞ — LLM KANIT çıkarır (tanık), kanonik profil uygulanır, PYTHON kural motoru KARAR verir.
    players = canon_profiles(extract_tr_players(name, p["tagline"], hits), hits)
    verdict, reason, chosen = rule_engine_gap(players, ev)
    first_pass = verdict
    recheck_by = None

    if verdict == "STRUCTURAL_GAP":
        # Recall backstop: TR girişim ekosisteminde (haber kaynakları) aday ARA; bulunan adayın CANLI kendi
        # sitesini live_product_check ile doğrula -> ancak o zaman NO_GAP. Aksi halde insan (ölü/pivot riski).
        recheck_by = "structural_gap"
        rhits, rev = local_discovery_recheck(queries)
        ev += rev
        rplayers = canon_profiles(extract_tr_players(name, p["tagline"], rhits), rhits)
        core = [pl for pl in rplayers if pl.get("does_core_work") and pl.get("is_local") == "evet"]
        live_dom = None
        for pl in core:
            ok, dom = live_product_check(pl["name"])
            if ok:
                live_dom = dom
                break
        if live_dom:
            verdict = "NO_GAP"
            reason = f"RECHECK: ekosistemde yerli oyuncu, canlı kendi sitesi doğrulandı ({live_dom})"
        elif core:
            verdict = "INCOMPLETE"
            reason = "RECHECK: ekosistemde yerli aday var ama canlı kendi sitesi doğrulanamadı (ölü/pivot olabilir) -> insan"
        else:
            reason = "RECHECK: ekosistemde de yerli oyuncu yok -> STRUCTURAL_GAP doğrulandı"

    elif verdict == "INCOMPLETE" and chosen is not None:
        # SEGMENT_UNVERIFIED backstop: self-servis/KOBİ muadilini FARKLI kaynakta (fiyat/market) ara.
        # Kural motoru self-servis kanıtı bulursa -> NO_GAP; bulamazsa INCOMPLETE (insan) kalır. PASS YOK.
        recheck_by = "segment_gap"
        rhits, rev = segment_discovery_recheck(queries)
        ev += rev
        rplayers = canon_profiles(extract_tr_players(name, p["tagline"], rhits), rhits)
        sverdict, sreason, schosen = rule_engine_gap(rplayers, rev)
        if sverdict == "NO_GAP":
            verdict = "NO_GAP"
            reason = f"SEGMENT-RECHECK: {sreason}"
        else:
            reason = f"SEGMENT-RECHECK: self-servis/KOBİ muadili doğrulanamadı -> insan | {reason}"

    # GÜVENLİK KİLİDİ: bu ürünün değerlendirmesinde arama hatası olduysa "rakip yok" kanıtı GÜVENİLMEZ.
    # (Serper 403 vakası: kör arama -> boş SERP -> STRUCTURAL_GAP -> yanlış PASS. Kanıt yokluğu ≠ yokluğun kanıtı.)
    nfails = _SEARCH_FAILS - fails_before
    if nfails > 0 and verdict == "STRUCTURAL_GAP":
        verdict = "INCOMPLETE"
        reason = f"ARAMA HATASI ({nfails} sorgu çöktü) — 'yerli oyuncu yok' doğrulanamaz -> insan | {reason}"

    log_verdict(p.get("pid") or name, first_pass, recheck_by, verdict, queries, hits)
    return verdict, reason, ev


# ---------------------------------------------------------------- GATE 3: traction (us_paid_demand)
def gate_traction(p):
    # Eşikler KALİBRE EDİLMEDİ -> sadece sinyal topla, oto-PASS'i has_paid_pricing'e bağla.
    res = tr_search(f"{p['name']} pricing plans", 5) + tr_search(f"{p['name']} reviews", 4)
    serp = "\n".join(f"- {h['title']} | {h['url']} | {h['snippet'][:140]}" for h in res[:12]) or "(yok)"
    prompt = f"""Ürünün ÜCRETLİ fiyatı var mı, ve ödeme-temelli traction sinyali var mı?
(AppSumo satış / G2-Capterra verified-buyer yorumları / aktif ücretli plan). Eşik YOK, sadece raporla.

ÜRÜN: {p['name']} — {p['tagline']}
SONUÇLAR:
{serp}

SADECE JSON: {{"has_paid_pricing": true, "signals": ["..."], "note": "tek cümle"}}"""
    j = llm_json(prompt, 350) or {}
    return bool(j.get("has_paid_pricing")), (j.get("note") or "")


# ---------------------------------------------------------------- karar
def decide_status(pid, vectors, gap, paid):
    if pid is None:
        return "İNCELE", "domain çözülemedi (product_id yok)"
    if not vectors:
        return "DROP", "yerel duvar yok -> global TR'ye doğrudan hizmet veriyor"
    if gap == "NO_GAP":
        return "DROP", "TR'de aynı işi aynı segmente yapan müstakil yerli oyuncu var"
    if gap == "BROAD_FEATURE":
        return "İNCELE", "iş büyük bir TR suite'inin yan özelliği; kapsam derinliği insan kontrolü gerektiriyor (Aşama 2)"
    if gap == "INCOMPLETE":
        return "İNCELE", "TR oyuncu varlığı/işlevi belirsiz"
    # STRUCTURAL_GAP (TR'de hiç yok) veya SEGMENT_GAP (var ama farklı segmente)
    if not paid:
        return "İNCELE", "boşluk var ama ödeme sinyali zayıf/yok"
    if gap == "SEGMENT_GAP":
        return "İNCELE", "TR'de oyuncu var, segment farkı kanıtlanamadı -> insan kontrolü (SEGMENT_UNVERIFIED)"
    return "PASS", "TR'de çekirdek işi yapan müstakil yerli oyuncu yok"


# ---------------------------------------------------------------- Notion
def _notion_auth():
    return {"Authorization": f"Bearer {NOTION_TOKEN}",
            "Notion-Version": "2022-06-28", "Content-Type": "application/json"}

def ensure_notion_db():
    """DB ID'sini döndür. Yoksa NOTION_PARENT sayfasının İÇİNDE DB'yi otomatik yaratır."""
    if NOTION_DB:
        return NOTION_DB
    try:
        return json.load(open(NOTION_DB_FILE, encoding="utf-8"))["id"]
    except Exception:
        pass
    if not NOTION_PARENT:
        sys.exit("Eksik env: NOTION_DB ya da NOTION_PARENT (DB'yi kuracağı sayfa ID'si)")
    schema = {
        "Ürün":    {"title": {}},
        "Durum":   {"select": {"options": [{"name": "PASS"}, {"name": "İNCELE"}, {"name": "DROP"}]}},
        "Domain":  {"rich_text": {}},
        "URL":     {"url": {}},
        "Duvar":   {"multi_select": {"options": [{"name": v} for v in MOAT_VECTORS]}},
        "tr_gap":  {"select": {"options": [{"name": x} for x in
                    ["STRUCTURAL_GAP", "NO_GAP", "INCOMPLETE", "-"]]}},
        "Ücretli": {"checkbox": {}},
        "Gerekçe": {"rich_text": {}},
        "Kanıt":   {"rich_text": {}},
    }
    body = {"parent": {"type": "page_id", "page_id": NOTION_PARENT},
            "title": [{"type": "text", "text": {"content": "US->TR Fırsatlar"}}],
            "properties": schema}
    try:
        r = requests.post("https://api.notion.com/v1/databases",
                          headers=_notion_auth(), json=body, timeout=40)
        if r.status_code >= 300:
            sys.exit(f"Notion DB oluşturulamadı (sayfayı integration'la paylaştın mı?): {r.text[:400]}")
        db_id = r.json()["id"]
        json.dump({"id": db_id}, open(NOTION_DB_FILE, "w", encoding="utf-8"))
        print(f"Notion veritabanı oluşturuldu ✓  ({db_id})")
        return db_id
    except SystemExit:
        raise
    except Exception as e:
        sys.exit(f"Notion DB oluşturulamadı: {e}")


def notion_write(db_id, pid, p, vectors, gap, paid, status, reason, evidence):
    ev_txt = ""
    for e in evidence:
        ev_txt += f"[{e['query']}] {e['searched_at']}\n"
        for r in e["results"][:4]:
            ev_txt += f"  - {r['url']}\n"
    props = {
        "Ürün":    {"title": [{"text": {"content": p["name"][:200] or "?"}}]},
        "Durum":   {"select": {"name": status}},
        "Domain":  {"rich_text": [{"text": {"content": pid or "-"}}]},
        "Duvar":   {"multi_select": [{"name": v} for v in vectors]},
        "tr_gap":  {"select": {"name": gap or "-"}},
        "Ücretli": {"checkbox": bool(paid)},
        "Gerekçe": {"rich_text": [{"text": {"content": (reason or "")[:1900]}}]},
        "Kanıt":   {"rich_text": [{"text": {"content": ev_txt[:1900]}}]},
    }
    if p.get("site"):
        props["URL"] = {"url": p["site"]}
    try:
        r = requests.post(
            "https://api.notion.com/v1/pages",
            headers=_notion_auth(),
            json={"parent": {"database_id": db_id}, "properties": props}, timeout=40,
        )
        r.raise_for_status()
    except Exception as e:
        print(f"   [notion hata] {e}")


# ---------------------------------------------------------------- dedup
def load_seen():
    try:
        return set(json.load(open(SEEN_FILE, encoding="utf-8")))
    except Exception:
        return set()

def save_seen(seen):
    try:
        json.dump(sorted(seen), open(SEEN_FILE, "w", encoding="utf-8"))
    except Exception as e:
        print(f"[seen kaydedilemedi] {e}")


# ---------------------------------------------------------------- run
def run(first):
    for k, v in [("OPENROUTER_API_KEY", OPENROUTER_KEY), ("SERPER_API_KEY", SERPER_KEY),
                 ("NOTION_TOKEN", NOTION_TOKEN)]:
        if not v:
            sys.exit(f"Eksik env: {k}")
    db_id = ensure_notion_db()

    seen = load_seen()

    # AppSumo'dan kategori kategori topla, slug ile tekille
    pool, pool_ids = [], set()
    for q in APPSUMO_QUERIES:
        for d in fetch_appsumo(q):
            if d["pid"] and d["pid"] not in pool_ids:
                pool_ids.add(d["pid"]); pool.append(d)
        time.sleep(0.5)
    products = [p for p in pool if p["pid"] not in seen][:first]
    print(f"AppSumo: {len(pool)} tekil deal bulundu, {len(products)} yeni işlenecek.")

    counts = {"PASS": 0, "İNCELE": 0, "DROP": 0}
    processed = 0

    for p in products:
        pid = p["pid"]
        seen.add(pid)
        processed += 1
        print(f"\n• {p['name']}  ({pid})")

        # GATE 1 — duvar ön-elemesi (tanık+yargıç; denetim izi moat_reason'da)
        vectors, moat_reason = gate_moat(p)
        if not vectors:
            status, reason = "DROP", f"yerel duvar yok | denetim: {moat_reason}"
            counts[status] += 1
            print(f"  -> {status}: {reason}")
            continue
        print(f"  duvar: {vectors}")

        # GATE 2 — tr_gap (sadece moat-pozitifler; pahalı kısım)
        gap, gap_reason, evidence = gate_tr_gap(p)
        print(f"  tr_gap: {gap} — {gap_reason}")

        # GATE 3 — traction: AppSumo deal'i zaten KANITLANMIŞ ücretli talep
        paid = True
        paid_note = f"AppSumo Lifetime Deal (kalan kod: {p.get('codes')})"

        status, reason = decide_status(pid, vectors, gap, paid)

        # İKİNCİ AŞAMA: Motor PASS → Gemini'ye sor, çıktısını Notion kaydına ekle. Karar Şeref'in.
        gemini_note = ""
        if status == "PASS":
            print("  [PASS → Gemini kontrol ediyor...]")
            try:
                ss_res = second_stage_verdict(p, second_stage_gather(p)) or {}
            except Exception as e:
                ss_res = {"var_mi": "HATA", "gerekce": str(e)}
            rakip_str = "; ".join(f"{r.get('ad','')} ({r.get('domain','')})"
                                  for r in ss_res.get("rakipler", [])) or "-"
            gemini_note = (f"GEMINI → VAR MI: {ss_res.get('var_mi','?')} | "
                           f"Rakipler: {rakip_str} | "
                           f"Kanıt: {str(ss_res.get('en_guclu_kanit','-'))[:400]} | "
                           f"Gerekçe: {str(ss_res.get('gerekce','-'))[:400]}")
            print("  ── Gemini ──────────────────────────────────")
            print(f"  VAR MI  : {ss_res.get('var_mi', '?')}")
            for r in ss_res.get("rakipler", []):
                print(f"  Rakip   : {r.get('ad', '')} | {r.get('domain', '')}")
            print(f"  Kanıt   : {ss_res.get('en_guclu_kanit', '?')}")
            print(f"  Gerekçe : {ss_res.get('gerekce', '?')}")
            print("  ─────────────────────────────────────────────")

        counts[status] += 1
        full_reason = f"{reason} || duvar:{moat_reason} | gap:{gap_reason} | para:{paid_note}"
        if gemini_note:
            full_reason = f"{gemini_note} || {full_reason}"
        print(f"  -> {status}: {reason}")
        if status in ("PASS", "İNCELE"):  # İNCELE artık görünür: 'emin değilim, bak' dediği ürünler logda kaybolmasın
            notion_write(db_id, pid, p, vectors, gap, paid, status, full_reason, evidence)

    save_seen(seen)
    print(f"\n=== ÖZET: işlenen {processed} | PASS {counts['PASS']} | "
          f"İNCELE {counts['İNCELE']} | DROP {counts['DROP']}")

    # KURAL: 0 survivor = motor hatası alarmı, piyasa kararı değil.
    if processed >= 10 and counts["PASS"] == 0 and counts["İNCELE"] == 0:
        print("⚠ ALARM: 0 survivor. Gate'ler fazla katı / duvar tespiti kırık olabilir — "
              "MOTORU kontrol et, piyasayı suçlama.")


if __name__ == "__main__":
    ap = argparse.ArgumentParser()
    ap.add_argument("--first", type=int, default=40, help="işlenecek yeni deal üst sınırı")
    a = ap.parse_args()
    run(a.first)
