"""
DOESP Monitor v5.2 — Diário Oficial do Estado de São Paulo
===========================================================
v5.1 showed: the sumário page is a SPA that requires CLICKS, not just URL params.
Body text was only 323 chars = just the nav shell (date picker + caderno tabs).
The content only loads after: Click caderno → Click section → content appears.

FIX: Playwright clicks through the navigation:
  1. Open doe.sp.gov.br/sumario
  2. Click caderno tab ("Executivo" / "Municípios")
  3. Wait → sections appear ("Atos Normativos", "Atos de Pessoal", etc.)
  4. Click section
  5. Wait → content/PDF loads → intercept API calls with edition UUIDs
  6. Read content from DOM OR download PDF with discovered UUID
"""

import requests, datetime, os, sys, re, json, unicodedata, io, time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
if not TELEGRAM_TOKEN or not CHAT_ID:
    print("FATAL: TELEGRAM_TOKEN ou CHAT_ID ausentes."); sys.exit(1)

CADERNOS = [
    {"journalName":"Executivo",  "rootSectionName":"Atos Normativos",
     "label":"Normativos", "emoji":"📋"},
    {"journalName":"Executivo",  "rootSectionName":"Atos de Pessoal",
     "label":"Pessoal",    "emoji":"👤"},
    {"journalName":"Executivo",  "rootSectionName":"Atos de Gestão e Despesas",
     "label":"Gestão",     "emoji":"💼"},
    {"journalName":"Municípios", "rootSectionName":"Atos Municipais",
     "label":"Municípios", "emoji":"🏛️"},
]

SOURCE_NAME  = "DOESP"
SOURCE_EMOJI = "📋"
PORTAL_URL   = "https://doe.sp.gov.br/sumario"
PDF_API      = "https://do-api-publication-pdf.doe.sp.gov.br"
SEARCH_API   = "https://do-api-web-search.doe.sp.gov.br"
MAX_PDF_PAGES = 20
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

KEYWORD_CATEGORIES = {
    "contratação emergencial":"urgencia","organização social de saúde":"saude",
    "contrato de gestão":"saude","hospital das clínicas":"saude",
    "leito de UTI":"saude","medicamento de alto custo":"saude",
    "improbidade administrativa":"investigativo","superfaturamento":"investigativo",
    "sobrepreço":"investigativo","fraude em licitação":"investigativo",
    "desvio de verba":"investigativo","dispensa de licitação":"licitacao",
    "inexigibilidade de licitação":"licitacao","licitação deserta":"licitacao",
    "concorrência eletrônica":"licitacao","extrato de contrato":"contrato",
    "rescisão de contrato":"contrato","termo de aditamento":"contrato",
    "obra paralisada":"obras","habitação de interesse social":"obras",
    "unidades habitacionais":"obras","saneamento básico":"obras",
    "pavimentação":"obras","recapeamento asfáltico":"obras",
    "canalização":"obras","concessão rodoviária":"obras",
    "demissão de servidor":"disciplinar","aposentadoria compulsória":"disciplinar",
    "processo administrativo disciplinar":"disciplinar","sindicância":"disciplinar",
    "aplicação de penalidade":"penalidade","multa contratual":"penalidade",
    "ação civil pública":"legal","merenda escolar":"educacao",
    "transporte escolar":"educacao","construção de escola estadual":"educacao",
    "dengue":"saude","operação policial":"seguranca",
    "unidade prisional":"seguranca","morte em custódia":"seguranca",
    "licença ambiental":"meio_ambiente","auto de infração ambiental":"meio_ambiente",
    "CETESB":"meio_ambiente","área contaminada":"meio_ambiente",
    "crédito adicional suplementar":"orcamento",
    "nomeação para cargo em comissão":"pessoal",
    "exoneração a pedido":"pessoal","exoneração de servidor":"pessoal",
}
KEYWORDS = sorted(KEYWORD_CATEGORIES.keys(), key=len, reverse=True)

