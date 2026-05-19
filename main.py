"""
DOESP Monitor v4.0 — Diário Oficial do Estado de São Paulo
===========================================================
Portal: https://doe.sp.gov.br/sumario

ARQUITETURA v4.0 — Search-first, sem download de PDF
  ANTES (v3.x): descobre UUID → baixa PDF (190-274pp) → extrai texto → scan
    • Falha total se UUID não encontrado
    • Pesado: 150+ MB de PDF por execução
    • Nenhum resultado se API de UUID bloqueia

  AGORA (v4.0): Search API → referências, sem PDF
    1. _discover_search_endpoint()
         Testa ~20 paths no do-api-web-search.doe.sp.gov.br
         Salva o endpoint que retorna JSON válido
         Se falhar → fallback para descoberta de UUID + PDF mínimo
    2. search_caderno(caderno, keyword)
         Chama o search endpoint com: text, journalName, rootSectionName, date
         Retorna lista de referências: {title, page, excerpt, edition_id}
    3. score_reference() — TV scoring adaptado para referências
    4. build_ref_card() — cartão com link direto ao portal
         Substitui ficha/digesto: referência clicável, não extrato de PDF

  OUTPUT (referência, não ficha):
    📋 DOESP 19/05/2026 | Executivo > Atos Normativos | p.45
    🔑 "extrato de contrato"
    📄 PORTARIA GP 123/2026
    🔗 [Abrir no portal](https://doe.sp.gov.br/sumario?...)

  DOMÍNIOS CONFIRMADOS (do log de 19/05/2026):
    do-api-web-search.doe.sp.gov.br         ← SEARCH (novo primário)
    do-api-publication-pdf.doe.sp.gov.br    ← PDF download (fallback)
    do-api-publication-workflow.doe.sp.gov.br
    do-api-admin-edition.doe.sp.gov.br      ← retorna HTML (descartado)

Secrets: TELEGRAM_TOKEN, CHAT_ID
"""

import requests, datetime, os, sys, re, json, unicodedata, io, time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
if not TELEGRAM_TOKEN or not CHAT_ID:
    print("FATAL: TELEGRAM_TOKEN ou CHAT_ID ausentes."); sys.exit(1)

# ===========================================================================
# CADERNOS
# ===========================================================================
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

# ===========================================================================
# CONFIG
# ===========================================================================
SOURCE_NAME  = "DOESP"
SOURCE_EMOJI = "📋"
PORTAL_URL   = "https://doe.sp.gov.br/sumario"
PORTAL_BASE  = "https://doe.sp.gov.br"
PDF_API      = "https://do-api-publication-pdf.doe.sp.gov.br"
SEARCH_API   = "https://do-api-web-search.doe.sp.gov.br"
WORKFLOW_API = "https://do-api-publication-workflow.doe.sp.gov.br"

UUID_RE = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

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

# ===========================================================================
# NORMALIZE / HELPERS
# ===========================================================================
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
    jne = requests.utils.quote(jn)
    rsne = requests.utils.quote(rsn)
    return f"{PORTAL_URL}?journalName={jne}&rootSectionName={rsne}"

# ===========================================================================
# SEARCH API DISCOVERY
# Cache the working endpoint in a module-level variable.
# ===========================================================================
_search_endpoint_cache = {}   # domain -> working path template (or None)

def _probe_search(session, base, path):
    try:
        r = session.get(base + path, timeout=10)
        ct = r.headers.get("content-type", "")
        if r.status_code == 200 and "json" in ct:
            try: return r.json()
            except: pass
        if r.status_code == 200 and not ct.startswith("text/html"):
            print(f"  probe {r.status_code} {ct[:30]} {base}{path[:50]}")
    except: pass
    return None

