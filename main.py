"""
DOESP Monitor v5.3 — Diário Oficial do Estado de São Paulo
===========================================================
v5.2 confirmed: Playwright click navigation works. API fully mapped.

ARCHITECTURE v5.3:
  1. Playwright: loads page → clicks caderno → clicks section
  2. Intercepts 4 API responses after section click:
     a) /v1/editions/status      → all edition IDs
     b) ?EditionDate=...         → PDF UUID for this caderno+section
     c) ?name=publications       → paginated list (title + excerpt)
     d) ?JournalId=&SectionId=   → TREE STRUCTURE (ALL publications, not paginated)
  3. Parses TREE STRUCTURE recursively → every publication title + hierarchy path
     (330-1860 pubs per section, vs 10 from DOM page 1)
  4. Scans titles for keywords with KEYWORD_FILTERS
  5. Builds ficha with clear hierarchy:
     📋 Executivo > Atos Normativos > CASA CIVIL > Subsecretaria...

Improvements over v5.2:
  - Reads ALL publications via tree API (not just DOM page 1)
  - KEYWORD_FILTERS ported from DOC-SP (require_any, skip_if, max_hits)
  - Clear hierarchy labels (caderno > seção > órgão > divisão)
  - TV scoring with state-specific bonuses
  - Field extraction (valor, CNPJ, empresa) from excerpts
  - PDF UUID correctly extracted for optional download
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
PORTAL_URL   = "https://doe.sp.gov.br/sumario"
PDF_API      = "https://do-api-publication-pdf.doe.sp.gov.br"
UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

# ===========================================================================
# KEYWORDS + FILTERS (ported from DOC-SP v9.3 + DOESP v2)
# ===========================================================================
KEYWORD_CATEGORIES = {
    "contratação emergencial":"urgencia","organização social de saúde":"saude",
    "contrato de gestão":"saude","hospital das clínicas":"saude",
    "leito de UTI":"saude","medicamento de alto custo":"saude",
    "improbidade administrativa":"investigativo","superfaturamento":"investigativo",
    "sobrepreço":"investigativo","fraude em licitação":"investigativo",
    "desvio de verba":"investigativo","lavagem de dinheiro":"investigativo",
    "dispensa de licitação":"licitacao","inexigibilidade de licitação":"licitacao",
    "licitação deserta":"licitacao","concorrência eletrônica":"licitacao",
    "extrato de contrato":"contrato","rescisão de contrato":"contrato",
    "termo de aditamento":"contrato","rescindido o contrato":"contrato",
    "obra paralisada":"obras","habitação de interesse social":"obras",
    "unidades habitacionais":"obras","saneamento básico":"obras",
    "pavimentação":"obras","recapeamento asfáltico":"obras",
    "canalização":"obras","concessão rodoviária":"obras",
    "demissão de servidor":"disciplinar","aposentadoria compulsória":"disciplinar",
    "processo administrativo disciplinar":"disciplinar","sindicância":"disciplinar",
    "aplicação de penalidade":"penalidade","multa contratual":"penalidade",
    "ação civil pública":"legal","merenda escolar":"educacao",
    "transporte escolar":"educacao","construção de escola estadual":"educacao",
    "fechamento de escola":"educacao","concurso de professor":"educacao",
    "dengue":"saude","operação policial":"seguranca",
    "unidade prisional":"seguranca","morte em custódia":"seguranca",
    "feminicídio":"seguranca","delegacia de polícia":"seguranca",
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

KEYWORD_FILTERS = {
    "extrato de contrato":{"max_hits":20,
        "require_any":["cnpj","contratad","objeto","contratante","valor"]},
    "termo de aditamento":{"max_hits":15,
        "require_any":["cnpj","contratad","valor","objeto","aditamento"]},
    "dispensa de licitação":{"max_hits":15,
        "require_any":["autorizo","homologo","contratad","valor","objeto","dispensa"],
        "skip_if":["resultou fracassada"]},
    "inexigibilidade de licitação":{"min_value":50_000},
    "aplicação de penalidade":{"max_hits":10,
        "require_any":["aplico","notifico","suspensão","multa","pena"]},
    "sindicância":{"max_hits":10,
        "require_any":["instaurar","instaurada","conclusão","arquivada","pena","aplico"]},
    "processo administrativo disciplinar":{"max_hits":8,
        "require_any":["instaurado","instaurada","corregedoria","demissão","suspensão","aplico"]},
    "organização social de saúde":{"require_any":["contrato de gestão","os ","spdm","hospital"]},
    "CETESB":{"require_any":["multa","embargo","auto de infração","licença"]},
    "dengue":{"require_any":["caso","foco","combate","surto","contrato"],
              "skip_if":["projeto de lei"]},
    "superfaturamento":{
        "require_any":["apurou","indício","constatou","investigação","TCE","MP "],
        "skip_if":["evitar superfaturamento","vedado o superfaturamento"]},
    "sobrepreço":{
        "require_any":["apurou","indício","constatou","investigação"],
        "skip_if":["evitar contratações com sobrepreço"]},
    "nomeação para cargo em comissão":{"max_hits":8},
    "exoneração a pedido":{"max_hits":8},
    "exoneração de servidor":{"max_hits":8},
    "demissão de servidor":{"max_hits":5},
}

# ===========================================================================
# HELPERS
# ===========================================================================
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

_RE_MONEY = re.compile(r'R\$\s*[\d.,]+(?:\s*\([^)]{0,80}\))?', re.I)
_RE_CNPJ  = re.compile(r'(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)')
_RE_SEI   = re.compile(r'\d{3}\.\d{8}/\d{4}[-–]\d{2}')

# ===========================================================================
# TREE PARSER — extract ALL publications from the nested tree API response
# ===========================================================================
def extract_publications_from_tree(tree_data):
    """
    Recursively walk the tree structure API response.
    Returns list of {title, slug, id, path} for every publication.
    The path is the hierarchical chain: CASA CIVIL > Gabinete > Subsecretaria
    """
    pubs = []
    def walk(node, path_parts):
        if isinstance(node, dict):
            name = node.get("name","")
            new_path = path_parts + [name] if name else path_parts
            for pub in node.get("publications",[]):
                pubs.append({
                    "title": pub.get("title",""),
                    "slug":  pub.get("slug",""),
                    "id":    pub.get("id",""),
                    "path":  " > ".join(new_path),
                    "org":   new_path[-1] if len(new_path)>=2 else name,
                    "dept":  new_path[-1] if len(new_path)>=3 else "",
                })
            for key in ("children","items","itens","categories"):
                for child in node.get(key,[]):
                    walk(child, new_path)
        elif isinstance(node, list):
            for item in node: walk(item, path_parts)
    walk(tree_data, [])
    return pubs

# ===========================================================================
# PLAYWRIGHT: navigate, click, intercept APIs
# ===========================================================================
def process_caderno(browser, caderno):
    """
    Click through caderno → section. Intercept all API responses.
    Parse tree structure for ALL publications.
    Returns (publications_list, pdf_uuid, dom_text, excerpts_dict).
    """
    jn  = caderno["journalName"]
    rsn = caderno["rootSectionName"]
    lbl = caderno["label"]

    tree_data     = None
    pub_excerpts  = {}     # pub_id → excerpt
    pdf_uuid      = None
    all_api       = []

    def on_response(response):
        nonlocal tree_data, pdf_uuid
        try:
            url=response.url; ct=response.headers.get("content-type","")
            if response.status==200 and "json" in ct:
                data=response.json()
                all_api.append({"url":url,"data":data})
                raw=json.dumps(data,ensure_ascii=False)

                # Detect tree structure (has journalName + items with children/publications)
                if isinstance(data,dict) and "journalName" in data and "items" in data:
                    tree_data=data
                    print(f"    TREE [{url[-55:]}]")

                # Detect publications list (has "publications" array with excerpts)
                if isinstance(data,dict) and "publications" in data and "pages" in data:
                    for p in data["publications"]:
                        if p.get("id") and p.get("excerpt"):
                            pub_excerpts[p["id"]]=p.get("excerpt","")[:500]
                    print(f"    PUBS [{url[-55:]}] {data.get('pages',0)} pages, {len(data.get('publications',[]))} on p1")

                # Detect PDF edition URL
                if isinstance(data,dict) and "fileName" in data and "url" in data:
                    pdf_uuid=data["fileName"]
                    print(f"    PDF UUID: {pdf_uuid}")

                # Detect edition status (backup for PDF UUID)
                if isinstance(data,dict) and "editionsProcessed" in data:
                    print(f"    EDITIONS STATUS: {len(data['editionsProcessed'])} editions")
        except: pass

    ctx=browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                   "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="pt-BR")
    page=ctx.new_page()
    page.on("response", on_response)

    try:
        # Step 1: load
        print(f"  [{lbl}] Loading sumário...")
        page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        # Step 2: click caderno
        print(f"  [{lbl}] Click caderno '{jn}'")
        for sel in [f"text='{jn}'",f"button:has-text('{jn}')",f"a:has-text('{jn}')"]:
            loc=page.locator(sel)
            if loc.count()>0: loc.first.click(); print(f"    ✅ {sel}"); break
        page.wait_for_timeout(4000)

        # Step 3: click section
        print(f"  [{lbl}] Click section '{rsn}'")
        clicked=False
        for sel in [f"text='{rsn}'",f"a:has-text('{rsn}')",f"span:has-text('{rsn}')"]:
            loc=page.locator(sel)
            if loc.count()>0: loc.first.click(); clicked=True; print(f"    ✅ {sel}"); break
        if not clicked:
            words=rsn.split()
            for el in page.query_selector_all("a, button, div, span, li"):
                txt=(el.inner_text() or "").strip()
                if all(w.lower() in txt.lower() for w in words) and len(txt)<80:
                    el.click(); clicked=True; print(f"    ✅ '{txt[:40]}'"); break
        page.wait_for_timeout(6000)

        # Read DOM text
        dom_text=page.inner_text("body")
        print(f"  [{lbl}] DOM: {len(dom_text):,} chars | tree={'✅' if tree_data else '❌'} | pdf={pdf_uuid or '❌'} | excerpts={len(pub_excerpts)}")

        ctx.close()
        return tree_data, pub_excerpts, pdf_uuid, dom_text, all_api

    except Exception as e:
        print(f"  [{lbl}] PW error: {e}")
        import traceback; traceback.print_exc()
        ctx.close()
        return None, {}, None, "", []

# ===========================================================================
# SCAN — keyword search over all publications from tree
# ===========================================================================
def scan_publications(pubs, excerpts, dom_text, caderno):
    """
    Scan all publication titles (from tree API) + excerpts + DOM text for keywords.
    Returns scored hit list.
    """
    lbl=caderno["label"]; jn=caderno["journalName"]; rsn=caderno["rootSectionName"]
    results=[]; seen=set(); kw_cnt={}
    dom_low=normalize(dom_text)

    for kw in KEYWORDS:
        kn=normalize(kw); cat=KEYWORD_CATEGORIES.get(kw,"general")
        rules=KEYWORD_FILTERS.get(kw,{})
        mh=rules.get("max_hits",999); cnt=0

        for pub in pubs:
            title_low=normalize(pub["title"])
            excerpt_low=normalize(excerpts.get(pub["id"],""))
            searchable=title_low+" "+excerpt_low

            if kn not in searchable and kn not in dom_low: continue
            if kn in searchable:
                # Found in this specific publication
                dedup=(kw,pub["id"])
                if dedup in seen: continue
                seen.add(dedup)
                if cnt>=mh: continue

                full_text=pub["title"]+" "+(excerpts.get(pub["id"],""))
                full_low=normalize(full_text)
                if not passes_filter(kw, full_low, full_text): continue

                v,sc,ft=tv_score(full_low, kw, full_text)
                results.append({
                    "keyword":kw,"veredito":v,"score":sc,"fatores":ft,
                    "ref":{
                        "keyword":kw,"label":lbl,
                        "journal":jn,"section":rsn,
                        "title":pub["title"][:150],
                        "path":pub["path"],
                        "org":pub.get("org",""),
                        "slug":pub.get("slug",""),
                        "excerpt":(excerpts.get(pub["id"],""))[:400],
                        "pub_id":pub["id"],
                    }
                })
                cnt+=1; kw_cnt[kw]=cnt

    for kw,n in sorted(kw_cnt.items(),key=lambda x:-x[1]):
        print(f"    '{kw}': {n}")
    ap=sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr=sum(1 for r in results if r["veredito"].startswith("🟡"))
    total_pubs=len(pubs)
    print(f"  [{lbl}] {total_pubs} publicações escaneadas → {len(results)} hits | 🟢{ap} 🟡{pr}")
    return results

def passes_filter(kw, text_low, text_raw):
    rules=KEYWORD_FILTERS.get(kw,{})
    req=rules.get("require_any",[])
    if req and not any(normalize(p) in text_low for p in req): return False
    for ph in rules.get("skip_if",[]):
        if normalize(ph) in text_low: return False
    mv=rules.get("min_value")
    if mv:
        m=_RE_MONEY.search(text_raw)
        if m and 0<parse_brl(m.group(0))<mv: return False
    return True

# ===========================================================================
# TV SCORING (DOC-SP v9.3 + state-level bonuses)
# ===========================================================================
def tv_score(text_low, keyword, text_raw):
    cat=KEYWORD_CATEGORIES.get(keyword,"general")
    tier=CATEGORY_TV.get(cat,(3,"",""))[0]
    score={1:4,2:2,3:0}.get(tier,0); fatores=[]

    m=_RE_MONEY.search(text_raw)
    if m:
        amt=parse_brl(m.group(0))
        if amt>=50_000_000: score+=5; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=10_000_000: score+=4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=1_000_000: score+=3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt>=100_000: score+=1; fatores.append(f"R${amt/1e3:.0f}k")

    if any(t in text_low for t in ["emergencial","urgente","urgência"]):
        score+=3; fatores.append("EMERGENCIAL")
    if any(t in text_low for t in ["superfaturamento","sobrepreço","fraude em licitação","improbidade"]):
        score+=4; fatores.append("SUSPEITO")
    if any(t in text_low for t in ["hospital das clínicas","leito de uti","organização social de saúde","pronto-socorro"]):
        score+=2; fatores.append("SAÚDE")
    if any(t in text_low for t in ["escola estadual","merenda","alimentação escolar"]):
        score+=2; fatores.append("EDUCAÇÃO")
    if any(t in text_low for t in ["unidade prisional","policial penal","penitenciária","complexo penal"]):
        score+=1; fatores.append("PENITENCIÁRIO")
    if any(t in text_low for t in ["demissão","suspensão por","aposentadoria compulsória"]):
        score+=2; fatores.append("SANÇÃO FUNCIONAL")
    if any(t in text_low for t in ["concessão rodoviária","sabesp","metrô","cptm","dersa"]):
        score+=1; fatores.append("INFRAESTRUTURA")
    if any(t in text_low for t in ["cetesb","contaminada","embargo ambiental","área de risco"]):
        score+=1; fatores.append("MEIO AMBIENTE")
    # Field bonuses
    if _RE_CNPJ.search(text_raw): score+=1; fatores.append("CNPJ")
    if _RE_SEI.search(text_raw): score+=1; fatores.append("SEI")

    if score>=8: v="🟢 APROVADA"
    elif score>=5: v="🟡 PODE RENDER"
    else: v="🔴 BACKGROUND"
    return v,score,fatores

# ===========================================================================
# FICHA — structured card with clear hierarchy
# ===========================================================================
def build_ficha(hit, date_str):
    ref=hit["ref"]; kw=ref["keyword"]
    cat=KEYWORD_CATEGORIES.get(kw,"general")
    _,icon,cat_nome=CATEGORY_TV.get(cat,(3,"🔍","Geral"))
    lbl=ref.get("label",""); jn=ref.get("journal",""); rsn=ref.get("section","")
    emo=next((c["emoji"] for c in CADERNOS if c["label"]==lbl),"📋")
    fstr=" · ".join(hit["fatores"]) if hit["fatores"] else "—"
    link=caderno_url(jn,rsn)
    path=ref.get("path","")
    excerpt=ref.get("excerpt","")

    # Extract fields from excerpt
    valor=None; cnpj=None; sei=None; empresa=None
    if excerpt:
        m=_RE_MONEY.search(excerpt)
        if m: valor=m.group(0)
        m=_RE_CNPJ.search(excerpt)
        if m: cnpj=m.group(0)
        m=_RE_SEI.search(excerpt)
        if m: sei=m.group(0)
        m=re.search(r'(?:Contratad[ao]|empresa)\s*:?\s*([A-Z][A-Za-záéíóúÀ-ÿ\s&.,/()-]{5,80}?(?:LTDA|S/?A|EIRELI|EPP|ME)\b)',excerpt,re.I)
        if m: empresa=m.group(1).strip()

    lines=[
        f"📋 *DOESP {date_str}*",
        f"{emo} *{jn}* › *{rsn}*",
        f"{hit['veredito']} | {icon} *{cat_nome}*",
        f"🔑 `{kw}` | Score {hit['score']} | {fstr}",
        "─"*22,
    ]
    # Hierarchy path (CASA CIVIL > Gabinete > Subsecretaria)
    if path:
        # Remove section name from path (already shown above)
        short_path=path.split(" > ",1)[1] if " > " in path else path
        if short_path: lines.append(f"🏛️ {short_path}")
    if ref.get("title"): lines.append(f"📄 *{ref['title'][:130]}*")
    # Extracted fields
    if empresa: lines.append(f"🏢 {empresa[:80]}")
    if cnpj: lines.append(f"   CNPJ: {cnpj}")
    if valor: lines.append(f"💰 {valor}")
    if sei: lines.append(f"🔖 SEI: {sei}")
    # Excerpt (if available)
    if excerpt:
        hi=re.sub(f"(?i)({re.escape(kw)})",r"*\1*",excerpt[:250])
        lines.append(f"💬 _{hi}_")
    # Lacunas (what's missing)
    lacunas=[]
    if not empresa and cat in ("contrato","licitacao","penalidade","urgencia"):
        lacunas.append("Empresa/contratada")
    if not cnpj and cat in ("contrato","licitacao","penalidade"):
        lacunas.append("CNPJ")
    if not valor and cat not in ("pessoal","disciplinar","educacao"):
        lacunas.append("Valor")
    if lacunas: lines.append(f"❓ Faltando: {' · '.join(lacunas)}")
    lines+=["─"*22,f"🔗 [Abrir no portal]({link})"]
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str, pub_counts):
    all_h=[h for v in results_by_caderno.values() for h in v]
    total=len(all_h)
    ap=sum(1 for h in all_h if h["veredito"].startswith("🟢"))
    pr=sum(1 for h in all_h if h["veredito"].startswith("🟡"))
    lines=[
        f"📋 *{SOURCE_NAME} — {date_str}*",
        f"📊 *{total} resultado(s)*",
        f"🟢 {ap}  🟡 {pr}  🔴 {total-ap-pr}\n",
    ]
    for c in CADERNOS:
        lbl=c["label"]; hits=results_by_caderno.get(lbl,[])
        n_pubs=pub_counts.get(lbl,0)
        a=sum(1 for h in hits if h["veredito"].startswith("🟢"))
        p=sum(1 for h in hits if h["veredito"].startswith("🟡"))
        if hits:
            lines.append(f"{c['emoji']} *{lbl}* ({n_pubs} pub): {len(hits)} hits | 🟢{a} 🟡{p}")
        else:
            lines.append(f"{c['emoji']} *{lbl}* ({n_pubs} pub): —")
    lines.append("━"*20)
    for h in sorted(all_h,key=lambda x:-x["score"])[:12]:
        cat=KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
        ref=h["ref"]
        org=(ref.get("org") or "")[:25]
        lines.append(f"{h['veredito'][:2]} {icon} `{ref['keyword'][:28]}` [{ref['label']}] {org}")
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
    hoje=datetime.date.today()
    date_str=hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v5.3 — {date_str} ===\n")

    from playwright.sync_api import sync_playwright
    print("  Playwright: ✅\n")

    results_by_caderno={}
    pub_counts={}

    with sync_playwright() as pw:
        browser=pw.chromium.launch(
            headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox",
                  "--disable-dev-shm-usage","--disable-gpu"])

        for caderno in CADERNOS:
            lbl,emo=caderno["label"],caderno["emoji"]
            print(f"\n{'─'*60}")
            print(f"{emo}  {caderno['journalName']} / {caderno['rootSectionName']}")
            print(f"{'─'*60}")

            tree_data, excerpts, pdf_uuid, dom_text, api_data = \
                process_caderno(browser, caderno)

            hits=[]

            # Strategy A: parse tree structure (ALL publications)
            if tree_data:
                pubs=extract_publications_from_tree(tree_data)
                pub_counts[lbl]=len(pubs)
                print(f"  [{lbl}] Tree: {len(pubs)} publicações extraídas")
                if pubs:
                    hits=scan_publications(pubs, excerpts, dom_text, caderno)

            # Strategy B: if no tree, scan DOM text
            if not hits and len(dom_text)>2000:
                print(f"  [{lbl}] Fallback: scanning DOM text...")
                fn=normalize(dom_text)
                for kw in KEYWORDS:
                    kn=normalize(kw)
                    if kn in fn:
                        v,sc,ft=tv_score(fn,kw,dom_text)
                        hits.append({"keyword":kw,"veredito":v,"score":sc,"fatores":ft,
                            "ref":{"keyword":kw,"label":lbl,
                                   "journal":caderno["journalName"],
                                   "section":caderno["rootSectionName"],
                                   "title":"","path":"","org":"",
                                   "excerpt":"","slug":"","pub_id":""}})
                pub_counts.setdefault(lbl,0)

            if not hits:
                msg=(f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\n"
                     f"Sem resultados relevantes\n"
                     f"🔗 [Verificar]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
                print(f"  [{lbl}] ⚠️ Nenhum resultado")
                send_telegram(msg)

            results_by_caderno[lbl]=hits
            time.sleep(2)

        browser.close()

    total=sum(len(v) for v in results_by_caderno.values())
    all_hits=[h for v in results_by_caderno.values() for h in v]
    print(f"\n{'='*60}\nTOTAL: {total}")

    if total==0: return

    send_telegram(build_summary(results_by_caderno, date_str, pub_counts))
    time.sleep(1)

    aprovadas  =sorted([h for h in all_hits if h["veredito"].startswith("🟢")],key=lambda x:-x["score"])
    pode_render=sorted([h for h in all_hits if h["veredito"].startswith("🟡")],key=lambda x:-x["score"])
    background =       [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas+pode_render:
        card=build_ficha(h, date_str)
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines=[f"🗂️ *Background — {date_str}* — {len(background)} ref(s)"]
        for h in background[:25]:
            cat=KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
            ref=h["ref"]; org=(ref.get("org") or "")[:30]
            lines.append(f"{icon} `{h['keyword'][:28]}` [{ref['label']}] {org}")
        send_telegram("\n".join(lines), silent=True)

if __name__=="__main__":
    main()