CATEGORY_TV = {
    "urgencia":(1,"🚨","Emergência"),"saude":(1,"🏥","Saúde"),
    "investigativo":(1,"🔎","Investigativo"),"obras":(2,"🏗️","Obras"),
    "licitacao":(2,"🛒","Licitação"),"contrato":(2,"📝","Contrato"),
    "disciplinar":(2,"⚖️","Disciplinar"),"penalidade":(2,"⚖️","Penalidade"),
    "educacao":(2,"🎓","Educação"),"seguranca":(2,"🚔","Segurança"),
    "legal":(2,"🏛️","Judicial"),"meio_ambiente":(2,"🌿","Meio Ambiente"),
    "orcamento":(3,"💼","Orçamento"),"pessoal":(3,"👤","Pessoal"),
    "general":(3,"🔍","Geral"),
}

def normalize(t):
    return "".join(c for c in unicodedata.normalize("NFKD",t)
                   if not unicodedata.combining(c)).lower()
def parse_brl(s):
    if not s: return 0.0
    m=re.search(r"[\d.,]+",s)
    if not m: return 0.0
    v=re.sub(r"\.(?=\d{3}(\D|$))","",m.group()).replace(",",".")
    try: return float(v)
    except: return 0.0
def caderno_url(jn, rsn):
    return (f"{PORTAL_URL}?journalName={requests.utils.quote(jn)}"
            f"&rootSectionName={requests.utils.quote(rsn)}")

# ===========================================================================
# PLAYWRIGHT: CLICK-BASED NAVIGATION
# ===========================================================================
def navigate_and_read(browser, caderno, date_str):
    """
    Open the sumário page, CLICK through caderno → section, intercept
    all API calls and read the rendered content after each click.
    Returns (body_text, edition_uuid, all_api_responses).
    """
    jn  = caderno["journalName"]
    rsn = caderno["rootSectionName"]
    lbl = caderno["label"]

    api_responses = []
    pdf_urls      = []

    def on_response(response):
        try:
            url = response.url; ct = response.headers.get("content-type","")
            if response.status == 200 and "json" in ct:
                try:
                    data = response.json()
                    api_responses.append({"url": url, "data": data})
                    summary = json.dumps(data, ensure_ascii=False)[:500]
                    print(f"    API [{url[-65:]}]")
                    print(f"      → {summary}")
                except: pass
            if response.status == 200 and "pdf" in ct:
                pdf_urls.append(url)
                print(f"    PDF response: {url[-70:]}")
            # Also catch edition UUIDs in URL paths
            if "editions/" in url or "edition/" in url:
                m = UUID_RE.search(url)
                if m: print(f"    Edition URL: {url[-70:]} → {m.group(0)}")
        except: pass

    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="pt-BR")
    page = ctx.new_page()
    page.on("response", on_response)

    try:
        # Step 1: load the base sumário page
        print(f"  [{lbl}] Step 1: loading {PORTAL_URL}")
        page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)
        body1 = page.inner_text("body")
        print(f"  [{lbl}] After load: {len(body1)} chars")

        # Step 2: click the CADERNO tab
        # The caderno names on the page: Executivo, Legislativo, Municípios, Empresarial
        print(f"  [{lbl}] Step 2: clicking caderno '{jn}'")
        clicked = False
        for selector in [
            f"text='{jn}'",
            f"button:has-text('{jn}')",
            f"a:has-text('{jn}')",
            f"div:has-text('{jn}') >> nth=0",
            f"span:has-text('{jn}')",
            f"li:has-text('{jn}')",
        ]:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    loc.first.click()
                    clicked = True
                    print(f"  [{lbl}] Clicked: {selector} (count={loc.count()})")
                    break
            except Exception as e:
                continue

        if not clicked:
            # Try clicking by exact text match in all clickable elements
            print(f"  [{lbl}] Trying fallback click...")
            all_clickable = page.query_selector_all("a, button, div[role=tab], li[role=tab], span[role=button]")
            for el in all_clickable:
                txt = (el.inner_text() or "").strip()
                if txt == jn or jn in txt:
                    el.click()
                    clicked = True
                    print(f"  [{lbl}] Clicked element with text: '{txt[:30]}'")
                    break

        if not clicked:
            print(f"  [{lbl}] ⚠️ Could not find caderno tab '{jn}' to click")
            # Dump all visible text elements for debugging
            visible = page.inner_text("body")
            print(f"  [{lbl}] Page text: {visible[:400]}")
            ctx.close()
            return "", None, api_responses

        page.wait_for_timeout(4000)
        body2 = page.inner_text("body")
        print(f"  [{lbl}] After caderno click: {len(body2)} chars")
        print(f"  [{lbl}] Preview: {body2[:500].replace(chr(10),' | ')}")

        # Step 3: click the SECTION (rootSectionName)
        # After clicking caderno, sections should appear
        print(f"  [{lbl}] Step 3: clicking section '{rsn}'")
        section_clicked = False

        # Try exact text match first
        for selector in [
            f"text='{rsn}'",
            f"a:has-text('{rsn}')",
            f"button:has-text('{rsn}')",
            f"div:has-text('{rsn}') >> nth=0",
            f"span:has-text('{rsn}')",
        ]:
            try:
                loc = page.locator(selector)
                if loc.count() > 0:
                    loc.first.click()
                    section_clicked = True
                    print(f"  [{lbl}] Clicked section: {selector}")
                    break
            except: continue

        if not section_clicked:
            # Sections might have slightly different names; try partial match
            # e.g., "Atos Normativos" might be displayed as "ATOS NORMATIVOS"
            rsn_words = rsn.split()
            for el in page.query_selector_all("a, button, div, span, li"):
                txt = (el.inner_text() or "").strip()
                if all(w.lower() in txt.lower() for w in rsn_words) and len(txt) < 80:
                    try:
                        el.click()
                        section_clicked = True
                        print(f"  [{lbl}] Clicked section element: '{txt[:50]}'")
                        break
                    except: continue

        if section_clicked:
            page.wait_for_timeout(5000)
            body3 = page.inner_text("body")
            print(f"  [{lbl}] After section click: {len(body3)} chars")
            print(f"  [{lbl}] Preview: {body3[:500].replace(chr(10),' | ')}")
        else:
            print(f"  [{lbl}] ⚠️ Section '{rsn}' not found to click")
            body3 = body2

        # Step 4: look for PDF download links / iframe / content
        # After clicking section, there might be a PDF viewer or download link
        dom = page.content()

        # Find hrefs with UUIDs (potential PDF downloads)
        uuid_hrefs = []
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            m = UUID_RE.search(href)
            if m:
                link_text = (a.inner_text() or "")[:40]
                uuid_hrefs.append({"href":href, "text":link_text, "uuid":m.group(0)})
        if uuid_hrefs:
            print(f"  [{lbl}] UUID links found: {len(uuid_hrefs)}")
            for h in uuid_hrefs[:6]:
                print(f"    {h['uuid']} | {h['text'][:25]} | ...{h['href'][-50:]}")

        # Find iframes (PDF viewer)
        for iframe in page.query_selector_all("iframe"):
            src = iframe.get_attribute("src") or ""
            if src:
                print(f"  [{lbl}] iframe src: {src[:80]}")
                if UUID_RE.search(src): pdf_urls.append(src)

        # Extract the edition UUID from intercepted API responses
        edition_uuid = _find_edition_uuid(api_responses, jn, rsn, uuid_hrefs, pdf_urls)

        # Final body text for scanning
        final_text = body3 if len(body3) > len(body2) else body2

        ctx.close()
        return final_text, edition_uuid, api_responses

    except Exception as e:
        print(f"  [{lbl}] PW error: {e}")
        import traceback; traceback.print_exc()
        ctx.close()
        return "", None, []