def discover_search_endpoint(session):
    """
    Find which path on do-api-web-search.doe.sp.gov.br returns valid search results.
    Returns a callable search(keyword, jn, rsn, date) -> list[dict].
    """
    global _search_endpoint_cache
    if "search_fn" in _search_endpoint_cache:
        return _search_endpoint_cache["search_fn"]

    hoje = datetime.date.today().isoformat()
    probe_kw = "contrato"
    pkw = requests.utils.quote(probe_kw)
    # Probe paths roughly ordered by likelihood
    probe_paths = [
        f"/v1/search?text={pkw}&date={hoje}",
        f"/v1/search?q={pkw}&date={hoje}",
        f"/v1/search?texto={pkw}&date={hoje}",
        f"/api/search?q={pkw}&date={hoje}",
        f"/api/v1/search?q={pkw}&date={hoje}",
        f"/v1/search?text={pkw}",
        f"/v1/search?q={pkw}",
        f"/v1/publications?text={pkw}&publicationDate={hoje}",
        f"/v1/publications?q={pkw}&date={hoje}",
        f"/v1/contents?q={pkw}&date={hoje}",
        f"/v1/articles?q={pkw}&date={hoje}",
        f"/search?q={pkw}&date={hoje}",
        f"/v1/texts?q={pkw}&date={hoje}",
    ]

    print(f"\n  Descobrindo search endpoint em {SEARCH_API}...")
    for path in probe_paths:
        data = _probe_search(session, SEARCH_API, path)
        if data is not None:
            print(f"  ✅ Search endpoint encontrado: {SEARCH_API}{path[:60]}")
            _inspect_search_response(data, probe_kw)
            # Build a factory for this path
            _path_template = re.sub(r'(text|q|texto)=' + pkw, r'\1={KW}', path)
            _path_template = re.sub(r'date=' + hoje, 'date={DATE}', _path_template)
            _search_endpoint_cache["path"] = path
            _search_endpoint_cache["path_template"] = _path_template
            _search_endpoint_cache["base"] = SEARCH_API
            _search_endpoint_cache["sample"] = data
            fn = _make_search_fn(SEARCH_API, _path_template)
            _search_endpoint_cache["search_fn"] = fn
            return fn

    # Also try workflow API
    print(f"  Tentando workflow API {WORKFLOW_API}...")
    for path in [
        f"/v1/search?q={pkw}&date={hoje}",
        f"/v1/editions?date={hoje}",
        f"/v1/publications?date={hoje}",
    ]:
        data = _probe_search(session, WORKFLOW_API, path)
        if data is not None:
            print(f"  ✅ Workflow endpoint: {WORKFLOW_API}{path[:50]}")
            _search_endpoint_cache["search_fn"] = None
            return None

    print("  ⚠️ Nenhum search endpoint encontrado — usando fallback UUID+PDF")
    _search_endpoint_cache["search_fn"] = None
    return None

def _inspect_search_response(data, keyword):
    """Print a brief summary of the search response structure for diagnostics."""
    if isinstance(data, list):
        print(f"  response: lista com {len(data)} itens")
        if data: print(f"  primeiro item: {list(data[0].keys()) if isinstance(data[0], dict) else type(data[0])}")
    elif isinstance(data, dict):
        print(f"  response: dict {list(data.keys())[:8]}")
        for k in ("total", "count", "results", "items", "content", "data"):
            if k in data:
                v = data[k]
                print(f"  .{k}: {type(v).__name__} len={len(v) if hasattr(v,'__len__') else '-'}")

def _make_search_fn(base, path_template):
    """Factory: returns a function search(keyword, jn, rsn, date) -> list[dict]."""
    def _search(session, keyword, jn, rsn, date):
        kw_enc = requests.utils.quote(keyword)
        jn_enc = requests.utils.quote(jn)
        rsn_enc = requests.utils.quote(rsn)
        path = path_template.replace("{KW}", kw_enc).replace("{DATE}", date)
        # Add journalName / rootSectionName if supported
        if "journalName" not in path:
            path += f"&journalName={jn_enc}&rootSectionName={rsn_enc}"
        try:
            r = session.get(base + path, timeout=15)
            if r.status_code == 200:
                data = r.json()
                return _extract_hits(data, keyword, jn, rsn)
        except Exception as e:
            print(f"  search err: {e}")
        return []
    return _search

