"""
DOESP Monitor v4.0 — Diário Oficial do Estado de São Paulo
===========================================================
Portal: https://doe.sp.gov.br/sumario

ARQUITETURA v4.0 — Search API primeiro, sem download de PDF enorme
  API confirmada (19/05/2026):
    do-api-web-search.doe.sp.gov.br/v2/publications/attachment/downloadattachment/{id}
    → retorna PDF de publicação individual
  Portanto endpoint de busca deve ser:
    do-api-web-search.doe.sp.gov.br/v2/publications?text=...&journalName=...&date=...

  Se search API retornar JSON → scan_via_search() → referências sem PDF
  Se search API falhar → get_uuid_fallback() → baixa só primeiras 15pp do PDF

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
SEARCH_API   = "https://do-api-web-search.doe.sp.gov.br"
PDF_API      = "https://do-api-publication-pdf.doe.sp.gov.br"
WORKFLOW_API = "https://do-api-publication-workflow.doe.sp.gov.br"
UUID_RE      = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
MAX_PDF_PAGES = 15

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
# HELPERS
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
    return f"{PORTAL_URL}?journalName={requests.utils.quote(jn)}&rootSectionName={requests.utils.quote(rsn)}"

# ===========================================================================
# SEARCH API DISCOVERY
# Confirmed path: /v2/publications/attachment/downloadattachment/{uuid}
# → search endpoint is at /v2/publications?text=...
# ===========================================================================
_search_endpoint_cache = {}

def _inspect_response(data, keyword=""):
    """Print response structure for diagnostics."""
    if isinstance(data, list):
        print(f"    → lista {len(data)} itens")
        if data and isinstance(data[0], dict):
            print(f"    → keys[0]: {list(data[0].keys())[:10]}")
    elif isinstance(data, dict):
        print(f"    → dict keys: {list(data.keys())[:10]}")
        for k in ("total","totalElements","count","results","items","content","data","publications"):
            if k in data:
                v = data[k]
                n = len(v) if hasattr(v,"__len__") else v
                print(f"    → .{k}: {type(v).__name__} {n}")

def discover_search_endpoint(session):
    """
    Probe /v2/publications on do-api-web-search.doe.sp.gov.br with every
    plausible parameter combination. Returns a search callable or None.
    """
    global _search_endpoint_cache
    if "search_fn" in _search_endpoint_cache:
        return _search_endpoint_cache["search_fn"]

    hoje    = datetime.date.today().isoformat()
    hoje_br = datetime.date.today().strftime("%d/%m/%Y")
    kw_enc  = requests.utils.quote("extrato de contrato")
    jn_enc  = requests.utils.quote("Executivo")
    rsn_enc = requests.utils.quote("Atos Normativos")

    # Try Swagger docs first — great for diagnosis
    print(f"\n  Verificando Swagger em {SEARCH_API}...")
    for sw_path in ["/swagger/index.html", "/swagger-ui/index.html",
                    "/v2/api-docs", "/v3/api-docs", "/openapi.json"]:
        try:
            r = session.get(SEARCH_API + sw_path, timeout=8)
            ct = r.headers.get("content-type","")
            if r.status_code == 200:
                print(f"  ✅ Swagger {sw_path}: {ct[:40]}")
                if "json" in ct: print(f"  Swagger: {r.text[:500]}")
            else:
                print(f"  {r.status_code} {sw_path}")
        except Exception as e:
            print(f"  err {sw_path}: {e}")

    # /v2/publications — correct path based on confirmed URL
    # Try every likely search param name and date format
    probes = [
        # text param + publicationDate (most likely for a Spring Boot API)
        f"/v2/publications?text={kw_enc}&journalName={jn_enc}&rootSectionName={rsn_enc}&publicationDate={hoje}",
        f"/v2/publications?text={kw_enc}&journalName={jn_enc}&publicationDate={hoje}",
        f"/v2/publications?text={kw_enc}&publicationDate={hoje}",
        # date param (alternative field name)
        f"/v2/publications?text={kw_enc}&journalName={jn_enc}&date={hoje}",
        f"/v2/publications?text={kw_enc}&date={hoje}",
        # text alone (no date filter)
        f"/v2/publications?text={kw_enc}",
        # q param variants
        f"/v2/publications?q={kw_enc}&journalName={jn_enc}&publicationDate={hoje}",
        f"/v2/publications?q={kw_enc}&publicationDate={hoje}",
        f"/v2/publications?q={kw_enc}",
        # Other param name conventions
        f"/v2/publications?searchText={kw_enc}&publicationDate={hoje}",
        f"/v2/publications?conteudo={kw_enc}&publicationDate={hoje}",
        f"/v2/publications?busca={kw_enc}&publicationDate={hoje}",
        f"/v2/publications?termo={kw_enc}&publicationDate={hoje}",
        # Sub-path: /v2/publications/search
        f"/v2/publications/search?text={kw_enc}&publicationDate={hoje}",
        f"/v2/publications/search?q={kw_enc}&date={hoje}",
        # BR date format
        f"/v2/publications?text={kw_enc}&publicationDate={requests.utils.quote(hoje_br)}",
        # Root listing (no filter — might return today by default)
        f"/v2/publications",
        # v1 fallback
        f"/v1/publications?text={kw_enc}&publicationDate={hoje}",
        f"/v1/publications?q={kw_enc}&date={hoje}",
        # /v2/search as alternative resource
        f"/v2/search?text={kw_enc}&journalName={jn_enc}&publicationDate={hoje}",
        f"/v2/search?q={kw_enc}&publicationDate={hoje}",
    ]

    print(f"\n  Testando {len(probes)} paths em {SEARCH_API}...")
    for path in probes:
        try:
            r = session.get(SEARCH_API + path, timeout=12)
            ct = r.headers.get("content-type","")
            is_json = "json" in ct.lower()
            print(f"  {r.status_code} {('json' if is_json else ct[:12]):<14} {(SEARCH_API+path)[-70:]}")
            if r.status_code == 200 and is_json:
                try:
                    data = r.json()
                    _inspect_response(data, "extrato de contrato")
                    # Build template preserving path structure
                    tmpl = path
                    tmpl = re.sub(r'text=[^&]+',       'text={KW}',   tmpl)
                    tmpl = re.sub(r'q=[^&]+',           'q={KW}',      tmpl)
                    tmpl = re.sub(r'searchText=[^&]+', 'searchText={KW}', tmpl)
                    tmpl = re.sub(r'conteudo=[^&]+',   'conteudo={KW}', tmpl)
                    tmpl = re.sub(r'busca=[^&]+',      'busca={KW}',  tmpl)
                    tmpl = re.sub(r'termo=[^&]+',      'termo={KW}',  tmpl)
                    tmpl = re.sub(re.escape(hoje_br).replace("/",r"[/]"), '{DATE_BR}', tmpl)
                    tmpl = re.sub(re.escape(hoje),     '{DATE}',      tmpl)
                    fn = _make_search_fn(SEARCH_API, tmpl)
                    _search_endpoint_cache["search_fn"] = fn
                    _search_endpoint_cache["template"]  = tmpl
                    return fn
                except Exception as e:
                    print(f"  JSON err: {e}; body={r.text[:100]}")
            elif r.status_code in (401, 403):
                print(f"  → auth/block. Outros paths também falharão.")
                break
        except Exception as e:
            print(f"  exc {path[:50]}: {e}")

    # Probe WORKFLOW API too (maybe it has an editions listing)
    print(f"\n  Testando {WORKFLOW_API}...")
    for path in [
        f"/v1/editions?journalName={jn_enc}&rootSectionName={rsn_enc}&date={hoje}",
        f"/v2/editions?journalName={jn_enc}&rootSectionName={rsn_enc}&date={hoje}",
        f"/v1/editions?date={hoje}",
        f"/v2/editions?date={hoje}",
        f"/v1/publications?text={kw_enc}&date={hoje}",
    ]:
        try:
            r = session.get(WORKFLOW_API + path, timeout=10)
            ct = r.headers.get("content-type","")
            is_json = "json" in ct.lower()
            print(f"  {r.status_code} {('json' if is_json else ct[:12]):<14} {path[:70]}")
            if r.status_code == 200 and is_json:
                data = r.json()
                print(f"  workflow: {str(data)[:300]}")
        except Exception as e:
            print(f"  exc {path[:40]}: {e}")

    print("  ⚠️ Nenhum search endpoint JSON encontrado — fallback UUID+PDF")
    _search_endpoint_cache["search_fn"] = None
    return None

def _make_search_fn(base, path_template):
    def _search(session, keyword, jn, rsn, date, date_br):
        path = path_template
        path = path.replace("{KW}",      requests.utils.quote(keyword))
        path = path.replace("{DATE}",    date)
        path = path.replace("{DATE_BR}", requests.utils.quote(date_br))
        # If journalName/rootSectionName not in template, add them
        if "{JN}" in path:
            path = path.replace("{JN}",  requests.utils.quote(jn))
        if "{RSN}" in path:
            path = path.replace("{RSN}", requests.utils.quote(rsn))
        if "journalName" not in path:
            path += f"&journalName={requests.utils.quote(jn)}&rootSectionName={requests.utils.quote(rsn)}"
        try:
            r = session.get(base + path, timeout=15)
            if r.status_code == 200:
                ct = r.headers.get("content-type","")
                if "json" in ct:
                    return _extract_hits(r.json(), keyword, jn, rsn)
        except Exception as e:
            print(f"  search err: {e}")
        return []
    return _search

def _find_field(item, candidates, default=""):
    for c in candidates:
        v = item.get(c)
        if v is not None and str(v).strip():
            return str(v).strip() if not isinstance(v,(dict,list)) else default
    return default

def _extract_hits(data, keyword, jn, rsn):
    items = []
    if isinstance(data, list): items = data
    elif isinstance(data, dict):
        for k in ("results","items","content","data","hits","publications","texts","articles","list"):
            if k in data and isinstance(data[k], list): items = data[k]; break
        if not items: items = [data] if data else []

    kw_low = normalize(keyword)
    refs = []
    for item in items:
        if not isinstance(item, dict): continue
        text_blob = " ".join(str(v) for v in item.values() if isinstance(v, str))
        if kw_low not in normalize(text_blob): continue
        refs.append({
            "keyword":    keyword,
            "journal":    _find_field(item, ["journalName","journal","caderno","diario"], jn),
            "section":    _find_field(item, ["rootSectionName","sectionName","section","secao"], rsn),
            "title":      _find_field(item, ["title","titulo","name","docTitle","heading","nomeAto"], ""),
            "page":       _find_field(item, ["page","pagina","pageNumber","numeroPagina","pg"], ""),
            "excerpt":    _find_field(item, ["content","text","body","snippet","excerpt","trecho","conteudo","texto"], "")[:400],
            "edition_id": _find_field(item, ["editionId","edition_id","edicaoId","uuid","id"], ""),
            "attach_id":  _find_field(item, ["attachmentId","attachment_id","arquivoId","fileId"], ""),
            "doc_type":   _find_field(item, ["type","tipo","docType","tipoAto"], ""),
            "date":       _find_field(item, ["date","data","publicationDate","dataPublicacao"], ""),
        })
    return refs

# ===========================================================================
# SCORING
# ===========================================================================
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
    lbl   = next((c["label"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn), rsn or jn)
    emo   = next((c["emoji"] for c in CADERNOS if c["journalName"]==jn and c["rootSectionName"]==rsn), "📋")
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
    if ref.get("date"):     lines.append(f"📅 {ref['date'][:20]}")
    if ref.get("excerpt"):
        hi = re.sub(f"(?i)({re.escape(ref['keyword'])})", r"*\1*", ref["excerpt"][:280])
        lines.append(f"💬 _{hi}_")
    # Attachment download link if available
    if ref.get("attach_id"):
        dl_url = f"{SEARCH_API}/v2/publications/attachment/downloadattachment/{ref['attach_id']}"
        lines.append(f"📥 [Baixar ato]({dl_url})")
    lines.append("─" * 22)
    lines.append(f"🔗 [Abrir no portal]({link})")
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str, mode):
    all_hits = [h for v in results_by_caderno.values() for h in v]
    total = len(all_hits)
    ap = sum(1 for h in all_hits if h.get("veredito","").startswith("🟢"))
    pr = sum(1 for h in all_hits if h.get("veredito","").startswith("🟡"))
    lines = [
        f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
        f"_{mode}_",
        f"📋 *{total} ocorrência(s)*\n",
        f"🟢 Aprovadas: {ap}  🟡 Pode render: {pr}  🔴 Background: {total-ap-pr}\n",
    ]
    for cad in CADERNOS:
        lbl, emo = cad["label"], cad["emoji"]
        hits = results_by_caderno.get(lbl, [])
        a2 = sum(1 for h in hits if h.get("veredito","").startswith("🟢"))
        p2 = sum(1 for h in hits if h.get("veredito","").startswith("🟡"))
        lines.append(f"{emo} *{lbl}*: {len(hits)} | 🟢{a2} 🟡{p2}" if hits
                     else f"{emo} *{lbl}*: nenhum")
    lines.append("━"*20)
    for h in sorted(all_hits, key=lambda x: -x.get("score",0))[:10]:
        cat = KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general")
        _, icon, _ = CATEGORY_TV.get(cat,(3,"🔍",""))
        pg  = f" p.{h['ref']['page']}" if h["ref"].get("page") else ""
        ttl = (h["ref"].get("title") or "")[:35]
        lines.append(f"{h.get('veredito','')[:2]} {icon} `{h['ref']['keyword'][:30]}`"
                     f" [{h['ref'].get('label','')}]{pg} {ttl}")
    lines += ["━"*20, f"\n🔗 [Portal DOESP]({PORTAL_URL})"]
    return "\n".join(lines)

# ===========================================================================
# SEARCH-BASED SCAN
# ===========================================================================
def scan_via_search(session, search_fn, caderno, date_str):
    jn, rsn = caderno["journalName"], caderno["rootSectionName"]
    lbl     = caderno["label"]
    hoje    = datetime.date.today().isoformat()
    hoje_br = datetime.date.today().strftime("%d/%m/%Y")
    results = []; seen = set()
    kw_hits = {}

    for kw in KEYWORDS:
        refs = search_fn(session, kw, jn, rsn, hoje, hoje_br)
        kw_hits[kw] = len(refs)
        for ref in refs:
            ref["keyword"] = kw; ref["label"] = lbl
            ref["journal"] = ref.get("journal") or jn
            ref["section"] = ref.get("section") or rsn
            dedup = (kw, ref.get("title",""), ref.get("page",""))
            if dedup in seen: continue
            seen.add(dedup)
            exc_low = normalize((ref.get("excerpt","") + " " + ref.get("title","")).lower())
            v, sc, ft = score_ref(ref, exc_low)
            results.append({"ref":ref,"veredito":v,"score":sc,"fatores":ft,"keyword":kw})

    hit_kws = {k:v for k,v in kw_hits.items() if v}
    if hit_kws: print(f"  {lbl} hits: {hit_kws}")
    ap = sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr = sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  {lbl}: {len(results)} refs via search | 🟢{ap} 🟡{pr}")
    return results

# ===========================================================================
# UUID FALLBACK
# ===========================================================================
def _fetch_html_once(session):
    if hasattr(session,"_doesp_html"): return session._doesp_html, session._doesp_build_id
    try:
        r = session.get(PORTAL_URL, timeout=20); r.encoding="utf-8"; html=r.text
        print(f"  HTML: {r.status_code} | {len(html):,} chars")
        if r.status_code!=200: return None,None
    except Exception as e: print(f"  HTML err: {e}"); return None,None
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    bid = m.group(1) if m else ""
    session._doesp_html=html; session._doesp_build_id=bid
    return html, bid

def _best_uuid_in(obj, jn, rsn):
    jn_l=jn.lower().replace(" ",""); rsn_l=rsn.lower().replace(" ","")
    def sc(t): t=str(t).lower().replace(" ",""); return (jn_l in t)*2+(rsn_l in t)*3
    def col(o,ctx,d):
        if d>12: return []
        res=[]
        if isinstance(o,dict):
            cx=" ".join(str(v) for k,v in o.items() if k in
               ("name","journalName","rootSectionName","title","section") and isinstance(v,str))
            s=ctx+sc(cx)
            for k,v in o.items():
                if isinstance(v,str) and re.match(r'^[0-9a-f-]{36}$',v,re.I): res.append((s+sc(k),v))
                else: res.extend(col(v,s,d+1))
        elif isinstance(o,list):
            for item in o: res.extend(col(item,ctx,d+1))
        return res
    cands=col(obj,0,0)
    if not cands: return None
    cands.sort(reverse=True)
    return cands[0][1] if cands[0][0]>0 else (cands[0][1] if len(cands)==1 else None)

def get_uuid_fallback(session, caderno):
    jn, rsn, lbl = caderno["journalName"], caderno["rootSectionName"], caderno["label"]
    hoje = datetime.date.today().isoformat()
    jne  = requests.utils.quote(jn); rsne = requests.utils.quote(rsn)
    print(f"\n  [UUID fallback] {lbl}")

    html, bid = _fetch_html_once(session)

    # S1: __NEXT_DATA__
    if html:
        m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.DOTALL)
        if m:
            try:
                u = _best_uuid_in(json.loads(m.group(1)), jn, rsn)
                if u: print(f"  UUID via __NEXT_DATA__: {u}"); return u
            except: pass

    # S2: ISR data route
    if bid:
        for p in [f"sumario.json?journalName={jne}&rootSectionName={rsne}", "sumario.json"]:
            try:
                r = session.get(f"{PORTAL_BASE}/_next/data/{bid}/{p}", timeout=10)
                print(f"  S2 {r.status_code} ...{p[-50:]}")
                if r.status_code==200:
                    u = _best_uuid_in(r.json(), jn, rsn)
                    if u: print(f"  UUID via ISR: {u}"); return u
            except: pass

    # S3: PDF API v1 + v2 editions listing
    for base in [PDF_API]:
        for ver in ["v1","v2"]:
            for path in [
                f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
                f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}",
                f"/{ver}/editions?date={hoje}",
            ]:
                try:
                    r = session.get(base+path, timeout=10)
                    ct = r.headers.get("content-type","")
                    is_json = "json" in ct.lower()
                    print(f"  S3 {r.status_code} {'json' if is_json else 'html':<4} {(base+path)[-65:]}")
                    if r.status_code==200 and is_json:
                        u = _best_uuid_in(r.json(), jn, rsn)
                        if u: print(f"  UUID via PDF API: {u}"); return u
                        uuids = UUID_RE.findall(json.dumps(r.json()))
                        if uuids: print(f"  UUID candidates: {uuids[:4]}")
                except: pass

    # S4: workflow API v1 + v2
    for ver in ["v1","v2"]:
        for path in [
            f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/{ver}/editions?journalName={jne}&rootSectionName={rsne}",
        ]:
            try:
                r = session.get(WORKFLOW_API+path, timeout=10)
                ct = r.headers.get("content-type","")
                is_json = "json" in ct.lower()
                print(f"  S4 {r.status_code} {'json' if is_json else 'html':<4} {(WORKFLOW_API+path)[-65:]}")
                if r.status_code==200 and is_json:
                    u = _best_uuid_in(r.json(), jn, rsn)
                    if u: print(f"  UUID via workflow: {u}"); return u
                    uuids = UUID_RE.findall(json.dumps(r.json()))
                    if uuids: print(f"  UUID candidates: {uuids[:4]}")
            except: pass

    # S5: scan caderno-specific HTML for UUIDs
    try:
        r = session.get(f"{PORTAL_URL}?journalName={jne}&rootSectionName={rsne}", timeout=15)
        print(f"  S5 caderno HTML: {r.status_code} | {len(r.text):,} chars")
        if r.status_code==200:
            uuids = list(set(UUID_RE.findall(r.text)))
            ed_paths = re.findall(r'/editions/([0-9a-f-]{36})', r.text, re.I)
            if uuids:   print(f"  S5 UUIDs: {uuids[:6]}")
            if ed_paths: return ed_paths[0]
            if len(uuids)==1: return uuids[0]
    except Exception as e: print(f"  S5 err: {e}")

    print(f"  UUID não encontrado para {lbl}")
    return None

# ===========================================================================
# PDF FALLBACK (minimal download — first MAX_PDF_PAGES pages only)
# ===========================================================================
def baixar_pdf_parcial(session, uuid):
    url = f"{PDF_API}/v1/editions/{uuid}"
    print(f"  PDF {url[-55:]}")
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code!=200: print(f"  PDF HTTP {r.status_code}"); return None
        data = b"".join(r.iter_content(65536))
        if data[:4]!=b"%PDF": return None
        print(f"  PDF OK {len(data)/1e6:.1f}MB"); return data
    except Exception as e: print(f"  PDF err: {e}"); return None

def extract_text_partial(pdf_bytes, label=""):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        parts=[]
        for i,page in enumerate(extract_pages(io.BytesIO(pdf_bytes), laparams=lp)):
            if i>=MAX_PDF_PAGES: break
            mid=page.width*0.52
            boxes=[(el.bbox[3],(el.bbox[0]+el.bbox[2])/2,el.get_text())
                   for el in page if isinstance(el,LTTextBox) and el.get_text().strip()]
            left  = sorted([(y,t) for y,x,t in boxes if x<mid],  reverse=True)
            right = sorted([(y,t) for y,x,t in boxes if x>=mid], reverse=True)
            parts.append("\n".join(t.strip() for _,t in left+right))
            parts.append("\x0c")
        result="\n".join(parts)
        print(f"  texto {len(result):,} chars ({MAX_PDF_PAGES}pp max)")
        return result
    except Exception as e:
        print(f"  pdfminer err: {e}"); return ""

def scan_text_for_refs(full_text, caderno):
    fn=normalize(full_text); lbl=caderno["label"]; jn=caderno["journalName"]; rsn=caderno["rootSectionName"]
    page_breaks=[(m.start(),f"p.{i+2}") for i,m in enumerate(re.finditer(r"\x0c",full_text))]
    def pag(pos):
        p="p.1"
        for pb,pl in page_breaks:
            if pb<=pos: p=pl
            else: break
        return p
    results=[]; seen=set()
    for kw in KEYWORDS:
        kn=normalize(kw); sp=0
        while True:
            pos=fn.find(kn,sp)
            if pos==-1: break
            sp=pos+max(len(kn),400)
            dedup=(kw,pag(pos))
            if dedup in seen: continue
            seen.add(dedup)
            win=re.sub(r"\s+"," ",full_text[max(0,pos-150):pos+300]).strip()
            doc_header=""
            before=full_text[max(0,pos-800):pos]
            for pat in [r'((?:PORTARIA|RESOLUÇÃO|DECRETO|DESPACHO|EXTRATO)\s+[^\n]{10,80})',]:
                m=re.findall(pat,before,re.I)
                if m: doc_header=m[-1].strip()[:100]; break
            ref={"keyword":kw,"label":lbl,"journal":jn,"section":rsn,
                 "title":doc_header,"page":pag(pos),"excerpt":win[:300],
                 "edition_id":"","attach_id":"","doc_type":"","date":""}
            v,sc,ft=score_ref(ref, normalize(win))
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
    gap=time.time()-_last_send
    if gap<2.0: time.sleep(2.0-gap)
    for _ in range(3):
        try:
            r=requests.post(
                f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown",
                      "disable_web_page_preview":True,"disable_notification":silent},
                timeout=15)
            _last_send=time.time()
            if r.status_code==200: return True
            if r.status_code==429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1); continue
            print(f"  TG {r.status_code}: {r.text[:60]}"); return False
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
    print(f"=== {SOURCE_NAME} Monitor v4.0 — {date_str} ===\n")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,
                            "text":f"⏳ DOESP v4.0 iniciado — {date_str}",
                            "disable_notification":True}, timeout=10)
    except: pass

    session = requests.Session()
    session.headers.update({
        "User-Agent":("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept":          "application/json,text/html,*/*;q=0.9",
        "Accept-Language": "pt-BR,pt;q=0.9",
        "Origin":          "https://doe.sp.gov.br",
        "Referer":         "https://doe.sp.gov.br/",
    })

    search_fn = discover_search_endpoint(session)
    mode = "search API ✅" if search_fn else "UUID + PDF parcial"
    print(f"\n  Modo: {mode}\n")

    results_by_caderno = {}
    for caderno in CADERNOS:
        lbl, emo = caderno["label"], caderno["emoji"]
        print(f"\n{'─'*60}")
        print(f"{emo}  {caderno['journalName']} / {caderno['rootSectionName']}")
        print(f"{'─'*60}")

        if search_fn:
            hits = scan_via_search(session, search_fn, caderno, date_str)
        else:
            uuid = get_uuid_fallback(session, caderno)
            if not uuid:
                send_telegram(
                    f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\n"
                    f"UUID não encontrado. API ainda inacessível.\n"
                    f"🔗 [Verificar manualmente]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
                results_by_caderno[lbl]=[]; continue
            pdf = baixar_pdf_parcial(session, uuid)
            if not pdf: results_by_caderno[lbl]=[]; continue
            text = extract_text_partial(pdf, lbl)
            if not text: results_by_caderno[lbl]=[]; continue
            hits = scan_text_for_refs(text, caderno)

        results_by_caderno[lbl] = hits
        time.sleep(1)

    total = sum(len(v) for v in results_by_caderno.values())
    print(f"\n{'='*60}\nTOTAL: {total} ({mode})")

    if total==0:
        lines=[f"✅ *{SOURCE_NAME} — {date_str}*",
               f"Modo: {mode}",
               "Nenhuma ocorrência nos cadernos monitorados."]
        for cad in CADERNOS:
            lines.append(f"{cad['emoji']} [{cad['label']}] "
                         f"[Abrir caderno]({caderno_url(cad['journalName'],cad['rootSectionName'])})")
        send_telegram("\n".join(lines)); return

    all_hits = [h for v in results_by_caderno.values() for h in v]
    send_telegram(build_summary(results_by_caderno, date_str, mode))
    time.sleep(1)

    aprovadas   = sorted([h for h in all_hits if h["veredito"].startswith("🟢")], key=lambda x:-x["score"])
    pode_render = sorted([h for h in all_hits if h["veredito"].startswith("🟡")], key=lambda x:-x["score"])
    background  =        [h for h in all_hits if h["veredito"].startswith("🔴")]

    for h in aprovadas + pode_render:
        card = build_ref_card(h["ref"], date_str, h["veredito"], h["score"], h["fatores"])
        for part in split_long(card): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines=[f"🗂️ *Background DOESP — {date_str}* — {len(background)} ref(s)"]
        for h in background[:20]:
            cat=KEYWORD_CATEGORIES.get(h["keyword"],"general")
            _,icon,_=CATEGORY_TV.get(cat,(3,"🔍",""))
            r=h["ref"]; pg=f" {r.get('page','')}" if r.get("page") else ""
            ttl=(r.get("title") or "")[:35]
            lines.append(f"{icon} `{h['keyword'][:30]}` [{r.get('label','')}]{pg} {ttl}")
        send_telegram("\n".join(lines), silent=True)

if __name__=="__main__":
    main()