def _find_edition_uuid(api_responses, jn, rsn, uuid_hrefs, pdf_urls):
    """
    From all intercepted data, find the edition UUID for this caderno+section.
    The API responses after clicking should contain edition-level UUIDs.
    """
    # Check PDF URLs first (most reliable — direct PDF link)
    for url in pdf_urls:
        m = UUID_RE.search(url)
        if m:
            print(f"  Edition UUID from PDF URL: {m.group(0)}")
            return m.group(0)

    # Check UUID hrefs that look like edition/PDF downloads
    for h in uuid_hrefs:
        href = h["href"].lower()
        if any(kw in href for kw in ["edition","pdf","download","publication"]):
            print(f"  Edition UUID from href: {h['uuid']}")
            return h["uuid"]

    # Check API responses for edition-level data
    # Look for responses containing "edition", "rootSection", "section" keys
    jn_low  = jn.lower()
    rsn_low = rsn.lower()
    for resp in api_responses:
        raw = json.dumps(resp["data"], ensure_ascii=False).lower()
        # Skip the journals list (already seen)
        if "/journals" in resp["url"] and "edition" not in raw:
            continue
        # This response has edition data
        if "edition" in raw or "section" in raw or rsn_low in raw:
            uuids = UUID_RE.findall(json.dumps(resp["data"]))
            # Filter out known journal IDs
            journal_ids = set()
            for r2 in api_responses:
                if isinstance(r2["data"], dict) and "items" in r2["data"]:
                    for item in r2["data"]["items"]:
                        if "id" in item: journal_ids.add(item["id"])
            edition_uuids = [u for u in uuids if u not in journal_ids]
            if edition_uuids:
                print(f"  Edition UUID from API: {edition_uuids[0]} (from {resp['url'][-50:]})")
                return edition_uuids[0]

    # Last resort: return UUID from href that isn't a journal ID
    journal_ids = set()
    for resp in api_responses:
        if isinstance(resp["data"], dict) and "items" in resp["data"]:
            for item in resp["data"]["items"]:
                if "id" in item: journal_ids.add(item["id"])
    for h in uuid_hrefs:
        if h["uuid"] not in journal_ids:
            print(f"  Edition UUID from non-journal href: {h['uuid']}")
            return h["uuid"]

    print(f"  ⚠️ No edition UUID found in intercepted data")
    return None