def _extract_hits(data, keyword, jn, rsn):
    """
    Normalize search API response to list of reference dicts.
    Handles multiple possible response schemas.
    """
    items = []
    if isinstance(data, list): items = data
    elif isinstance(data, dict):
        for k in ("results","items","content","data","hits","publications","texts","articles"):
            if k in data and isinstance(data[k], list):
                items = data[k]; break
        if not items and "total" in data:
            items = [data]

    refs = []
    kw_low = normalize(keyword)
    for item in items:
        if not isinstance(item, dict): continue
        # Look for keyword in any text field
        text_blob = " ".join(str(v) for v in item.values() if isinstance(v, str))
        if kw_low not in normalize(text_blob): continue
        # Extract standard reference fields
        ref = {
            "keyword": keyword,
            "journal": _find_field(item, ["journalName","journal","caderno","diario"], jn),
            "section": _find_field(item, ["rootSectionName","sectionName","section","secao"], rsn),
            "title":   _find_field(item, ["title","titulo","name","docTitle","documentTitle","heading"], ""),
            "page":    _find_field(item, ["page","pagina","pageNumber","pg"], ""),
            "excerpt": _find_field(item, ["excerpt","trecho","content","text","body","snippet","description"], "")[:300],
            "edition_id": _find_field(item, ["editionId","edition_id","uuid","id"], ""),
            "doc_type": _find_field(item, ["type","tipo","docType","documentType"], ""),
            "date":    _find_field(item, ["date","data","publicationDate","publishedAt"], ""),
            "raw": item,
        }
        refs.append(ref)
    return refs

def _find_field(item, candidates, default=""):
    for c in candidates:
        if c in item and item[c] is not None:
            v = item[c]
            return str(v).strip() if not isinstance(v, (dict,list)) else default
    return default

# ===========================================================================
# SCORING — lightweight for references (no extracted fields)
# ===========================================================================
def score_ref(ref, excerpt_low):
    keyword = ref["keyword"]
    cat     = KEYWORD_CATEGORIES.get(keyword, "general")
    tier    = CATEGORY_TV.get(cat, (3,"",""))[0]
    score   = {1:4, 2:2, 3:0}.get(tier, 0)
    fatores = []

    money = re.search(r"R\$\s*([\d.,]+)", excerpt_low, re.I)
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

    if   score >= 8: verdade = "🟢 APROVADA"
    elif score >= 5: verdade = "🟡 PODE RENDER"
    else:            verdade = "🔴 BACKGROUND"
    return verdade, score, fatores

