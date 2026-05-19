"""
DOESP Monitor v5.1 — Diário Oficial do Estado de São Paulo
===========================================================
Playwright reads the rendered page DOM directly.
No PDF download. No UUID guessing. No API reverse-engineering.

APPROACH:
  1. Playwright opens each caderno sumario page
  2. Intercepts /v2/journals API → logs FULL response (diagnostic)
  3. Waits for page to render → reads DOM text
  4. If DOM has enough content → scan for keywords → report references
  5. If DOM is thin (TOC only) → look for PDF download links → try UUID
  6. Also: capture all href values containing UUIDs from rendered DOM

Secrets: TELEGRAM_TOKEN, CHAT_ID
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
# PLAYWRIGHT: read the rendered page
# ===========================================================================
def process_caderno_playwright(browser, caderno, date_str):
    """
    Navigate to the caderno sumário page. Wait for JS to render.
    Read the rendered DOM for text and links. Try to find content.
    Returns (text, pdf_uuids, api_data) for downstream processing.
    """
    jn  = caderno["journalName"]
    rsn = caderno["rootSectionName"]
    lbl = caderno["label"]
    url = caderno_url(jn, rsn)

    api_responses = []   # full API responses for diagnosis
    pdf_hrefs     = []   # PDF download hrefs found in DOM

    def handle_response(response):
        try:
            resp_url = response.url
            ct = response.headers.get("content-type","")
            if response.status == 200 and "json" in ct:
                try:
                    body = response.json()
                    api_responses.append({"url": resp_url, "data": body})
                    # Print full structure for diagnosis (first 600 chars)
                    print(f"  API JSON [{resp_url[-60:]}]")
                    print(f"    → {json.dumps(body, ensure_ascii=False)[:600]}")
                except: pass
            elif response.status == 200 and "pdf" in ct:
                print(f"  API PDF [{resp_url[-60:]}]")
                pdf_hrefs.append(resp_url)
        except: pass

    print(f"  [{lbl}] Playwright → {url}")
    ctx = browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="pt-BR")
    page = ctx.new_page()
    page.on("response", handle_response)

    try:
        page.goto(url, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(5000)

        # Read the full DOM HTML
        dom = page.content()
        print(f"  [{lbl}] Rendered DOM: {len(dom):,} chars")

        # Read visible text
        body_text = page.inner_text("body")
        print(f"  [{lbl}] Body text: {len(body_text):,} chars")
        # Show first 600 chars (to understand page structure)
        preview = body_text[:600].replace("\n"," | ")
        print(f"  [{lbl}] Preview: {preview}")

        # Find ALL links that contain UUIDs (potential PDF download links)
        hrefs_with_uuid = []
        for a in page.query_selector_all("a[href]"):
            href = a.get_attribute("href") or ""
            m = UUID_RE.search(href)
            if m:
                link_text = (a.inner_text() or "")[:50]
                hrefs_with_uuid.append({"href": href, "text": link_text, "uuid": m.group(0)})
        if hrefs_with_uuid:
            print(f"  [{lbl}] Links with UUID: {len(hrefs_with_uuid)}")
            for h in hrefs_with_uuid[:8]:
                print(f"    → {h['uuid']} | {h['text'][:30]} | {h['href'][-60:]}")

        # Find links to sections/pages (for deeper navigation)
        section_links = []
        for a in page.query_selector_all("a[href*=sumario], a[href*=leitura], a[href*=caderno]"):
            href = a.get_attribute("href") or ""
            text = (a.inner_text() or "")[:60]
            if text.strip():
                section_links.append({"href": href, "text": text})
        if section_links:
            print(f"  [{lbl}] Section links: {len(section_links)}")
            for s in section_links[:6]:
                print(f"    → {s['text'][:40]} | {s['href'][:60]}")

        # Also look for iframe or embedded PDF viewer
        iframes = page.query_selector_all("iframe")
        if iframes:
            for iframe in iframes:
                src = iframe.get_attribute("src") or ""
                print(f"  [{lbl}] iframe: {src[:80]}")
                if UUID_RE.search(src):
                    pdf_hrefs.append(src)

        ctx.close()

        return body_text, hrefs_with_uuid, api_responses, pdf_hrefs

    except Exception as e:
        print(f"  [{lbl}] PW error: {e}")
        ctx.close()
        return "", [], [], []

# ===========================================================================
# SCAN TEXT FOR KEYWORDS
# ===========================================================================
def scan_text(full_text, caderno, date_str):
    fn = normalize(full_text)
    lbl = caderno["label"]; jn = caderno["journalName"]; rsn = caderno["rootSectionName"]

    # Page detection (form-feed or "Página X" markers)
    page_markers = [(m.start(), m.group(0))
                    for m in re.finditer(r'(?:\x0c|Página\s+(\d+))', full_text)]
    def pag(pos):
        p = "p.1"
        for pm_pos, pm_text in page_markers:
            if pm_pos <= pos:
                m = re.search(r'\d+', pm_text)
                p = f"p.{m.group(0)}" if m else p
            else: break
        return p

    results = []; seen = set(); kw_cnt = {}
    for kw in KEYWORDS:
        kn = normalize(kw); sp = 0
        while True:
            pos = fn.find(kn, sp)
            if pos == -1: break
            sp = pos + max(len(kn), 400)
            pg = pag(pos)
            dedup = (kw, pg)
            if dedup in seen: continue
            seen.add(dedup)
            window = re.sub(r"\s+", " ", full_text[max(0,pos-200):pos+400]).strip()
            # Nearest document header
            doc_header = ""
            before = full_text[max(0,pos-1000):pos]
            for pat in [r'((?:PORTARIA|RESOLUÇÃO|RESOLUCAO|DECRETO|DESPACHO'
                        r'|EDITAL|COMUNICADO|EXTRATO)\s+[^\n]{8,100})']:
                m = re.findall(pat, before, re.I)
                if m: doc_header = m[-1].strip()[:120]; break
            ref = {"keyword":kw,"label":lbl,"journal":jn,"section":rsn,
                   "title":doc_header,"page":pg,"excerpt":window[:350],
                   "date":date_str}
            v,sc,ft = score_ref(ref, normalize(window))
            results.append({"ref":ref,"veredito":v,"score":sc,"fatores":ft,"keyword":kw})
            kw_cnt[kw] = kw_cnt.get(kw,0)+1

    for kw,n in sorted(kw_cnt.items(), key=lambda x:-x[1])[:12]:
        print(f"    '{kw}': {n}")
    ap=sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr=sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} hits | 🟢{ap} 🟡{pr}")
    return results

def score_ref(ref, excerpt_low):
    keyword=ref["keyword"]; cat=KEYWORD_CATEGORIES.get(keyword,"general")
    tier=CATEGORY_TV.get(cat,(3,"",""))[0]
    score={1:4,2:2,3:0}.get(tier,0); fatores=[]
    money=re.search(r"r\$\s*([\d.,]+)",excerpt_low)
    if money:
        amt=parse_brl(money.group(1))
        if amt>=10_000_000: score+=4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=1_000_000: score+=3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt>=100_000: score+=1; fatores.append(f"R${amt/1e3:.0f}k")
    if any(t in excerpt_low for t in ["emergencial","urgente","urgência"]):
        score+=3; fatores.append("EMERGENCIAL")
    if any(t in excerpt_low for t in ["superfaturamento","sobrepreço","fraude","improbidade"]):
        score+=4; fatores.append("SUSPEITO")
    if any(t in excerpt_low for t in ["hospital das clínicas","leito de uti","organização social de saúde"]):
        score+=2; fatores.append("SAÚDE")
    if any(t in excerpt_low for t in ["unidade prisional","policial penal","penitenciária"]):
        score+=1; fatores.append("PENITENCIÁRIO")
    if any(t in excerpt_low for t in ["demissão","suspensão por","aposentadoria compulsória"]):
        score+=2; fatores.append("SANÇÃO FUNCIONAL")
    if ref.get("title"): score+=1; fatores.append("TÍTULO")
    if ref.get("page"): score+=1; fatores.append(f"p.{ref['page']}")
    if score>=8: v="🟢 APROVADA"
    elif score>=5: v="🟡 PODE RENDER"
    else: v="🔴 BACKGROUND"
    return v,score,fatores

# ===========================================================================
# REFERENCE CARD + SUMMARY
# ===========================================================================
def build_ref_card(ref, date_str, veredito, score, fatores):
    cat=KEYWORD_CATEGORIES.get(ref["keyword"],"general")
    _,icon,cat_nome=CATEGORY_TV.get(cat,(3,"🔍","Geral"))
    jn=ref.get("journal",""); rsn=ref.get("section","")
    lbl=next((c["label"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn),rsn or jn)
    emo=next((c["emoji"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn),"📋")
    fator_str=" · ".join(fatores) if fatores else "—"
    link=caderno_url(jn or "Executivo",rsn or "Atos Normativos")
    lines=[
        f"{SOURCE_EMOJI} *DOESP {date_str}* | {emo} {lbl}",
        f"{veredito} | {icon} *{cat_nome}*",
        f"🔑 `{ref['keyword']}` | Score {score} | {fator_str}",
        "─"*22,
    ]
    if ref.get("title"): lines.append(f"📄 *{ref['title'][:120]}*")
    if ref.get("page"):  lines.append(f"📖 Página {ref['page']}")
    if ref.get("excerpt"):
        hi=re.sub(f"(?i)({re.escape(ref['keyword'])})",r"*\1*",ref["excerpt"][:300])
        lines.append(f"💬 _{hi}_")
    lines.append("─"*22)
    lines.append(f"🔗 [Abrir no portal]({link})")
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str):
    all_hits=[h for v in results_by_caderno.values() for h in v]
    total=len(all_hits)
    ap=sum(1 for h in all_hits if h.get("veredito","").startswith("🟢"))
    pr=sum(1 for h in all_hits if h.get("veredito","").startswith("🟡"))
    lines=[
        f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
        f"📋 *{total} resultado(s)*\n",
        f"🟢 Aprovadas: {ap}  🟡 Pode render: {pr}  🔴 Background: {total-ap-pr}\n",
    ]
    for cad in CADERNOS:
        lbl,emo=cad["label"],cad["emoji"]
        hits=results_by_caderno.get(lbl,[])
        a2=sum(1 for h in hits if h.get("veredito","").startswith("🟢"))
        p2=sum(1 for h in hits if h.get("veredito","").startswith("🟡"))
        lines.append(f"{emo} *{lbl}*: {len(hits)} hits | 🟢{a2} 🟡{p2}" if hits
                     else f"{emo} *{lbl}*: nenhum")
    lines.append("━"*20)
    for h in sorted(all_hits,key=lambda x:-x.get("score",0))[:10]:
        cat=KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
        pg=f" {h['ref'].get('page','')}"
        ttl=(h["ref"].get("title") or "")[:35]
        lines.append(f"{h.get('veredito','')[:2]} {icon} `{h['ref']['keyword'][:30]}`"
                     f" [{h['ref'].get('label','')}]{pg} {ttl}")
    lines+=["━"*20,f"\n🔗 [Portal DOESP]({PORTAL_URL})"]
    return "\n".join(lines)

# ===========================================================================
# PDF DOWNLOAD + TEXT EXTRACTION (used when DOM has a PDF UUID)
# ===========================================================================
def try_pdf_download(session, uuid, label=""):
    url=f"{PDF_API}/v1/editions/{uuid}"
    print(f"  PDF try {url[-60:]}")
    try:
        r=session.get(url,timeout=120,stream=True)
        if r.status_code!=200:
            # Also try v2
            url2=f"{PDF_API}/v2/editions/{uuid}"
            r=session.get(url2,timeout=120,stream=True)
            if r.status_code!=200:
                # Try web-search API download
                url3=f"{SEARCH_API}/v2/publications/attachment/downloadattachment/{uuid}"
                r=session.get(url3,timeout=120,stream=True)
                if r.status_code!=200:
                    print(f"  PDF all endpoints failed (last: {r.status_code})")
                    return None
        data=b"".join(r.iter_content(65536))
        if data[:4]!=b"%PDF":
            print(f"  Not PDF (first bytes: {data[:10]!r})")
            return None
        print(f"  PDF OK {len(data)/1e6:.1f}MB")
        return data
    except Exception as e:
        print(f"  PDF err: {e}"); return None

def extract_text_partial(pdf_bytes, label=""):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp=LAParams(line_margin=0.3,char_margin=2.0,word_margin=0.1,boxes_flow=None)
        parts=[]
        for i,page in enumerate(extract_pages(io.BytesIO(pdf_bytes),laparams=lp)):
            if i>=MAX_PDF_PAGES: break
            mid=page.width*0.52
            boxes=[(el.bbox[3],(el.bbox[0]+el.bbox[2])/2,el.get_text())
                   for el in page if isinstance(el,LTTextBox) and el.get_text().strip()]
            left=sorted([(y,t) for y,x,t in boxes if x<mid],reverse=True)
            right=sorted([(y,t) for y,x,t in boxes if x>=mid],reverse=True)
            parts.append("\n".join(t.strip() for _,t in left+right)); parts.append("\x0c")
        result="\n".join(parts)
        print(f"  texto {len(result):,} chars ({label}, {MAX_PDF_PAGES}pp)")
        return result
    except Exception as e:
        print(f"  pdfminer err: {e}"); return ""

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
                      "disable_web_page_preview":True,"disable_notification":silent},
                timeout=15)
            _last_send=time.time()
            if r.status_code==200: return True
            if r.status_code==429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1); continue
            print(f"  TG {r.status_code}"); return False
        except Exception as e: print(f"  TG err: {e}"); time.sleep(3)
    return False

def split_long(text, max_len=3800):
    if len(text)<=max_len: return [text]
    parts=[]; cur=""
    for line in text.split("\n"):
        if len(cur)+len(line)+1>max_len: parts.append(cur); cur=line
        else: cur+=("\n" if cur else "")+line
    if cur: parts.append(cur)
    return parts

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    hoje     = datetime.date.today()
    date_str = hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v5.1 — {date_str} ===\n")

    try:
        from playwright.sync_api import sync_playwright
        print("  Playwright: disponível ✅")
    except ImportError:
        print("FATAL: playwright não instalado. Instalar com:")
        print("  pip install playwright && playwright install chromium --with-deps")
        sys.exit(1)

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

            body_text, uuid_links, api_data, pdf_hrefs = \
                process_caderno_playwright(browser, caderno, date_str)

            hits = []

            # Strategy A: if the rendered page has enough text, scan it directly
            if len(body_text) > 3000:
                print(f"  [{lbl}] Scanning rendered text ({len(body_text):,} chars)...")
                hits = scan_text(body_text, caderno, date_str)

            # Strategy B: if PDF download links were found, try them
            if not hits and (pdf_hrefs or uuid_links):
                print(f"  [{lbl}] Trying PDF download from {len(pdf_hrefs)} PDF hrefs + {len(uuid_links)} UUID links...")
                tried = set()
                # PDF hrefs from network intercept
                for href in pdf_hrefs:
                    m = UUID_RE.search(href)
                    if m and m.group(0) not in tried:
                        tried.add(m.group(0))
                        pdf = try_pdf_download(session, m.group(0), lbl)
                        if pdf:
                            text = extract_text_partial(pdf, lbl)
                            if text and len(text)>500:
                                hits = scan_text(text, caderno, date_str)
                                if hits: break
                # UUID links from DOM
                for link in uuid_links:
                    uid = link["uuid"]
                    if uid in tried: continue
                    tried.add(uid)
                    # Try multiple download endpoints
                    pdf = try_pdf_download(session, uid, lbl)
                    if pdf:
                        text = extract_text_partial(pdf, lbl)
                        if text and len(text)>500:
                            hits = scan_text(text, caderno, date_str)
                            if hits: break
                    if len(tried) >= 6: break  # don't try too many

            # Strategy C: use API data to find edition-specific UUIDs
            if not hits and api_data:
                print(f"  [{lbl}] Analyzing {len(api_data)} API responses...")
                for resp in api_data:
                    data = resp["data"]
                    # Look for nested structures that contain editions
                    raw = json.dumps(data, ensure_ascii=False)
                    all_uuids = list(set(UUID_RE.findall(raw)))
                    for uid in all_uuids:
                        if uid in (link.get("uuid") for link in uuid_links):
                            continue  # already tried
                        pdf = try_pdf_download(session, uid, lbl)
                        if pdf:
                            text = extract_text_partial(pdf, lbl)
                            if text and len(text)>500:
                                hits = scan_text(text, caderno, date_str)
                                if hits: break
                    if hits: break

            if not hits:
                msg = (f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\n"
                       f"Sem conteúdo disponível\n"
                       f"🔗 [Verificar manualmente]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
                print(f"  [{lbl}] ⚠️ Nenhum resultado")
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

    aprovadas   = sorted([h for h in all_hits if h["veredito"].startswith("🟢")], key=lambda x:-x["score"])
    pode_render = sorted([h for h in all_hits if h["veredito"].startswith("🟡")], key=lambda x:-x["score"])
    background  =        [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas+pode_render:
        card=build_ref_card(h["ref"],date_str,h["veredito"],h["score"],h["fatores"])
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines=[f"🗂️ *Background DOESP — {date_str}* — {len(background)} ref(s)"]
        for h in background[:20]:
            cat=KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
            r=h["ref"]; pg=f" {r.get('page','')}"
            ttl=(r.get("title") or "")[:40]
            lines.append(f"{icon} `{h['keyword'][:30]}` [{r.get('label','')}]{pg} {ttl}")
        send_telegram("\n".join(lines), silent=True)

if __name__=="__main__":
    main()