# ===========================================================================
# PDF DOWNLOAD + TEXT EXTRACTION
# ===========================================================================
def download_pdf(session, uuid):
    # Try multiple download endpoints
    endpoints = [
        f"{PDF_API}/v1/editions/{uuid}",
        f"{PDF_API}/v2/editions/{uuid}",
        f"{SEARCH_API}/v2/publications/attachment/downloadattachment/{uuid}",
    ]
    for url in endpoints:
        try:
            r = session.get(url, timeout=120, stream=True)
            if r.status_code == 200:
                data = b"".join(r.iter_content(65536))
                if data[:4] == b"%PDF":
                    print(f"  PDF OK from {url[-60:]} ({len(data)/1e6:.1f}MB)")
                    return data
        except: pass
    print(f"  PDF: all endpoints failed for {uuid}")
    return None

def extract_text(pdf_bytes, label=""):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        parts = []
        for i, pg in enumerate(extract_pages(io.BytesIO(pdf_bytes), laparams=lp)):
            if i >= MAX_PDF_PAGES: break
            mid = pg.width * 0.52
            boxes = [(el.bbox[3],(el.bbox[0]+el.bbox[2])/2,el.get_text())
                     for el in pg if isinstance(el, LTTextBox) and el.get_text().strip()]
            left  = sorted([(y,t) for y,x,t in boxes if x<mid], reverse=True)
            right = sorted([(y,t) for y,x,t in boxes if x>=mid], reverse=True)
            parts.append("\n".join(t.strip() for _,t in left+right))
            parts.append("\x0c")
        result = "\n".join(parts)
        print(f"  text {len(result):,} chars ({label}, {MAX_PDF_PAGES}pp max)")
        return result
    except Exception as e:
        print(f"  pdfminer err: {e}"); return ""

# ===========================================================================
# SCAN + SCORE
# ===========================================================================
def scan_text(full_text, caderno, date_str):
    fn = normalize(full_text)
    lbl = caderno["label"]; jn = caderno["journalName"]; rsn = caderno["rootSectionName"]
    pb = [(m.start(), f"p.{i+2}") for i,m in enumerate(re.finditer(r"\x0c", full_text))]
    def pag(pos):
        p="p.1"
        for pp,pl in pb:
            if pp<=pos: p=pl
            else: break
        return p
    results=[]; seen=set(); kw_cnt={}
    for kw in KEYWORDS:
        kn=normalize(kw); sp=0
        while True:
            pos=fn.find(kn,sp)
            if pos==-1: break
            sp=pos+max(len(kn),400)
            pg=pag(pos); dedup=(kw,pg)
            if dedup in seen: continue
            seen.add(dedup)
            window=re.sub(r"\s+"," ",full_text[max(0,pos-200):pos+400]).strip()
            doc=""
            before=full_text[max(0,pos-1000):pos]
            for pat in [r'((?:PORTARIA|RESOLUÇÃO|RESOLUCAO|DECRETO|DESPACHO'
                        r'|EDITAL|COMUNICADO|EXTRATO)\s+[^\n]{8,100})']:
                m=re.findall(pat,before,re.I)
                if m: doc=m[-1].strip()[:120]; break
            ref={"keyword":kw,"label":lbl,"journal":jn,"section":rsn,
                 "title":doc,"page":pg,"excerpt":window[:350],"date":date_str}
            v,sc,ft=score_ref(ref,normalize(window))
            results.append({"ref":ref,"veredito":v,"score":sc,"fatores":ft,"keyword":kw})
            kw_cnt[kw]=kw_cnt.get(kw,0)+1
    for kw,n in sorted(kw_cnt.items(),key=lambda x:-x[1])[:10]:
        print(f"    '{kw}': {n}")
    ap=sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr=sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} hits | 🟢{ap} 🟡{pr}")
    return results

