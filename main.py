"""
DOESP Monitor v5.0 — Diário Oficial do Estado de São Paulo
===========================================================
Portal: https://doe.sp.gov.br/sumario

ROOT CAUSE (confirmado v3.x-v4.0):
  __NEXT_DATA__ sempre tem nextExport:true, autoExport:true
  → app estaticamente exportado: HTML shell idêntico para qualquer URL
  → pageProps:{} sempre vazio: zero dados no HTML inicial
  → UUID só existe após JavaScript executar no browser

SOLUÇÃO v5.0: Playwright como método primário de UUID discovery
  1. Playwright navega até a URL do caderno
  2. Intercepta respostas de rede dos dominios do-api-*
  3. Extrai UUID da URL ou body JSON interceptado
  4. Fallback: lê DOM renderizado após networkidle
  5. Se UUID encontrado → baixa PDF parcial → escaneia

ARQUIVO EXTRA NECESSÁRIO: .github/workflows/scraper.yml
  Deve instalar: playwright + playwright install chromium --with-deps
  Ver scraper.yml incluído neste repositório.

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
PORTAL_BASE  = "https://doe.sp.gov.br"
PDF_API      = "https://do-api-publication-pdf.doe.sp.gov.br"
WORKFLOW_API = "https://do-api-publication-workflow.doe.sp.gov.br"
SEARCH_API   = "https://do-api-web-search.doe.sp.gov.br"
MAX_PDF_PAGES = 20

UUID_RE = re.compile(
    r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

# ===========================================================================
# KEYWORDS
# ===========================================================================
KEYWORD_CATEGORIES = {
    "contratação emergencial":              "urgencia",
    "organização social de saúde":          "saude",
    "contrato de gestão":                   "saude",
    "hospital das clínicas":                "saude",
    "leito de UTI":                         "saude",
    "medicamento de alto custo":            "saude",
    "improbidade administrativa":           "investigativo",
    "superfaturamento":                     "investigativo",
    "sobrepreço":                           "investigativo",
    "fraude em licitação":                  "investigativo",
    "desvio de verba":                      "investigativo",
    "dispensa de licitação":                "licitacao",
    "inexigibilidade de licitação":         "licitacao",
    "licitação deserta":                    "licitacao",
    "concorrência eletrônica":              "licitacao",
    "extrato de contrato":                  "contrato",
    "rescisão de contrato":                 "contrato",
    "termo de aditamento":                  "contrato",
    "obra paralisada":                      "obras",
    "habitação de interesse social":        "obras",
    "unidades habitacionais":               "obras",
    "saneamento básico":                    "obras",
    "pavimentação":                         "obras",
    "recapeamento asfáltico":               "obras",
    "canalização":                          "obras",
    "concessão rodoviária":                 "obras",
    "demissão de servidor":                 "disciplinar",
    "aposentadoria compulsória":            "disciplinar",
    "processo administrativo disciplinar":  "disciplinar",
    "sindicância":                          "disciplinar",
    "aplicação de penalidade":              "penalidade",
    "multa contratual":                     "penalidade",
    "ação civil pública":                   "legal",
    "merenda escolar":                      "educacao",
    "transporte escolar":                   "educacao",
    "construção de escola estadual":        "educacao",
    "dengue":                               "saude",
    "operação policial":                    "seguranca",
    "unidade prisional":                    "seguranca",
    "morte em custódia":                    "seguranca",
    "licença ambiental":                    "meio_ambiente",
    "auto de infração ambiental":           "meio_ambiente",
    "CETESB":                               "meio_ambiente",
    "área contaminada":                     "meio_ambiente",
    "crédito adicional suplementar":        "orcamento",
    "nomeação para cargo em comissão":      "pessoal",
    "exoneração a pedido":                  "pessoal",
    "exoneração de servidor":               "pessoal",
}
KEYWORDS = sorted(KEYWORD_CATEGORIES.keys(), key=len, reverse=True)

CATEGORY_TV = {
    "urgencia":      (1, "🚨", "Emergência"),
    "saude":         (1, "🏥", "Saúde"),
    "investigativo": (1, "🔎", "Investigativo"),
    "obras":         (2, "🏗️", "Obras"),
    "licitacao":     (2, "🛒", "Licitação"),
    "contrato":      (2, "📝", "Contrato"),
    "disciplinar":   (2, "⚖️", "Disciplinar"),
    "penalidade":    (2, "⚖️", "Penalidade"),
    "educacao":      (2, "🎓", "Educação"),
    "seguranca":     (2, "🚔", "Segurança"),
    "legal":         (2, "🏛️", "Judicial"),
    "meio_ambiente": (2, "🌿", "Meio Ambiente"),
    "orcamento":     (3, "💼", "Orçamento"),
    "pessoal":       (3, "👤", "Pessoal"),
    "general":       (3, "🔍", "Geral"),
}

def normalize(t):
    return "".join(c for c in unicodedata.normalize("NFKD", t)
                   if not unicodedata.combining(c)).lower()

def parse_brl(s):
    if not s: return 0.0
    m = re.search(r"[\d.,]+", s)
    if not m: return 0.0
    v = re.sub(r"\.(?=\d{3}(\D|$))", "", m.group()).replace(",", ".")
    try: return float(v)
    except: return 0.0

def caderno_url(jn, rsn):
    return (f"{PORTAL_URL}"
            f"?journalName={requests.utils.quote(jn)}"
            f"&rootSectionName={requests.utils.quote(rsn)}")

# ===========================================================================
# UUID DISCOVERY — PRIMARY: Playwright
# ===========================================================================
def get_uuid_playwright(caderno):
    """
    Navigate to the caderno URL with a real Chromium browser.
    Intercept network responses from do-api-* domains.
    The JavaScript SPA will call these APIs to get the edition UUID.
    """
    jn  = caderno["journalName"]
    rsn = caderno["rootSectionName"]
    lbl = caderno["label"]
    url = caderno_url(jn, rsn)

    try:
        from playwright.sync_api import sync_playwright
    except ImportError:
        print(f"  [{lbl}] playwright não instalado — usando fallback REST")
        return None

    captured = []   # (score, uuid, source_url)

    def handle_response(response):
        try:
            resp_url = response.url
            status   = response.status

            # UUID in the URL itself (e.g. /editions/{uuid})
            for u in UUID_RE.findall(resp_url):
                sc = _score_uuid_source(u, resp_url, jn, rsn)
                captured.append((sc, u, resp_url))

            # UUID in JSON response body
            if status == 200:
                ct = response.headers.get("content-type", "")
                if "json" in ct:
                    try:
                        body = response.text()
                        uuids_in_body = UUID_RE.findall(body)
                        for u in uuids_in_body:
                            sc = _score_uuid_source(u, resp_url + " " + body[:200], jn, rsn)
                            captured.append((sc, u, resp_url))
                        if uuids_in_body:
                            print(f"  PW JSON {resp_url[-70:]}: {uuids_in_body[:3]}")
                    except:
                        pass
        except:
            pass

    print(f"  [{lbl}] Playwright → {url}")
    try:
        with sync_playwright() as pw:
            browser = pw.chromium.launch(
                headless=True,
                args=["--no-sandbox", "--disable-setuid-sandbox",
                      "--disable-dev-shm-usage", "--disable-gpu"])
            ctx = browser.new_context(
                user_agent=(
                    "Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                    "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
                locale="pt-BR")
            page = ctx.new_page()
            page.on("response", handle_response)

            page.goto(url, wait_until="networkidle", timeout=30000)
            # Extra wait: some APIs fire after networkidle
            page.wait_for_timeout(4000)

            # Also scan the fully-rendered DOM
            dom = page.content()
            dom_uuids = UUID_RE.findall(dom)
            for u in dom_uuids:
                sc = _score_uuid_source(u, dom[max(0,dom.lower().find(u.lower())-100):
                                              dom.lower().find(u.lower())+100], jn, rsn)
                captured.append((sc, u, "dom"))

            print(f"  [{lbl}] PW intercepted {len(captured)} UUID candidates")
            # Show top 6 unique
            seen_u = set()
            for sc, u, src in sorted(captured, reverse=True)[:8]:
                if u not in seen_u:
                    print(f"    score={sc:2d} {u} ← {src[-50:]}")
                    seen_u.add(u)

            browser.close()
    except Exception as e:
        print(f"  [{lbl}] PW error: {e}")
        return None

    if not captured:
        print(f"  [{lbl}] PW: nenhum UUID capturado")
        return None

    # Return highest-scored UUID
    captured.sort(reverse=True)
    best = captured[0][1]
    print(f"  [{lbl}] PW UUID: {best}")
    return best

def _score_uuid_source(uuid, source_text, jn, rsn):
    """Score a UUID by how likely it is to be the edition UUID for this caderno."""
    s = source_text.lower()
    jn_low  = jn.lower().replace(" ", "")
    rsn_low = rsn.lower().replace(" ", "")
    score = 0
    # Domain signals
    if "do-api" in s:          score += 5
    if "edition" in s:         score += 4
    if "publication" in s:     score += 3
    if "pdf" in s:             score += 2
    if "workflow" in s:        score += 2
    if "web-search" in s:      score += 2
    # Caderno context
    if jn_low in s:            score += 2
    if rsn_low[:8] in s:       score += 3
    # Penalise build artifacts
    if "buildid" in s:         score -= 5
    if "static" in s:          score -= 3
    if "chunk" in s:           score -= 2
    if "fonts" in s:           score -= 2
    if "image" in s:           score -= 2
    return score

# ===========================================================================
# UUID DISCOVERY — FALLBACK: REST APIs
# Used when Playwright is unavailable or fails.
# ===========================================================================
_html_cache = {}

def _fetch_portal_html(session):
    if _html_cache: return _html_cache.get("html"), _html_cache.get("bid")
    try:
        r = session.get(PORTAL_URL, timeout=20)
        r.encoding = "utf-8"
        print(f"  HTML {r.status_code} | {len(r.text):,} chars")
        if r.status_code != 200: return None, None
        html = r.text
        m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
        bid = m.group(1) if m else ""
        _html_cache["html"] = html; _html_cache["bid"] = bid
        return html, bid
    except Exception as e:
        print(f"  HTML err: {e}"); return None, None

def get_uuid_rest(session, caderno):
    """Last-resort REST probing. Documents why each path fails."""
    jn, rsn, lbl = caderno["journalName"], caderno["rootSectionName"], caderno["label"]
    hoje = datetime.date.today().isoformat()
    jne  = requests.utils.quote(jn); rsne = requests.utils.quote(rsn)

    html, bid = _fetch_portal_html(session)

    # S1: __NEXT_DATA__ (will be empty — kept for completeness)
    if html:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                # Print keys once for diagnostics
                if not hasattr(get_uuid_rest, "_printed_keys"):
                    print(f"  __NEXT_DATA__ keys: {list(data.keys())}")
                    print(f"  nextExport={data.get('nextExport')} autoExport={data.get('autoExport')}")
                    get_uuid_rest._printed_keys = True
                u = _best_uuid_in_obj(data, jn, rsn)
                if u: print(f"  UUID via __NEXT_DATA__: {u}"); return u
            except: pass

    # S2-S4: try all known API domains
    for base, ver, desc in [
        (PDF_API,      "v1", "PDF-API-v1"),
        (PDF_API,      "v2", "PDF-API-v2"),
        (WORKFLOW_API, "v1", "Workflow-v1"),
        (WORKFLOW_API, "v2", "Workflow-v2"),
    ]:
        for path in [
            f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}",
        ]:
            try:
                r = session.get(base + path, timeout=10)
                ct = r.headers.get("content-type", "")
                is_json = "json" in ct.lower()
                print(f"  {desc} {r.status_code} {'json' if is_json else 'html'} ...{path[-55:]}")
                if r.status_code == 200 and is_json:
                    data = r.json()
                    u = _best_uuid_in_obj(data, jn, rsn)
                    if u: print(f"  UUID via {desc}: {u}"); return u
                    all_u = UUID_RE.findall(json.dumps(data))
                    if all_u: print(f"  candidates: {all_u[:4]}")
            except Exception as e:
                print(f"  {desc} exc: {e}")

    print(f"  UUID não encontrado via REST para {lbl}")
    return None

def _best_uuid_in_obj(obj, jn, rsn, depth=0):
    if depth > 15: return None
    jn_l = jn.lower().replace(" ",""); rsn_l = rsn.lower().replace(" ","")
    def sc(t): t=str(t).lower().replace(" ",""); return (jn_l in t)*2+(rsn_l in t)*3
    def collect(o, ctx, d):
        if d > 12: return []
        res = []
        if isinstance(o, dict):
            cx = " ".join(str(v) for k,v in o.items()
                if k in ("name","journalName","rootSectionName","title","section") and isinstance(v,str))
            s = ctx + sc(cx)
            for k,v in o.items():
                if isinstance(v,str) and re.match(r'^[0-9a-f-]{36}$',v,re.I):
                    res.append((s+sc(k), v))
                else: res.extend(collect(v, s, d+1))
        elif isinstance(o, list):
            for item in o: res.extend(collect(item, ctx, d+1))
        return res
    cands = collect(obj, 0, 0)
    if not cands: return None
    cands.sort(reverse=True)
    return cands[0][1] if cands[0][0] > 0 else (cands[0][1] if len(cands)==1 else None)

# ===========================================================================
# PDF DOWNLOAD + TEXT EXTRACTION
# ===========================================================================
def baixar_pdf(session, uuid):
    url = f"{PDF_API}/v1/editions/{uuid}"
    print(f"  PDF {url[-60:]}")
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code != 200:
            print(f"  PDF HTTP {r.status_code}")
            return None
        data = b"".join(r.iter_content(65536))
        if data[:4] != b"%PDF":
            print(f"  Não é PDF (primeiros bytes: {data[:10]!r})")
            return None
        print(f"  PDF OK — {len(data)/1e6:.1f} MB")
        return data
    except Exception as e:
        print(f"  PDF err: {e}")
        return None

def extract_text(pdf_bytes, label=""):
    """pdfminer 2-column, máx MAX_PDF_PAGES páginas."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        parts = []
        for i, page in enumerate(extract_pages(io.BytesIO(pdf_bytes), laparams=lp)):
            if i >= MAX_PDF_PAGES: break
            mid = page.width * 0.52
            boxes = [(el.bbox[3], (el.bbox[0]+el.bbox[2])/2, el.get_text())
                     for el in page if isinstance(el, LTTextBox) and el.get_text().strip()]
            left  = sorted([(y,t) for y,x,t in boxes if x  < mid], reverse=True)
            right = sorted([(y,t) for y,x,t in boxes if x >= mid], reverse=True)
            parts.append("\n".join(t.strip() for _,t in left + right))
            parts.append("\x0c")
        result = "\n".join(parts)
        print(f"  texto {len(result):,} chars ({label}, {MAX_PDF_PAGES}pp max)")
        return result
    except Exception as e:
        print(f"  pdfminer err: {e}")
        return ""