# ===========================================================================
# REFERENCE CARD BUILDER
# No more fichas. Just a clean reference with a clickable link.
# ===========================================================================
def build_ref_card(ref, date_str, veredito, score, fatores):
    cat   = KEYWORD_CATEGORIES.get(ref["keyword"], "general")
    _, icon, cat_nome = CATEGORY_TV.get(cat, (3, "🔍", "Geral"))
    jn    = ref.get("journal", "")
    rsn   = ref.get("section", "")
    lbl   = next((c["label"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn), rsn)
    emo   = next((c["emoji"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn), "📋")
    fator_str = " · ".join(fatores) if fatores else "—"
    link  = caderno_url(jn or "Executivo", rsn or "Atos Normativos")
    page  = ref.get("page", "")
    title = ref.get("title", "")
    exc   = ref.get("excerpt", "")
    dtype = ref.get("doc_type", "")

    lines = [
        f"{SOURCE_EMOJI} *DOESP {date_str}* | {emo} {lbl}",
        f"{veredito} | {icon} *{cat_nome}*",
        f"🔑 `{ref['keyword']}` | Score {score} | {fator_str}",
        "─" * 22,
    ]
    if dtype:   lines.append(f"📑 {dtype[:80]}")
    if title:   lines.append(f"📄 *{title[:100]}*")
    if page:    lines.append(f"📖 Página {page}")
    if exc:
        # Highlight keyword in excerpt
        hi = re.sub(f"(?i)({re.escape(ref['keyword'])})", r"*\1*", exc[:250])
        lines.append(f"💬 _{hi}_")
    lines.append("─" * 22)
    lines.append(f"🔗 [Abrir no portal]({link})")
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str, mode):
    all_hits = [h for hits in results_by_caderno.values() for h in hits]
    total = len(all_hits)
    ap = sum(1 for h in all_hits if h.get("veredito","").startswith("🟢"))
    pr = sum(1 for h in all_hits if h.get("veredito","").startswith("🟡"))
    lines = [
        f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
        f"_{mode}_",
        f"📋 *{total} ocorrência(s)*\n",
        f"🟢 *Aprovadas:* {ap}",
        f"🟡 *Pode render:* {pr}",
        f"🔴 *Background:* {total-ap-pr}\n",
    ]
    for cad in CADERNOS:
        lbl, emo = cad["label"], cad["emoji"]
        hits = results_by_caderno.get(lbl, [])
        a2 = sum(1 for h in hits if h.get("veredito","").startswith("🟢"))
        p2 = sum(1 for h in hits if h.get("veredito","").startswith("🟡"))
        if hits:
            lines.append(f"{emo} *{lbl}*: {len(hits)} | 🟢{a2} 🟡{p2}")
        else:
            lines.append(f"{emo} *{lbl}*: nenhum")
    lines.append("━"*20)
    # Top hits
    for h in sorted(all_hits, key=lambda x: -x.get("score",0))[:10]:
        cat = KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _, icon, _ = CATEGORY_TV.get(cat,(3,"🔍",""))
        pg = f" p.{h['ref']['page']}" if h["ref"].get("page") else ""
        lbl2 = h["ref"].get("label","")
        lines.append(f"{h.get('veredito','')[:2]} {icon} `{h['ref']['keyword'][:30]}`"
                     f" [{lbl2}]{pg}")
    lines += ["━"*20, f"\n🔗 [Portal DOESP]({PORTAL_URL})"]
    return "\n".join(lines)

# ===========================================================================
# SEARCH-BASED SCAN (primary path)
# ===========================================================================
def scan_via_search(session, search_fn, caderno, date_str):
    jn   = caderno["journalName"]
    rsn  = caderno["rootSectionName"]
    lbl  = caderno["label"]
    hoje = datetime.date.today().isoformat()
    results = []; seen = set()

    for kw in KEYWORDS:
        refs = search_fn(session, kw, jn, rsn, hoje)
        for ref in refs:
            ref["keyword"] = kw
            ref["label"]   = lbl
            ref["journal"]  = ref.get("journal") or jn
            ref["section"]  = ref.get("section") or rsn
            dedup = (kw, ref.get("title",""), ref.get("page",""))
            if dedup in seen: continue
            seen.add(dedup)
            exc_low = normalize(ref.get("excerpt","") + " " + ref.get("title",""))
            veredito, score, fatores = score_ref(ref, exc_low)
            results.append({
                "ref": ref, "veredito": veredito, "score": score, "fatores": fatores,
                "keyword": kw,
            })

    ap = sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr = sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} refs via search | 🟢{ap} 🟡{pr}")
    return results

# ===========================================================================
# UUID DISCOVERY — fallback for when search API not available
# Simplified: only the strategies that worked in previous runs.
# Verbose: prints HTTP status so every failure is diagnosable.
# ===========================================================================
def _fetch_html_once(session):
    if hasattr(session, "_doesp_html"): return session._doesp_html, session._doesp_build_id
    try:
        r = session.get(PORTAL_URL, timeout=20); r.encoding="utf-8"; html=r.text
        print(f"  HTML: HTTP {r.status_code} | {len(html):,} chars")
        if r.status_code != 200: return None, None
    except Exception as e: print(f"  HTML err: {e}"); return None, None
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    bid = m.group(1) if m else ""
    if bid: print(f"  buildId: {bid}")
    session._doesp_html = html; session._doesp_build_id = bid
    return html, bid