def score_ref(ref, excerpt_low):
    kw=ref["keyword"]; cat=KEYWORD_CATEGORIES.get(kw,"general")
    tier=CATEGORY_TV.get(cat,(3,"",""))[0]
    score={1:4,2:2,3:0}.get(tier,0); fatores=[]
    money=re.search(r"r\$\s*([\d.,]+)",excerpt_low)
    if money:
        amt=parse_brl(money.group(1))
        if amt>=10_000_000: score+=4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=1_000_000: score+=3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt>=100_000: score+=1; fatores.append(f"R${amt/1e3:.0f}k")
    if any(t in excerpt_low for t in ["emergencial","urgente"]): score+=3; fatores.append("EMERGENCIAL")
    if any(t in excerpt_low for t in ["superfaturamento","sobrepreço","fraude","improbidade"]):
        score+=4; fatores.append("SUSPEITO")
    if any(t in excerpt_low for t in ["hospital das clínicas","leito de uti"]):
        score+=2; fatores.append("SAÚDE")
    if any(t in excerpt_low for t in ["unidade prisional","policial penal"]):
        score+=1; fatores.append("PENITENCIÁRIO")
    if any(t in excerpt_low for t in ["demissão","suspensão por","aposentadoria compulsória"]):
        score+=2; fatores.append("SANÇÃO FUNCIONAL")
    if ref.get("title"): score+=1; fatores.append("TÍTULO")
    if ref.get("page"): score+=1; fatores.append(f"{ref['page']}")
    if score>=8: v="🟢 APROVADA"
    elif score>=5: v="🟡 PODE RENDER"
    else: v="🔴 BACKGROUND"
    return v,score,fatores

# ===========================================================================
# CARDS + SUMMARY
# ===========================================================================
def build_ref_card(ref, date_str, veredito, score, fatores):
    cat=KEYWORD_CATEGORIES.get(ref["keyword"],"general")
    _,icon,cat_nome=CATEGORY_TV.get(cat,(3,"🔍","Geral"))
    lbl=ref.get("label",""); emo="📋"
    for c in CADERNOS:
        if c["label"]==lbl: emo=c["emoji"]; break
    fstr=" · ".join(fatores) if fatores else "—"
    link=caderno_url(ref.get("journal","Executivo"),ref.get("section","Atos Normativos"))
    lines=[
        f"{SOURCE_EMOJI} *DOESP {date_str}* | {emo} {lbl}",
        f"{veredito} | {icon} *{cat_nome}*",
        f"🔑 `{ref['keyword']}` | Score {score} | {fstr}",
        "─"*22,
    ]
    if ref.get("title"): lines.append(f"📄 *{ref['title'][:120]}*")
    if ref.get("page"):  lines.append(f"📖 {ref['page']}")
    if ref.get("excerpt"):
        hi=re.sub(f"(?i)({re.escape(ref['keyword'])})",r"*\1*",ref["excerpt"][:300])
        lines.append(f"💬 _{hi}_")
    lines+=["─"*22,f"🔗 [Portal]({link})"]
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str):
    all_h=[h for v in results_by_caderno.values() for h in v]
    total=len(all_h)
    ap=sum(1 for h in all_h if h["veredito"].startswith("🟢"))
    pr=sum(1 for h in all_h if h["veredito"].startswith("🟡"))
    lines=[f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
           f"📋 *{total} resultado(s)*",
           f"🟢 {ap}  🟡 {pr}  🔴 {total-ap-pr}\n"]
    for c in CADERNOS:
        lbl=c["label"]; hits=results_by_caderno.get(lbl,[])
        a=sum(1 for h in hits if h["veredito"].startswith("🟢"))
        p=sum(1 for h in hits if h["veredito"].startswith("🟡"))
        lines.append(f"{c['emoji']} *{lbl}*: {len(hits)} | 🟢{a} 🟡{p}" if hits
                     else f"{c['emoji']} *{lbl}*: —")
    lines.append("━"*20)
    for h in sorted(all_h,key=lambda x:-x["score"])[:10]:
        cat=KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
        r=h["ref"]
        lines.append(f"{h['veredito'][:2]} {icon} `{r['keyword'][:28]}` [{r.get('label','')}] {r.get('page','')}")
    lines+=["━"*20,f"🔗 [Portal]({PORTAL_URL})"]
    return "\n".join(lines)