# ===========================================================================
# SCAN + SCORE
# ===========================================================================
def scan_text(full_text, caderno, date_str):
    fn  = normalize(full_text)
    lbl = caderno["label"]; jn = caderno["journalName"]; rsn = caderno["rootSectionName"]
    page_breaks = [(m.start(), f"p.{i+2}")
                   for i,m in enumerate(re.finditer(r"\x0c", full_text))]
    def pag(pos):
        p = "p.1"
        for pb, pl in page_breaks:
            if pb <= pos: p = pl
            else: break
        return p

    results = []; seen = set(); kw_cnt = {}
    for kw in KEYWORDS:
        kn = normalize(kw); sp = 0
        while True:
            pos = fn.find(kn, sp)
            if pos == -1: break
            sp  = pos + max(len(kn), 400)
            pg  = pag(pos)
            dedup = (kw, pg)
            if dedup in seen: continue
            seen.add(dedup)
            window = re.sub(r"\s+", " ", full_text[max(0,pos-200):pos+400]).strip()
            # Nearest document header before this keyword
            doc_header = ""
            before = full_text[max(0,pos-1000):pos]
            for pat in [
                r'((?:PORTARIA|RESOLUÇÃO|RESOLUCAO|DECRETO|DESPACHO'
                r'|EDITAL|COMUNICADO|EXTRATO[S]?\s+DE\s+CONTRATO)\s+[^\n]{8,100})',
            ]:
                m = re.findall(pat, before, re.I)
                if m: doc_header = m[-1].strip()[:120]; break
            ref = {
                "keyword": kw, "label": lbl, "journal": jn, "section": rsn,
                "title": doc_header, "page": pg,
                "excerpt": window[:350], "edition_id": "", "attach_id": "",
                "doc_type": "", "date": date_str,
            }
            v, sc, ft = score_ref(ref, normalize(window))
            results.append({"ref":ref, "veredito":v, "score":sc, "fatores":ft, "keyword":kw})
            kw_cnt[kw] = kw_cnt.get(kw, 0) + 1

    for kw, n in sorted(kw_cnt.items(), key=lambda x:-x[1])[:12]:
        print(f"    '{kw}': {n}")
    ap = sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr = sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} hits | 🟢{ap} 🟡{pr}")
    return results