def _best_uuid_in(obj, jn, rsn):
    jn_low = jn.lower().replace(" ",""); rsn_low = rsn.lower().replace(" ","")
    def sc(t): t=str(t).lower().replace(" ",""); return (jn_low in t)*2+(rsn_low in t)*3
    def collect(o, ctx, d):
        if d>12: return []
        res=[]
        if isinstance(o,dict):
            cx=" ".join(str(v) for k,v in o.items()
                if k in ("name","journalName","rootSectionName","title","section") and isinstance(v,str))
            s=ctx+sc(cx)
            for k,v in o.items():
                if isinstance(v,str) and re.match(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',v,re.I):
                    res.append((s+sc(k),v))
                else: res.extend(collect(v,s,d+1))
        elif isinstance(o,list):
            for item in o: res.extend(collect(item,ctx,d+1))
        return res
    cands=collect(obj,0,0)
    if not cands: return None
    cands.sort(reverse=True)
    return cands[0][1] if cands[0][0]>0 else (cands[0][1] if len(cands)==1 else None)

def get_uuid_fallback(session, caderno):
    """
    Tries to find the edition UUID when the search API is unavailable.
    Logs every HTTP status so failures are diagnosable in Actions logs.
    """
    jn, rsn, lbl = caderno["journalName"], caderno["rootSectionName"], caderno["label"]
    hoje = datetime.date.today().isoformat()
    jne  = requests.utils.quote(jn)
    rsne = requests.utils.quote(rsn)
    print(f"\n  [UUID fallback] {lbl}")

    html, bid = _fetch_html_once(session)

    # S1: __NEXT_DATA__ (works if SSR)
    if html:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                data = json.loads(m.group(1))
                u = _best_uuid_in(data, jn, rsn)
                if u: print(f"  UUID via __NEXT_DATA__: {u}"); return u
            except: pass

    # S2: ISR JSON data route
    if bid:
        for p in [
            f"sumario.json?journalName={jne}&rootSectionName={rsne}",
            f"sumario.json",
        ]:
            url = f"{PORTAL_BASE}/_next/data/{bid}/{p}"
            try:
                r = session.get(url, timeout=10)
                print(f"  S2 {r.status_code} ...{url[-55:]}")
                if r.status_code==200:
                    u = _best_uuid_in(r.json(), jn, rsn)
                    if u: print(f"  UUID via ISR: {u}"); return u
            except: pass

    # S3: JS bundle scan for API base URL + query
    if html:
        js_srcs = re.findall(r'/_next/static/[^"\'<>\s]+\.js', html)
        found_bases = set()
        for i, src in enumerate(js_srcs[:40]):
            try:
                r = session.get(f"{PORTAL_BASE}{src}", timeout=8)
                if r.status_code != 200: continue
                bases = set(re.findall(r'https?://[a-z0-9.-]+\.doe\.sp\.gov\.br', r.text))
                ed_urls = re.findall(r'https?://[^"\'\\s]+/(?:v\d/)?editions[^"\'\\s]*', r.text)
                for eu in ed_urls:
                    m2 = re.match(r'(https?://[^/]+)', eu)
                    if m2: bases.add(m2.group(1))
                found_bases |= bases
            except: pass
        for base in list(found_bases):
            for path in [
                f"/v1/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
                f"/v1/editions?journalName={jne}&rootSectionName={rsne}",
                f"/api/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            ]:
                try:
                    r = session.get(base+path, timeout=10)
                    ct = r.headers.get("content-type","")
                    print(f"  S3 {r.status_code} {'json' if 'json' in ct else 'html'} {(base+path)[-65:]}")
                    if r.status_code==200 and "json" in ct:
                        u = _best_uuid_in(r.json(), jn, rsn)
                        if u: print(f"  UUID via JS scan: {u}"); return u
                except: pass

    # S4: workflow API (newly discovered from JS bundles)
    for base in [WORKFLOW_API, PDF_API]:
        for path in [
            f"/v1/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/v1/editions?journalName={jne}&rootSectionName={rsne}",
            f"/api/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/v2/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        ]:
            try:
                r = session.get(base+path, timeout=10)
                ct = r.headers.get("content-type","")
                print(f"  S4 {r.status_code} {'json' if 'json' in ct else 'html'} {(base+path)[-65:]}")
                if r.status_code==200 and "json" in ct:
                    u = _best_uuid_in(r.json(), jn, rsn)
                    if u: print(f"  UUID via direct API: {u}"); return u
                    uuids = UUID_RE.findall(json.dumps(r.json()))
                    if uuids: print(f"  UUID candidates: {uuids[:4]}")
            except Exception as e:
                print(f"  S4 exc {base[-30:]}: {e}")

    # S5: scan the caderno-specific URL for any UUID in its HTML
    try:
        cad_url = f"{PORTAL_URL}?journalName={jne}&rootSectionName={rsne}"
        r = session.get(cad_url, timeout=15)
        print(f"  S5 caderno HTML: {r.status_code} | {len(r.text):,} chars")
        if r.status_code==200:
            uuids = list(set(UUID_RE.findall(r.text)))
            if uuids: print(f"  S5 UUIDs: {uuids[:6]}")
            if len(uuids)==1: return uuids[0]
            ed_paths = re.findall(r'/editions/([0-9a-f-]{36})', r.text, re.I)
            if ed_paths: return ed_paths[0]
    except Exception as e: print(f"  S5 err: {e}")

    print(f"  UUID não encontrado para {lbl}")
    return None

# ===========================================================================
# MINIMAL PDF FALLBACK
# When UUID found but search API unavailable:
# Download only first MAX_PDF_PAGES pages to extract structural references.
# ===========================================================================
MAX_PDF_PAGES = 15

def baixar_pdf_parcial(session, uuid):
    url = f"{PDF_API}/v1/editions/{uuid}"
    print(f"  PDF {url[-50:]}")
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code != 200: print(f"  PDF HTTP {r.status_code}"); return None
        data = b"".join(r.iter_content(65536))
        if data[:4] != b"%PDF": return None
        print(f"  PDF OK {len(data)/1e6:.1f} MB"); return data
    except Exception as e: print(f"  PDF err: {e}"); return None

def extract_text_partial(pdf_bytes, label=""):
    """pdfminer 2-col, max MAX_PDF_PAGES pages."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        parts=[]
        for i, page in enumerate(extract_pages(io.BytesIO(pdf_bytes), laparams=lp)):
            if i >= MAX_PDF_PAGES: break
            mid = page.width * 0.52
            boxes = [(el.bbox[3],(el.bbox[0]+el.bbox[2])/2,el.get_text())
                     for el in page if isinstance(el, LTTextBox) and el.get_text().strip()]
            left  = sorted([(y,t) for y,x,t in boxes if x<mid],  reverse=True)
            right = sorted([(y,t) for y,x,t in boxes if x>=mid], reverse=True)
            parts.append("\n".join(t.strip() for _,t in left+right))
            parts.append("\x0c")
        result="\n".join(parts)
        print(f"  text {len(result):,} chars ({label}, {MAX_PDF_PAGES}pp max)")
        return result
    except Exception as e:
        print(f"  pdfminer err: {e}"); return ""

def scan_text_for_refs(full_text, caderno, date_str):
    """
    Window-based scan of extracted text.
    Returns reference dicts compatible with the search-based format.
    """
    fn = normalize(full_text)
    lbl, jn, rsn, emo = (caderno["label"], caderno["journalName"],
                          caderno["rootSectionName"], caderno["emoji"])
    page_breaks = [(m.start(), f"p.{i+2}")
                   for i,m in enumerate(re.finditer(r"\x0c", full_text))]
    def pag(pos):
        p="p.1"
        for pb,pl in page_breaks:
            if pb<=pos: p=pl
            else: break
        return p

    results=[]; seen=set()
    for kw in KEYWORDS:
        kn = normalize(kw)
        sp=0
        while True:
            pos=fn.find(kn,sp)
            if pos==-1: break
            sp=pos+max(len(kn),400)
            dedup=(kw, pag(pos))
            if dedup in seen: continue
            seen.add(dedup)
            win = re.sub(r"\s+"," ", full_text[max(0,pos-150):pos+300]).strip()
            # Find nearest document header
            doc_header = ""
            before = full_text[max(0,pos-800):pos]
            for pat in [
                r'((?:PORTARIA|RESOLUÇÃO|DECRETO|DESPACHO|EXTRATO)\s+[^\n]{10,80})',
                r'((?:PORTARIA|RESOLUCAO|DECRETO)\s+\w[\w\s./]+)',
            ]:
                m = re.findall(pat, before, re.I)
                if m: doc_header = m[-1].strip()[:100]; break
            ref = {
                "keyword": kw, "label": lbl,
                "journal": jn, "section": rsn,
                "title": doc_header,
                "page": pag(pos),
                "excerpt": win[:300],
                "edition_id": "", "doc_type": "",
            }
            exc_low = normalize(win)
            v, sc, ft = score_ref(ref, exc_low)
            results.append({"ref":ref,"veredito":v,"score":sc,"fatores":ft,"keyword":kw})

    ap=sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr=sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} refs via PDF | 🟢{ap} 🟡{pr}")
    return results

# ===========================================================================
# TELEGRAM
# ===========================================================================
_last_send = 0.0

def send_telegram(text, silent=False):
    global _last_send
    gap = time.time()-_last_send
    if gap < 2.0: time.sleep(2.0-gap)
    for _ in range(3):
        try:
            r = requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown",
                      "disable_web_page_preview":True,"disable_notification":silent},
                timeout=15)
            _last_send = time.time()
            if r.status_code==200: return True
            if r.status_code==429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1)
                continue
            print(f"  TG {r.status_code}: {r.text[:80]}"); return False
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
    hoje  = datetime.date.today()
    date_str = hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v4.0 — {date_str} ===\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent": ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                       "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept":          "application/json,text/html,*/*;q=0.9",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin":          "https://doe.sp.gov.br",
        "Referer":         "https://doe.sp.gov.br/",
    })

    # -----------------------------------------------------------------------
    # 1. Discover search endpoint
    # -----------------------------------------------------------------------
    search_fn = discover_search_endpoint(session)
    mode = "search API" if search_fn else "UUID + PDF parcial"
    print(f"\n  Modo: {mode}\n")

    results_by_caderno = {}

    for caderno in CADERNOS:
        lbl, emo = caderno["label"], caderno["emoji"]
        print(f"\n{'─'*60}")
        print(f"{emo}  {caderno['journalName']} / {caderno['rootSectionName']}")
        print(f"{'─'*60}")

        if search_fn:
            # PRIMARY PATH: use search API
            hits = scan_via_search(session, search_fn, caderno, date_str)
        else:
            # FALLBACK: UUID + minimal PDF
            uuid = get_uuid_fallback(session, caderno)
            if not uuid:
                send_telegram(
                    f"⚠️ *{SOURCE_NAME} — {date_str}*\n"
                    f"{emo} [{lbl}] UUID não encontrado\n"
                    f"🔗 [Verificar manualmente]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
                results_by_caderno[lbl] = []; continue
            pdf = baixar_pdf_parcial(session, uuid)
            if not pdf:
                results_by_caderno[lbl] = []; continue
            text = extract_text_partial(pdf, lbl)
            if not text:
                results_by_caderno[lbl] = []; continue
            hits = scan_text_for_refs(text, caderno, date_str)

        results_by_caderno[lbl] = hits
        time.sleep(1)

    # -----------------------------------------------------------------------
    # 2. Summarize
    # -----------------------------------------------------------------------
    total = sum(len(v) for v in results_by_caderno.values())
    print(f"\n{'='*60}\nTOTAL: {total} ({mode})")

    if total == 0:
        # Show what cadernos were found even with 0 results
        lines = [f"✅ *{SOURCE_NAME} — {date_str}* ({mode})",
                 "Nenhuma ocorrência nos cadernos monitorados."]
        for cad in CADERNOS:
            lines.append(f"{cad['emoji']} [{cad['label']}] — "
                         f"[Ver caderno]({caderno_url(cad['journalName'],cad['rootSectionName'])})")
        send_telegram("\n".join(lines))
        return

    all_hits = [h for v in results_by_caderno.values() for h in v]
    send_telegram(build_summary(results_by_caderno, date_str, mode))
    time.sleep(1)

    # 3. Send ref cards: approved first, then pode render, then background (silent)
    aprovadas   = sorted([h for h in all_hits if h["veredito"].startswith("🟢")], key=lambda x: -x["score"])
    pode_render = sorted([h for h in all_hits if h["veredito"].startswith("🟡")], key=lambda x: -x["score"])
    background  =        [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas + pode_render:
        card = build_ref_card(h["ref"], date_str, h["veredito"], h["score"], h["fatores"])
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines = [f"🗂️ *Background DOESP — {date_str}* — {len(background)} ref(s)"]
        for h in background[:20]:
            cat = KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _, icon, _ = CATEGORY_TV.get(cat,(3,"🔍",""))
            ref = h["ref"]
            lbl2 = ref.get("label","")
            pg = f" {ref.get('page','')}" if ref.get("page") else ""
            title = ref.get("title","")[:40]
            lines.append(f"{icon} `{h['keyword'][:30]}` [{lbl2}]{pg} {title}")
        send_telegram("\n".join(lines), silent=True)

if __name__ == "__main__":
    main()