# ===========================================================================
# TELEGRAM
# ===========================================================================
_last_send=0.0
def send_telegram(text, silent=False):
    global _last_send
    gap=time.time()-_last_send
    if gap<2.0: time.sleep(2.0-gap)
    for _ in range(3):
        try:
            r=requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown",
                      "disable_web_page_preview":True,"disable_notification":silent},timeout=15)
            _last_send=time.time()
            if r.status_code==200: return True
            if r.status_code==429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1); continue
            print(f"  TG {r.status_code}"); return False
        except Exception as e: print(f"  TG err: {e}"); time.sleep(3)
    return False
def split_long(text, mx=3800):
    if len(text)<=mx: return [text]
    parts=[]; cur=""
    for l in text.split("\n"):
        if len(cur)+len(l)+1>mx: parts.append(cur); cur=l
        else: cur+=("\n" if cur else "")+l
    if cur: parts.append(cur)
    return parts

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    hoje = datetime.date.today()
    date_str = hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v5.2 — {date_str} ===\n")

    from playwright.sync_api import sync_playwright
    print("  Playwright: ✅\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept":"*/*","Accept-Language":"pt-BR,pt;q=0.9",
    })

    results_by_caderno = {}

    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox",
                  "--disable-dev-shm-usage","--disable-gpu"])

        for caderno in CADERNOS:
            lbl, emo = caderno["label"], caderno["emoji"]
            print(f"\n{'─'*60}")
            print(f"{emo}  {caderno['journalName']} / {caderno['rootSectionName']}")
            print(f"{'─'*60}")

            body_text, edition_uuid, api_data = \
                navigate_and_read(browser, caderno, date_str)

            hits = []

            # Strategy A: if body has enough content, scan it
            if len(body_text) > 2000:
                print(f"  [{lbl}] Scanning DOM text ({len(body_text):,} chars)...")
                hits = scan_text(body_text, caderno, date_str)

            # Strategy B: if edition UUID found, download PDF
            if not hits and edition_uuid:
                print(f"  [{lbl}] Downloading PDF for {edition_uuid}...")
                pdf = download_pdf(session, edition_uuid)
                if pdf:
                    text = extract_text(pdf, lbl)
                    if text and len(text) > 500:
                        hits = scan_text(text, caderno, date_str)

            if not hits:
                msg = (f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\n"
                       f"Conteúdo inacessível\n"
                       f"🔗 [Verificar]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
                print(f"  [{lbl}] ⚠️ Sem resultados")
                send_telegram(msg)

            results_by_caderno[lbl] = hits
            time.sleep(2)

        browser.close()

    total = sum(len(v) for v in results_by_caderno.values())
    all_hits = [h for v in results_by_caderno.values() for h in v]
    print(f"\n{'='*60}\nTOTAL: {total}")

    if total == 0: return

    send_telegram(build_summary(results_by_caderno, date_str))
    time.sleep(1)

    aprovadas   = sorted([h for h in all_hits if h["veredito"].startswith("🟢")],key=lambda x:-x["score"])
    pode_render = sorted([h for h in all_hits if h["veredito"].startswith("🟡")],key=lambda x:-x["score"])
    background  =        [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas+pode_render:
        card = build_ref_card(h["ref"],date_str,h["veredito"],h["score"],h["fatores"])
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines=[f"🗂️ *Background — {date_str}* — {len(background)} ref(s)"]
        for h in background[:20]:
            cat=KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
            r=h["ref"]
            lines.append(f"{icon} `{h['keyword'][:28]}` [{r.get('label','')}] {r.get('page','')}")
        send_telegram("\n".join(lines), silent=True)

if __name__=="__main__":
    main()