def score_ref(ref, excerpt_low):
    keyword = ref["keyword"]
    cat     = KEYWORD_CATEGORIES.get(keyword, "general")
    tier    = CATEGORY_TV.get(cat, (3,"",""))[0]
    score   = {1:4, 2:2, 3:0}.get(tier, 0)
    fatores = []

    money = re.search(r"r\$\s*([\d.,]+)", excerpt_low)
    if money:
        amt = parse_brl(money.group(1))
        if   amt >= 10_000_000: score += 4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt >= 1_000_000:  score += 3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt >= 100_000:    score += 1; fatores.append(f"R${amt/1e3:.0f}k")

    if any(t in excerpt_low for t in ["emergencial","urgente","urgência"]):
        score += 3; fatores.append("EMERGENCIAL")
    if any(t in excerpt_low for t in ["superfaturamento","sobrepreço","fraude","improbidade"]):
        score += 4; fatores.append("SUSPEITO")
    if any(t in excerpt_low for t in ["hospital das clínicas","leito de uti","organização social de saúde"]):
        score += 2; fatores.append("SAÚDE")
    if any(t in excerpt_low for t in ["unidade prisional","policial penal","penitenciária"]):
        score += 1; fatores.append("PENITENCIÁRIO")
    if any(t in excerpt_low for t in ["demissão","suspensão por","aposentadoria compulsória"]):
        score += 2; fatores.append("SANÇÃO FUNCIONAL")
    if ref.get("title"):  score += 1; fatores.append("TÍTULO")
    if ref.get("page"):   score += 1; fatores.append(f"p.{ref['page']}")

    if   score >= 8: v = "🟢 APROVADA"
    elif score >= 5: v = "🟡 PODE RENDER"
    else:            v = "🔴 BACKGROUND"
    return v, score, fatores

# ===========================================================================
# REFERENCE CARD
# ===========================================================================
def build_ref_card(ref, date_str, veredito, score, fatores):
    cat   = KEYWORD_CATEGORIES.get(ref["keyword"], "general")
    _, icon, cat_nome = CATEGORY_TV.get(cat, (3,"🔍","Geral"))
    jn    = ref.get("journal","")
    rsn   = ref.get("section","")
    lbl   = next((c["label"] for c in CADERNOS
                  if c["journalName"]==jn and c["rootSectionName"]==rsn), rsn or jn)
    emo   = next((c["emoji"] for c in CADERNOS
                  if c["journalName"]==jn and c["rootSectionName"]==rsn), "📋")
    fator_str = " · ".join(fatores) if fatores else "—"
    link  = caderno_url(jn or "Executivo", rsn or "Atos Normativos")

    lines = [
        f"{SOURCE_EMOJI} *DOESP {date_str}* | {emo} {lbl}",
        f"{veredito} | {icon} *{cat_nome}*",
        f"🔑 `{ref['keyword']}` | Score {score} | {fator_str}",
        "─" * 22,
    ]
    if ref.get("doc_type"): lines.append(f"📑 {ref['doc_type'][:80]}")
    if ref.get("title"):    lines.append(f"📄 *{ref['title'][:120]}*")
    if ref.get("page"):     lines.append(f"📖 Página {ref['page']}")
    if ref.get("excerpt"):
        hi = re.sub(f"(?i)({re.escape(ref['keyword'])})", r"*\1*", ref["excerpt"][:300])
        lines.append(f"💬 _{hi}_")
    lines.append("─" * 22)
    lines.append(f"🔗 [Abrir no portal]({link})")
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str):
    all_hits = [h for v in results_by_caderno.values() for h in v]
    total = len(all_hits)
    ap = sum(1 for h in all_hits if h.get("veredito","").startswith("🟢"))
    pr = sum(1 for h in all_hits if h.get("veredito","").startswith("🟡"))
    lines = [
        f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
        f"📋 *{total} resultado(s)*\n",
        f"🟢 Aprovadas: {ap}  🟡 Pode render: {pr}  🔴 Background: {total-ap-pr}\n",
    ]
    for cad in CADERNOS:
        lbl, emo = cad["label"], cad["emoji"]
        hits = results_by_caderno.get(lbl, [])
        a2 = sum(1 for h in hits if h.get("veredito","").startswith("🟢"))
        p2 = sum(1 for h in hits if h.get("veredito","").startswith("🟡"))
        lines.append(f"{emo} *{lbl}*: {len(hits)} hits | 🟢{a2} 🟡{p2}" if hits
                     else f"{emo} *{lbl}*: nenhum")
    lines.append("━"*20)
    for h in sorted(all_hits, key=lambda x:-x.get("score",0))[:10]:
        cat  = KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _,icon,_ = CATEGORY_TV.get(cat,(3,"🔍",""))
        pg   = f" {h['ref'].get('page','')}"
        ttl  = (h["ref"].get("title") or "")[:35]
        lines.append(f"{h.get('veredito','')[:2]} {icon} `{h['ref']['keyword'][:30]}`"
                     f" [{h['ref'].get('label','')}]{pg} {ttl}")
    lines += ["━"*20, f"\n🔗 [Portal DOESP]({PORTAL_URL})"]
    return "\n".join(lines)

# ===========================================================================
# TELEGRAM
# ===========================================================================
_last_send = 0.0

def send_telegram(text, silent=False):
    global _last_send
    gap = time.time() - _last_send
    if gap < 2.0: time.sleep(2.0 - gap)
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID, "text":text, "parse_mode":"Markdown",
                      "disable_web_page_preview":True, "disable_notification":silent},
                timeout=15)
            _last_send = time.time()
            if r.status_code == 200: return True
            if r.status_code == 429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1)
                continue
            print(f"  TG {r.status_code}: {r.text[:60]}")
            return False
        except Exception as e:
            print(f"  TG err: {e}"); time.sleep(3)
    return False

def split_long(text, max_len=3800):
    if len(text) <= max_len: return [text]
    parts = []; cur = ""
    for line in text.split("\n"):
        if len(cur)+len(line)+1 > max_len: parts.append(cur); cur = line
        else: cur += ("\n" if cur else "") + line
    if cur: parts.append(cur)
    return parts

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    hoje     = datetime.date.today()
    date_str = hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v5.0 — {date_str} ===\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept":          "application/json,text/html,*/*;q=0.9",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin":          "https://doe.sp.gov.br",
        "Referer":         "https://doe.sp.gov.br/",
    })

    # Check Playwright availability once
    try:
        from playwright.sync_api import sync_playwright
        pw_available = True
        print("  Playwright: disponível ✅")
    except ImportError:
        pw_available = False
        print("  Playwright: NÃO instalado — fallback REST apenas")

    results_by_caderno = {}

    for caderno in CADERNOS:
        lbl, emo = caderno["label"], caderno["emoji"]
        print(f"\n{'─'*60}")
        print(f"{emo}  {caderno['journalName']} / {caderno['rootSectionName']}")
        print(f"{'─'*60}")

        # Step 1: get UUID
        uuid = None
        if pw_available:
            uuid = get_uuid_playwright(caderno)
        if not uuid:
            print(f"  PW falhou — tentando REST...")
            uuid = get_uuid_rest(session, caderno)

        if not uuid:
            msg = (f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\n"
                   f"UUID não encontrado (PW+REST falharam)\n"
                   f"🔗 [Verificar manualmente]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
            print(f"  ⚠️ UUID não encontrado para {lbl}")
            send_telegram(msg)
            results_by_caderno[lbl] = []
            continue

        # Step 2: download PDF (first MAX_PDF_PAGES pages)
        pdf_bytes = baixar_pdf(session, uuid)
        if not pdf_bytes:
            results_by_caderno[lbl] = []
            continue

        # Step 3: extract text + scan
        full_text = extract_text(pdf_bytes, lbl)
        if not full_text or len(full_text) < 200:
            results_by_caderno[lbl] = []
            continue

        hits = scan_text(full_text, caderno, date_str)
        results_by_caderno[lbl] = hits
        time.sleep(2)

    total = sum(len(v) for v in results_by_caderno.values())
    all_hits = [h for v in results_by_caderno.values() for h in v]
    print(f"\n{'='*60}\nTOTAL: {total}")

    if total == 0:
        lines = [f"✅ *{SOURCE_NAME} — {date_str}*",
                 "Nenhuma ocorrência nos cadernos monitorados."]
        for cad in CADERNOS:
            lines.append(f"{cad['emoji']} [{cad['label']}] "
                         f"[Abrir caderno]({caderno_url(cad['journalName'],cad['rootSectionName'])})")
        send_telegram("\n".join(lines))
        return

    send_telegram(build_summary(results_by_caderno, date_str))
    time.sleep(1)

    aprovadas   = sorted([h for h in all_hits if h["veredito"].startswith("🟢")], key=lambda x:-x["score"])
    pode_render = sorted([h for h in all_hits if h["veredito"].startswith("🟡")], key=lambda x:-x["score"])
    background  =        [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas + pode_render:
        card = build_ref_card(h["ref"], date_str, h["veredito"], h["score"], h["fatores"])
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines = [f"🗂️ *Background DOESP — {date_str}* — {len(background)} ref(s)"]
        for h in background[:20]:
            cat = KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _,icon,_ = CATEGORY_TV.get(cat,(3,"🔍",""))
            r = h["ref"]; pg = f" {r.get('page','')}"
            ttl = (r.get("title") or "")[:40]
            lines.append(f"{icon} `{h['keyword'][:30]}` [{r.get('label','')}]{pg} {ttl}")
        send_telegram("\n".join(lines), silent=True)

if __name__ == "__main__":
    main()
