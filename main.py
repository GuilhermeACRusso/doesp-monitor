"""
DOESP Monitor v3.2 — Diário Oficial do Estado de São Paulo
===========================================================
Portal: https://doe.sp.gov.br/sumario

ARQUITETURA v3.2 — incorpora DOC-SP Monitor v9.3:
  ANTES (v3.1): janelas ao redor de keyword no texto corrido.
    • Janela cruzava atos (extrai dados do ato errado)
    • Sem deduplicação por ato
    • Sem tracking de Secretaria

  AGORA (v3.2): SEGMENTAÇÃO DE ATOS + EXTRAÇÃO TYPE-AWARE
    1. segment_doesp_atos()
         SECRETARIA headers → contexto herdado pelo próximo ato
         PORTARIA/RESOLUÇÃO/DECRETO/DESPACHO/EXTRATO → fronteiras de ato
         Cada ato: {tipo, secretaria, sei, body, page}
         Validado: ~21 atos/Normativos, ~82/Pessoal, ~32/Gestão
    2. extract_fields_from_ato()  dispatcher por tipo:
         APLICA penalidade → servidor + RG + tipo de pena
         Portaria/Resolução → nomeação, exoneração, cargo
         Extrato de Contrato → empresa, CNPJ, valor, objeto, prazo
         Despacho disciplinar → PAD/sindicância, SEI, interessado
         Convênio/Chamamento → OS, valor, secretaria
         Fallback → regex genérico
    3. scan_atos()  — atos-first (Executivo cadernos)
         Pré-extrai campos uma vez por ato, deduplica por (ato_hash, cat)
         Municípios usa window-based (sem estrutura de atos)
    4. tv_score()  — scoring estado (ported+adapted de DOC-SP v9.3)
    5. build_ficha()  — cartão factual (ported+adapted de DOC-SP v9.3)
    6. build_digesto()  — background (ported de DOC-SP v9.3)

  SEI validado: DDD.DDDDDDDD/YYYY-DD  ex: 006.00211365/2026-46
  Servidor: "ao ex-servidor NOME, RG. N.º XX.XXX.XXX-X"

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
TG_MAX       = 4000
SOURCE_NAME  = "DOESP"
SOURCE_EMOJI = "📋"
PORTAL_URL   = "https://doe.sp.gov.br/sumario"
PDF_API_BASE = "https://do-api-publication-pdf.doe.sp.gov.br"
ADMIN_API    = "https://do-api-admin-edition.doe.sp.gov.br"
WINDOW_SIDE  = 1200

UUID_RE      = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)
UUID_FULL_RE = re.compile(r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$', re.I)

# ===========================================================================
# KEYWORDS
# ===========================================================================
KEYWORD_CATEGORIES = {
    # TIER 1
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
    # TIER 2
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
    # TIER 3
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
    "obras":         (2, "🏗️", "Obras/Infraestrutura"),
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

KEYWORD_FILTERS = {
    "extrato de contrato": {
        "min_value":100_000, "max_hits":20,
        "require_any":["cnpj","contratad","objeto","contratante"],
    },
    "termo de aditamento": {
        "max_hits":15, "require_any":["cnpj","contratad","valor","objeto"],
    },
    "concorrência eletrônica": {
        "min_value":500_000,
        "require_any":["r$","contratad","adjudic","homolog"],
        "skip_if":["aviso de licitação","torna público","encontra-se aberto",
                   "recebimento das propostas"],
    },
    "pavimentação": {
        "require_any":["r$","contrat","obra","adjudic","homolog"],
        "skip_if":["aviso de licitação","torna público para conhecimento"],
    },
    "recapeamento asfáltico": {"require_any":["r$","contrat","adjudic","homolog"]},
    "inexigibilidade de licitação": {"min_value":50_000},
    "dispensa de licitação": {
        "max_hits":15,
        "require_any":["autorizo","homologo","contratad","valor","objeto"],
        "skip_if":["resultou fracassada"],
    },
    "aplicação de penalidade": {
        "min_value":5_000,
        "require_any":["aplico","notifico","suspensão","multa","pena pecuniária"],
        "skip_if":["deixo de aplicar"],
        "max_hits_per_cnpj":3,
    },
    "organização social de saúde": {"require_any":["contrato de gestão","os ","spdm","hospital"]},
    "contrato de gestão": {"require_any":["organização social","os ","spdm","santa casa","objeto"]},
    "CETESB": {"require_any":["multa","embargo","auto de infração","licença"]},
    "dengue": {"require_any":["caso","foco","combate","surto","contrato"],"skip_if":["projeto de lei"]},
    "superfaturamento": {
        "require_any":["apurou","indício","constatou","investigação","TCE","MP "],
        "skip_if":["evitar superfaturamento","vedado o superfaturamento"],
    },
    "sobrepreço": {
        "require_any":["apurou","indício","constatou","investigação"],
        "skip_if":["evitar contratações com sobrepreço"],
    },
    "sindicância": {
        "max_hits":10,
        "require_any":["instaurar","instaurada","instaurado","conclusão","arquivada",
                       "pena","portaria","comissão processante","aplico"],
        "skip_if":["sindicância patrimonial"],
    },
    "processo administrativo disciplinar": {
        "max_hits":8,
        "require_any":["instaurado","instaurada","instaurar","corregedoria",
                       "policial penal","demissão","suspensão","aplico","sap n"],
    },
    "demissão de servidor":             {"max_hits":5},
    "aposentadoria compulsória":        {"max_hits":5},
    "nomeação para cargo em comissão":  {"max_hits":8},
    "exoneração a pedido":              {"max_hits":8},
    "exoneração de servidor":           {"max_hits":8},
}

# ===========================================================================
# UUID DISCOVERY — 5 estratégias
# ===========================================================================
def _all_uuids_in_obj(obj, depth=0):
    found = []
    if depth > 15: return found
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(v, str) and UUID_FULL_RE.match(v): found.append((k, v))
            else: found.extend(_all_uuids_in_obj(v, depth+1))
    elif isinstance(obj, list):
        for item in obj: found.extend(_all_uuids_in_obj(item, depth+1))
    return found

def _best_uuid(obj, jn, rsn):
    jn_low = jn.lower(); rsn_low = rsn.lower().replace(" ","")
    def score(text):
        t = str(text).lower().replace(" ",""); s = 0
        if jn_low in t: s += 2
        if rsn_low in t: s += 3
        for w in rsn_low.split():
            if len(w) > 4 and w in t: s += 1
        return s
    def collect(o, ctx, d):
        if d > 15: return []
        res = []
        if isinstance(o, dict):
            cx = " ".join(str(v) for k,v in o.items()
                if k in ("name","nome","journalName","rootSectionName","title","section","type")
                and isinstance(v, str))
            s = ctx + score(cx)
            for k, v in o.items():
                if isinstance(v, str) and UUID_FULL_RE.match(v): res.append((s+score(k), v))
                else: res.extend(collect(v, s, d+1))
        elif isinstance(o, list):
            for item in o: res.extend(collect(item, ctx, d+1))
        return res
    cands = collect(obj, 0, 0)
    if not cands: return None
    cands.sort(reverse=True)
    return cands[0][1] if cands[0][0] > 0 else (cands[0][1] if len(cands)==1 else None)

def _fetch_html_once(session):
    if hasattr(session, "_doesp_html"): return session._doesp_html, session._doesp_build_id
    try:
        r = session.get(PORTAL_URL, timeout=20); r.encoding = "utf-8"; html = r.text
        print(f"  HTML: HTTP {r.status_code} | {len(html):,} chars")
        if r.status_code != 200: return None, None
        try:
            with open("/tmp/doesp_sumario.html","w",errors="replace") as f: f.write(html)
        except: pass
    except Exception as e:
        print(f"  HTML error: {e}"); return None, None
    m = re.search(r'"buildId"\s*:\s*"([^"]+)"', html)
    build_id = m.group(1) if m else ""
    if build_id: print(f"  buildId: {build_id}")
    session._doesp_html = html; session._doesp_build_id = build_id
    return html, build_id

def _strat1(session, html, jn, rsn):
    m = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>', html, re.DOTALL)
    if not m: return None
    try: data = json.loads(m.group(1))
    except: return None
    # FIX: never bail on empty pageProps — CSR apps always have pageProps:{}.
    # Scan ALL of __NEXT_DATA__ incl. runtimeConfig, query, buildManifest.
    keys = list(data.keys())
    print(f"    __NEXT_DATA__ keys: {keys[:8]}")
    # Extract any API base URL from runtimeConfig
    for key in ("API_BASE_URL","NEXT_PUBLIC_API_URL","API_URL","publicRuntimeConfig","runtimeConfig"):
        val = data.get(key) or data.get("props",{}).get(key)
        if val: print(f"    cfg.{key}: {str(val)[:120]}")
    u = _best_uuid(data, jn, rsn)
    if u: print(f"    ✅ S1: {u}")
    return u

def _strat2(session, build_id, jn, rsn):
    if not build_id: return None
    jne, rsne = requests.utils.quote(jn), requests.utils.quote(rsn)
    # Try multiple ISR/SSG data path patterns for this Next.js app
    paths = [
        f"sumario.json?journalName={jne}&rootSectionName={rsne}",
        f"sumario.json",
        f"sumario/{jne}/{rsne}.json",
        f"sumario/{jne}.json",
    ]
    for p in paths:
        url = f"https://doe.sp.gov.br/_next/data/{build_id}/{p}"
        try:
            r = session.get(url, timeout=10)
            print(f"    S2 {r.status_code} {url[-60:]}")
            if r.status_code == 200:
                try: data = r.json()
                except: continue
                u = _best_uuid(data, jn, rsn)
                if u: print(f"    ✅ S2: {u}"); return u
                all_u = _all_uuids_in_obj(data)
                if all_u: print(f"    S2 uuids: {[v for _,v in all_u[:4]]}")
                if len(all_u) == 1: return all_u[0][1]
        except: pass
    return None

def _strat3(session, html, jn, rsn):
    """Scan all JS bundles for any doe.sp.gov.br API base URL. Verbose logging."""
    hoje = datetime.date.today().isoformat()
    jne  = requests.utils.quote(jn)
    rsne = requests.utils.quote(rsn)
    js_srcs = re.findall(r'/_next/static/[^"\'<>\s]+\.js', html)
    print(f"    S3: {len(js_srcs)} JS files in HTML")
    found_bases = set()
    for i, src in enumerate(js_srcs[:40]):
        try:
            r = session.get(f"https://doe.sp.gov.br{src}", timeout=8)
            if r.status_code != 200:
                if i < 4: print(f"    S3 bundle[{i}] HTTP {r.status_code}")
                continue
            js = r.text
            # Broad: any subdomain of doe.sp.gov.br
            bases  = set(re.findall(r'https?://[a-z0-9.-]+\.doe\.sp\.gov\.br', js))
            bases |= set(re.findall(r'https?://do-api[a-z0-9.-]+\.sp\.gov\.br', js))
            # Partial domain tokens (catches split strings in minified JS)
            partials = re.findall(r'"(do-api[a-z0-9-]+)"', js)
            if partials: print(f"    S3 partial tokens: {partials[:4]}")
            # Full /editions/ URLs
            ed_urls = re.findall(r'https?://[^"\'\s]+/(?:v\d/)?editions[^"\'\s]*', js)
            if ed_urls: print(f"    S3 editions URLs: {ed_urls[:3]}")
            for eu in ed_urls:
                m = re.match(r'(https?://[^/]+)', eu)
                if m: bases.add(m.group(1))
            found_bases |= bases
            if bases and i < 5: print(f"    S3 bundle[{i}] bases: {list(bases)[:3]}")
        except Exception as e:
            if i < 4: print(f"    S3 bundle[{i}] err: {e}")
    if not found_bases:
        print("    S3: no API bases found in any JS bundle")
        return None
    print(f"    S3 total bases: {list(found_bases)[:5]}")
    for api_base in list(found_bases):
        for path in [
            f"/v1/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/v1/editions?journalName={jne}&rootSectionName={rsne}",
            f"/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
            f"/v1/editions?date={hoje}",
        ]:
            try:
                r = session.get(api_base + path, timeout=10)
                print(f"    S3 API {r.status_code} {(api_base+path)[-70:]}")
                if r.status_code == 200:
                    try: data = r.json()
                    except: continue
                    u = _best_uuid(data, jn, rsn)
                    if u: print(f"    ok S3: {u}"); return u
                    all_u = _all_uuids_in_obj(data)
                    if all_u:
                        print(f"    S3 uuids: {[v for _,v in all_u[:4]]}")
                        if len(all_u) == 1: return all_u[0][1]
            except: pass
    return None

def _strat4(session, jn, rsn):
    """Hardcoded API domains - verbose HTTP status for every call."""
    hoje = datetime.date.today().isoformat()
    jne  = requests.utils.quote(jn)
    rsne = requests.utils.quote(rsn)
    urls = [
        f"{PDF_API_BASE}/v1/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        f"{PDF_API_BASE}/v1/editions?journalName={jne}&rootSectionName={rsne}",
        f"{PDF_API_BASE}/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        f"{PDF_API_BASE}/v1/editions/today?journalName={jne}&rootSectionName={rsne}",
        f"{ADMIN_API}/v1/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        f"{ADMIN_API}/v1/editions?journalName={jne}&rootSectionName={rsne}",
        f"{ADMIN_API}/api/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        f"{ADMIN_API}/v1/editions/today?journalName={jne}&rootSectionName={rsne}",
    ]
    for url in urls:
        try:
            r = session.get(url, timeout=10)
            print(f"    S4 {r.status_code} {url[8:][-70:]}")
            if r.status_code == 200:
                try: data = r.json()
                except:
                    print(f"    S4 non-JSON: {r.text[:80]}")
                    continue
                u = _best_uuid(data, jn, rsn)
                if u: print(f"    ok S4: {u}"); return u
                all_u = _all_uuids_in_obj(data)
                print(f"    S4 200 uuids: {[v for _,v in all_u[:4]]}")
                if len(all_u) == 1: return all_u[0][1]
            elif r.status_code not in (404, 403):
                print(f"    S4 body: {r.text[:80]}")
        except Exception as e:
            print(f"    S4 exc {url[-40:]}: {e}")
    return None

def _strat5(session, jn, rsn):
    """Same-domain API + fetch the caderno-specific page and scan for UUIDs in HTML."""
    hoje = datetime.date.today().isoformat()
    jne, rsne = requests.utils.quote(jn), requests.utils.quote(rsn)
    # A: Same-domain API routes
    for url in [
        f"https://doe.sp.gov.br/api/editions?journalName={jne}&rootSectionName={rsne}&date={hoje}",
        f"https://doe.sp.gov.br/api/editions?journalName={jne}&rootSectionName={rsne}",
        f"https://doe.sp.gov.br/api/cadernos?date={hoje}",
        f"https://doe.sp.gov.br/api/sumario?journalName={jne}&rootSectionName={rsne}",
        f"https://doe.sp.gov.br/api/publication?journalName={jne}&rootSectionName={rsne}&date={hoje}",
    ]:
        try:
            r = session.get(url, timeout=10)
            print(f"    S5a {r.status_code} {url[-65:]}")
            if r.status_code == 200:
                try: data = r.json()
                except: continue
                u = _best_uuid(data, jn, rsn)
                if u: print(f"    ✅ S5a: {u}"); return u
                all_u = _all_uuids_in_obj(data)
                if all_u: print(f"    S5a uuids: {[v for _,v in all_u[:4]]}")
                if len(all_u) == 1: return all_u[0][1]
        except: pass
    # B: Fetch the specific caderno URL and scan its HTML for UUIDs
    caderno_url = f"https://doe.sp.gov.br/sumario?journalName={jne}&rootSectionName={rsne}"
    try:
        r = session.get(caderno_url, timeout=15)
        print(f"    S5b caderno page: HTTP {r.status_code} | {len(r.text):,} chars")
        if r.status_code == 200:
            html = r.text
            all_uuids = list(set(re.findall(
                r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', html, re.I)))
            print(f"    S5b UUIDs in caderno HTML: {all_uuids[:8]}")
            # Also look for any /editions/ paths or PDF links
            edition_paths = re.findall(r'/editions/([0-9a-f-]{36})', html, re.I)
            if edition_paths: print(f"    S5b /editions/ paths: {edition_paths[:4]}")
            # Try to use __NEXT_DATA__ from this specific page
            m = re.search(r'__NEXT_DATA__[^>]*>(.*?)</script>', html, re.DOTALL)
            if m:
                try:
                    page_data = json.loads(m.group(1))
                    u = _best_uuid(page_data, jn, rsn)
                    if u: print(f"    ✅ S5b __NEXT_DATA__: {u}"); return u
                except: pass
            if len(all_uuids) == 1: return all_uuids[0]
            if edition_paths: return edition_paths[0]
    except Exception as e:
        print(f"    S5b error: {e}")
    return None

def _strat6_html_scan(session, html, jn, rsn):
    """Last resort: scan the HTML for UUIDs, /editions/ paths, and PDF hrefs."""
    all_uuids = list(set(re.findall(
        r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', html, re.I)))
    edition_paths = re.findall(r'/editions/([0-9a-f-]{36})', html, re.I)
    pdf_hrefs = re.findall(r'href=["\']([^"\']*edition[^"\']*)["\']', html, re.I)
    print(f"    S6 UUIDs in main HTML: {all_uuids[:6]}")
    if edition_paths: print(f"    S6 /editions/ paths: {edition_paths[:4]}")
    if pdf_hrefs: print(f"    S6 PDF hrefs: {pdf_hrefs[:3]}")
    jn_low = jn.lower().replace(" ",""); rsn_low = rsn.lower().replace(" ","")
    # Return UUID that appears near journalName/rootSectionName context
    for uid in (edition_paths + all_uuids):
        ctx = html[max(0,html.lower().find(uid.lower())-200):
                   html.lower().find(uid.lower())+200].lower()
        if jn_low in ctx or rsn_low in ctx:
            print(f"    ✅ S6 contextual: {uid}"); return uid
    if len(all_uuids) == 1:
        print(f"    ✅ S6 sole UUID: {all_uuids[0]}"); return all_uuids[0]
    if len(edition_paths) == 1:
        print(f"    ✅ S6 edition path: {edition_paths[0]}"); return edition_paths[0]
    return None

def get_uuid_for_caderno(session, caderno):
    jn, rsn, lbl = caderno["journalName"], caderno["rootSectionName"], caderno["label"]
    print(f"\n  UUID {lbl}...")
    html, build_id = _fetch_html_once(session)
    if html is None: print("  HTML indisponivel"); return None
    for n, strat in enumerate([
        lambda: _strat1(session, html, jn, rsn),
        lambda: _strat2(session, build_id, jn, rsn),
        lambda: _strat3(session, html, jn, rsn),
        lambda: _strat4(session, jn, rsn),
        lambda: _strat5(session, jn, rsn),
        lambda: _strat6_html_scan(session, html, jn, rsn),
    ], start=1):
        print(f"    [S{n}]", end=" ")
        try:
            u = strat()
            if u: return u
        except Exception as e: print(f"Erro: {e}")
    # Final fallback: dump first 2000 chars of HTML for manual diagnosis
    print(f"  Falhou {lbl}")
    print(f"  HTML preview: {html[:300].replace(chr(10),' ')}")
    html_uuids = re.findall(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', html, re.I)
    if html_uuids: print(f"  UUIDs anywhere in HTML: {html_uuids[:8]}")
    return None

def baixar_pdf(session, uuid):
    url = f"{PDF_API_BASE}/v1/editions/{uuid}"
    print(f"  Baixando {url}")
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code != 200: print(f"  HTTP {r.status_code}"); return None
        data = b"".join(r.iter_content(65536))
        if data[:4] != b"%PDF": print(f"  Nao e PDF"); return None
        print(f"  OK {len(data)/1e6:.1f} MB"); return data
    except Exception as e: print(f"  {e}"); return None

# ===========================================================================
# TEXT EXTRACTION — 2-col pdfminer, pdfplumber fallback
# ===========================================================================
def extract_text(pdf_bytes, label=""):
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        parts = []
        for page in extract_pages(io.BytesIO(pdf_bytes), laparams=lp):
            mid = page.width * 0.52
            boxes = [(el.bbox[3], (el.bbox[0]+el.bbox[2])/2, el.get_text())
                     for el in page if isinstance(el, LTTextBox) and el.get_text().strip()]
            left  = sorted([(y,t) for y,x,t in boxes if x < mid],  reverse=True)
            right = sorted([(y,t) for y,x,t in boxes if x >= mid], reverse=True)
            parts.append("\n".join(t.strip() for _,t in left+right))
            parts.append("\x0c")
        result = "\n".join(parts)
        print(f"  pdfminer: {len(result):,} chars" + (f" ({label})" if label else ""))
        return result
    except Exception as e:
        print(f"  pdfminer ({e}) -> pdfplumber")
    try:
        import pdfplumber
        parts = []
        with pdfplumber.open(io.BytesIO(pdf_bytes)) as pdf:
            for page in pdf.pages:
                W, H = page.width, page.height
                L = page.within_bbox((0, 0, W*0.5, H)).extract_text() or ""
                R = page.within_bbox((W*0.5, 0, W, H)).extract_text() or ""
                parts.append((L+"\n"+R) if L.strip() and R.strip() else (page.extract_text() or ""))
                parts.append("\x0c")
        result = "\n".join(parts)
        print(f"  pdfplumber: {len(result):,} chars")
        return result
    except Exception as e:
        print(f"  Falhou: {e}"); return ""

# ===========================================================================
# DOESP ATO SEGMENTATION (adapted from DOC-SP v9.3 segment_atos())
#
# DOESP has no TipoDoc|Documento:NNNN IDs like DOC-SP.
# Instead: SECRETARIA headers inherit into acts; act headers are
# PORTARIA/RESOLUCAO/DECRETO/DESPACHO/EXTRATO at line start.
# Validated on real PDFs: ~82 acts/Pessoal, ~21/Normativos, ~32/Gestao.
# ===========================================================================
RE_DOESP_ACT = re.compile(
    r"^((?:PORTARIA|RESOLUCAO|RESOLUÇÃO|DECRETO|DESPACHO|EDITAL|COMUNICADO"
    r"|EXTRATOS?\s+DE\s+CONTRATOS?|CONVÊNIO|CONVENIO|ATA\s+DE)"
    r"\s+[^\n]{2,120})",
    re.M | re.I)

RE_DOESP_SEC = re.compile(
    r"^((?:SECRETARIA\s+(?:DE\s+ESTADO\s+)?(?:D[AEO]\s+)?[A-Z\u00C0-\u00FF]"
    r"|PROCURADORIA\s+GERAL\s+DO\s+ESTADO"
    r"|CASA\s+CIVIL"
    r"|CONTROLADORIA\s+GERAL\s+DO\s+ESTADO"
    r"|MINISTERIO\s+PUBLICO|MINISTÉRIO\s+PÚBLICO"
    r"|DEFENSORIA\s+PUBLICA\s+DO\s+ESTADO|DEFENSORIA\s+PÚBLICA\s+DO\s+ESTADO"
    r"|UNIVERSIDADE\s+(?:DE\s+SAO\s+PAULO|DE\s+SÃO\s+PAULO"
    r"|ESTADUAL\s+DE\s+CAMPINAS|ESTADUAL\s+PAULISTA))"
    r"[^\n]{0,80})$",
    re.M | re.I)

RE_SEI = re.compile(r"\d{3}\.\d{8}/\d{4}[-\u2013]\d{2}")

def segment_doesp_atos(text, caderno_label=""):
    page_breaks = [(m.start(), f"p.{i+2}") for i,m in enumerate(re.finditer(r"\x0c", text))]
    def page_of(pos):
        p = "p.1"
        for pb, pl in page_breaks:
            if pb <= pos: p = pl
            else: break
        return p

    lines = text.split("\n")
    line_starts = []; pos = 0
    for l in lines:
        line_starts.append(pos); pos += len(l) + 1

    atos = []; current_sec = ""; current_tipo = ""; current_start = 0; current_page = "p.1"

    def flush(end_pos):
        nonlocal current_tipo, current_start
        if current_tipo and end_pos > current_start + 30:
            body = text[current_start:end_pos]
            sei = ""
            ms = RE_SEI.search(body)
            if ms: sei = ms.group(0)
            atos.append({
                "tipo": current_tipo, "secretaria": current_sec,
                "sei": sei, "body": body, "page": current_page,
                "hash": f"{hash(current_sec[:20])}_{current_start}",
            })

    for i, line in enumerate(lines):
        s = line.strip()
        lpos = line_starts[i]
        ms = RE_DOESP_SEC.match(s)
        if ms:
            flush(lpos)
            current_sec = re.sub(r"\s+", " ", ms.group(1)).strip()[:70]
            current_tipo = ""; continue
        ma = RE_DOESP_ACT.match(s)
        if ma:
            flush(lpos)
            current_tipo = re.sub(r"\s+", " ", ma.group(1)).strip()[:120]
            current_start = lpos; current_page = page_of(lpos); continue

    flush(len(text))
    print(f"  Segmentacao {caderno_label}: {len(atos)} atos")
    return atos

# ===========================================================================
# FIELD EXTRACTION HELPERS (ported from DOC-SP v9.3)
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

_RE_MONEY = re.compile(r"R\$\s*[\d.,]+(?:\s*\([^)]{0,80}\))?", re.I)
# Strict: DD.DDD.DDD/DDDD-DD — excludes SEI numbers (DDD.DDDDDDDD/YYYY-DD format)
_RE_CNPJ  = re.compile(r"(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)")
_RE_DATE  = re.compile(r"\b\d{2}/\d{2}/\d{4}\b")

# Penalidade patterns (ported from DOC-SP, validated on DOESP real data)
_RE_APLICO_PENA = re.compile(
    r"APLICO\s+a\s+penalidade\s+de\s+"
    r"(SUSPENS[ÃA]O\s+POR\s+[\d]+\s+\([^)]+\)\s+DIAS?|DEMISS[ÃA]O|MULTA|ADVERTÊNCIA|IMPEDIMENTO)",
    re.I)
_RE_APLICO_VAL = re.compile(
    r"(?:APLICO|NOTIFICO).{0,600}?"
    r"(?:pena(?:lidade)?\s+de\s+(?:multa|advertência|suspens[ãa]o|impedimento|demiss[ãa]o)"
    r"|multa\s+no\s+valor\s+de)"
    r"[,.]?\s*(?:no\s+valor\s+de\s+)?(R\$\s*[\d.,]+(?:\s*\([^)]{0,80}\))?)",
    re.I | re.DOTALL)
# Validated on "ao ex-servidor MANOEL SIMÃO REZENDE DA SILVA, RG. N.º 35.140.683-9"
_RE_SERVIDOR = re.compile(
    r"(?:ao\s+)?(?:ex-)?(?:servidor[ae]?|funcion[áa]ri[oa])\s+"
    r"([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF\s]{5,60}?)"
    r"(?:,?\s*R\.?G\.?\s*[Nn]\.?[º°]?\s*)([\d.xX\-]{5,25})",
    re.I | re.U)

# Company (3-stage: labeled → OSC → CAPS) — ported from DOC-SP v9.3
_RE_EMP_LABELED = re.compile(
    r"(?:CONTRATAD[AO]\s*:\s*|Contratad[ao]\s*:\s*|\bempresa\s+"
    r"|\bà\s+empresa\s+|\bda\s+empresa\s+|\bpela\s+empresa\s+"
    r"|\bvencedora\s+(?:do\s+certame\s+)?(?:a\s+)?(?:empresa\s+)?"
    r"|\bem\s+favor\s+d[ae]\s+(?:empresa\s+)?)"
    r"([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF0-9\s&,./()-]{4,100}?"
    r"\s+(?:LTDA|S/?A|S\.A\.?|EIRELI|EPP|ME)\b)\.?",
    re.I | re.U)
_RE_EMP_OSC = re.compile(
    r"\b(ASSOCIA[ÇC][ÃA]O|FUNDA[ÇC][ÃA]O|INSTITUTO|COOPERATIVA|CONS[ÓO]RCIO|HOSPITAL"
    r"|SINDICATO|CENTRO|REAL\s+E\s+BENEMÉRITA)"
    r"(?:\s+[A-Z\u00C0-\u00FF0-9][\wÁÉÍÓÚáéíóú&.-]{1,40}){1,8}",
    re.U)
_RE_EMP_CAPS = re.compile(
    r"(\b[A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF0-9&./()-]+"
    r"(?:\s+[A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF0-9&./()-]+){1,8})"
    r"\s+(LTDA|S/?A|S\.A\.?|EIRELI|EPP|ME)\b\.?",
    re.U)
_CAPS_NOISE = {
    "EXTRATO","OBJETO","PROCESSO","SECRETARIA","PREFEITURA","DIRETORIA",
    "CONTRATANTE","CONTRATADA","PORTARIA","DECRETO","RESOLUÇÃO","DESPACHO",
    "EDITAL","COMUNICADO","PREGÃO","AQUISIÇÃO","HABILITAÇÃO","FORNECIMENTO",
    "ELABORAÇÃO","CONSTRUÇÃO","REFORMA","OS","DE","DA","DO","PARA","COM","CONFORME",
}
_RE_LEADING = re.compile(r"^(?:DA|DO|DE|DOS|DAS|EM|COM|NA|NO|[ÀA]O?|PELA|PELO)\s+", re.I)

def _clean_company(name):
    if not name: return None
    name = name.strip().rstrip(".,;:").lstrip(",. \t")
    for _ in range(3):
        new = _RE_LEADING.sub("", name).strip()
        if new == name: break
        name = new
    if not name or not re.match(r"^[A-ZÁÉÍÓÚa-záéíóú]", name): return None
    first = re.split(r"[\s\-]", name)[0].rstrip(".,:-").upper()
    if first in _CAPS_NOISE or len(name) < 8: return None
    return name

def _get_empresa(text):
    for m in _RE_EMP_LABELED.finditer(text):
        n = _clean_company(m.group(1))
        if n: return n
    for m in _RE_EMP_OSC.finditer(text):
        n = _clean_company(m.group(0))
        if n and len(n.split()) >= 2: return n
    for m in _RE_EMP_CAPS.finditer(text):
        n = _clean_company((m.group(1)+" "+m.group(2)).strip())
        if n: return n
    return None

def _fmt_cnpj(raw):
    d = re.sub(r"\D", "", raw)
    return f"{d[:2]}.{d[2:5]}.{d[5:8]}/{d[8:12]}-{d[12:]}" if len(d)==14 else raw

def _first(pat, text):
    m = pat.search(text); return m.group(0).strip() if m else None

def _get_sec(text, sec_from_ato=""):
    if sec_from_ato: return sec_from_ato[:70]
    m = re.search(
        r"SECRETARIA\s+(?:DE\s+ESTADO\s+)?(?:D[AEO]\s+)"
        r"([A-Z\u00C0-\u00FFa-z\u00E0-\u00FF,\s]{5,60}?)(?=\s*\n|\s{3,})",
        text, re.I)
    if m: return f"Sec. {re.sub(r'\s+', ' ', m.group(1)).strip().rstrip(',.')[:50]}"
    m2 = re.search(r"\b(SSP|SES|SEE|SEDS|SIMA|SABESP|CETESB|CDHU|DER|SAP|SEDUC)\b", text)
    if m2: return m2.group(1)
    return None

def clean_body(body):
    lines = body.split("\n"); out = []
    for line in lines:
        s = line.strip()
        if re.match(r"^─{5,}$", s): continue
        if re.match(r"^\d+\s*[-–]\s*(?:Diário Oficial|São\s+Paulo)", s, re.I) and len(s)<80: continue
        if re.match(r"^(?:Este documento|Documento assinado digitalmente)", s, re.I): continue
        out.append(line)
    return "\n".join(out)

# ===========================================================================
# TYPE-AWARE FIELD DISPATCHER (adapted from DOC-SP v9.3)
# ===========================================================================
def extract_fields_from_ato(ato, caderno_label=""):
    tipo = ato.get("tipo",""); sec = ato.get("secretaria","")
    raw  = clean_body(ato["body"])
    body = re.sub(r"\s+", " ", raw)
    f = {}

    sec_label = _get_sec(body, sec)
    if sec_label: f["🏛️ Secretaria"] = sec_label
    if ato.get("sei"): f["🔖 SEI"] = ato["sei"]
    elif ms := RE_SEI.search(body): f["🔖 SEI"] = ms.group(0)

    tipo_up = tipo.upper()

    # APLICA penalidade — validated pattern on DOESP Pessoal
    if re.search(r"\bAPLICO\b|\bNOTIFICO\b", body, re.I):
        mp = _RE_APLICO_PENA.search(body)
        if mp: f["📋 Pena"] = mp.group(1)
        ms2 = _RE_SERVIDOR.search(body)
        if ms2: f["👤 Servidor"] = ms2.group(1).strip(); f["🪪 RG"] = ms2.group(2).strip()
        mv = _RE_APLICO_VAL.search(body)
        if mv: f["💰 Valor"] = mv.group(1)
        elif mv2 := _RE_MONEY.search(body): f["💰 Valor"] = mv2.group(0)
        if mc := _RE_CNPJ.search(body): f["📄 CNPJ"] = _fmt_cnpj(mc.group(0))
        if v := _get_empresa(body): f["🏢 Empresa"] = v
        mm = re.search(r"por\s+violação\s+ao[s]?\s+(?:artigo|dispositivo)[^.]{5,150}", body, re.I)
        if mm: f["⚠️ Motivo"] = mm.group(0)[:150]
        return {k: v for k, v in f.items() if v}

    # Extrato de contrato — Gestão/Normativos
    if re.search(r"EXTRATO\s+DE\s+CONTRATO|EXTRATOS\s+DE\s+CONTRATOS", tipo_up):
        mc = re.search(r"Contrato(?:\s+n[º°.]*)?\s*:\s*([\w/\-\.]+)", body, re.I)
        if mc: f["📄 Contrato"] = mc.group(1)
        if v := _get_empresa(body): f["🏢 Empresa"] = v
        if mc2 := _RE_CNPJ.search(body): f["📄 CNPJ"] = _fmt_cnpj(mc2.group(0))
        mv = re.search(r"Valor\s*(?:Total|Global)?\s*:\s*(R\$\s*[\d.,]+)", body, re.I)
        if mv: f["💰 Valor"] = mv.group(1)
        elif mv2 := _RE_MONEY.search(body): f["💰 Valor"] = mv2.group(0)
        mp2 = re.search(r"Prazo\s*:\s*([^\n,;.]{3,60})", body, re.I)
        if mp2: f["⏱️ Prazo"] = mp2.group(1).strip()
        mo = re.search(r"Objeto:\s+(.{10,300}?)(?:\.\s+[A-Z]|\n|$)", body, re.I)
        if mo: f["📦 Objeto"] = re.sub(r"\s+"," ",mo.group(1)).strip()[:200]
        mc3 = re.search(r"Contratante\s*:\s+(.{5,80}?)(?:\.|Objeto|Processo|$)", body, re.I)
        if mc3: f["🏛️ Secretaria"] = mc3.group(1).strip()[:70]
        mp3 = re.search(r"Processo\s*:\s*([\d./-]+)", body, re.I)
        if mp3 and not f.get("🔖 SEI"): f["🔖 SEI"] = mp3.group(1)
        return {k: v for k, v in f.items() if v}

    # Portaria / Resolução / Decreto
    if re.search(r"^(PORTARIA|RESOLUCAO|RESOLUÇÃO|DECRETO)\b", tipo_up):
        ma = re.search(
            r"(NOMEIA|NOMEAR|EXONERA|EXONERAR|DEMITE|DEMITIR|APOSENTA|CESSA\s+OS\s+EFEITOS)"
            r"[^,]{0,60}?"
            r"([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF\s]{5,50}?)"
            r",?\s+R\.?G\.?\s*(?:N\.?[º°]?\s*)?([\d.xX\-]{5,20})",
            body, re.I | re.U)
        if ma:
            f["📋 Ação"] = ma.group(1).upper()
            f["👤 Servidor"] = ma.group(2).strip()
            f["🪪 RG"] = ma.group(3).strip()
        mc = re.search(r"cargo\s+(?:em\s+comissão\s+)?(?:de\s+)?([A-Z][^,\n.]{5,80})", body, re.I)
        if mc: f["💼 Cargo"] = mc.group(1).strip()[:80]
        mn = re.search(r"(?:PORTARIA|RESOLUÇÃO|DECRETO)\s+N[º°.]?\s*(\d+(?:[/\-][A-Z\d]+)?)", tipo_up)
        if mn: f["📜 Número"] = mn.group(1)
        # Also captures contracts in Normativos portarias
        if v := _get_empresa(body): f.setdefault("🏢 Empresa", v)
        if mc2 := _RE_CNPJ.search(body): f.setdefault("📄 CNPJ", _fmt_cnpj(mc2.group(0)))
        if mv := _RE_MONEY.search(body): f.setdefault("💰 Valor", mv.group(0))
        mo = re.search(r"Objeto:\s+(.{10,200}?)(?:\.|;|$)", body, re.I)
        if mo: f.setdefault("📦 Objeto", re.sub(r"\s+"," ",mo.group(1)).strip()[:200])
        return {k: v for k, v in f.items() if v}

    # Despacho — disciplinar or administrative
    if re.search(r"^DESPACHO\b", tipo_up):
        mpd = re.search(
            r"(?:Processo\s+Administrativo\s+Disciplinar|Sindicância|PAD)\s*"
            r"[-–]?\s*(?:SAP\s+)?N\.?[º°]?\s*([\d/\-]+)",
            body, re.I)
        if mpd: f.setdefault("🔖 SEI", mpd.group(1)[:40])
        ms3 = _RE_SERVIDOR.search(body)
        if ms3: f["👤 Servidor"] = ms3.group(1).strip(); f["🪪 RG"] = ms3.group(2).strip()
        mas = re.search(r"Assunto\s*:\s*([^\n.]{5,150})", body, re.I)
        if mas: f["📋 Assunto"] = mas.group(1).strip()[:150]
        mi = re.search(r"Interessad[oa]\s*:\s*([^\n.]{5,100})", body, re.I)
        if mi: f["👤 Interessado"] = mi.group(1).strip()
        if mc := _RE_CNPJ.search(body): f["📄 CNPJ"] = _fmt_cnpj(mc.group(0))
        if mv := _RE_MONEY.search(body): f["💰 Valor"] = mv.group(0)
        return {k: v for k, v in f.items() if v}

    # Convênio / Chamamento (Gestão)
    if re.search(r"CONV[ÊE]NIO|CHAMAMENTO|PARCERIA", tipo_up):
        if v := _get_empresa(body): f["🏢 Empresa"] = v
        if mc := _RE_CNPJ.search(body): f["📄 CNPJ"] = _fmt_cnpj(mc.group(0))
        if mv := _RE_MONEY.search(body): f["💰 Valor"] = mv.group(0)
        mp4 = re.search(r"Prazo\s*:\s*([^\n,;.]{3,50})", body, re.I)
        if mp4: f["⏱️ Prazo"] = mp4.group(1).strip()
        mo2 = re.search(r"Objeto:\s+(.{10,200}?)(?:\.|;|$)", body, re.I)
        if mo2: f["📦 Objeto"] = re.sub(r"\s+"," ",mo2.group(1)).strip()[:200]
        return {k: v for k, v in f.items() if v}

    # Fallback
    if mv := _RE_MONEY.search(body): f["💰 Valor"] = mv.group(0)
    if mc := _RE_CNPJ.search(body): f["📄 CNPJ"] = _fmt_cnpj(mc.group(0))
    if v := _get_empresa(body): f["🏢 Empresa"] = v
    mo3 = re.search(r"Objeto:\s+(.{10,200}?)(?:\.|;|$)", body, re.I)
    if mo3: f["📦 Objeto"] = re.sub(r"\s+"," ",mo3.group(1)).strip()[:200]
    if m := _RE_DATE.search(body): f["📅 Data"] = m.group(0)
    return {k: v for k, v in f.items() if v}

# Municípios specific
_RE_COMPACT = re.compile(
    r"Contratad[ao]:\s+(.+?)\s+–\s+(?:Objetivo|Objeto):\s+(.+?)"
    r"(?:\s+–\s+Prazo:\s+([^–\n]{3,40}))?"
    r"(?:\s+–\s+Valor:\s+(R\$\s*[\d.,]+))?"
    r"(?:\s+–\s+Data:\s+([\d/]{5,10}))?",
    re.I | re.DOTALL)
_RE_MUNIC = re.compile(
    r"(?:^|\n)([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF\s\-\']{1,35}?)\n"
    r"(?=PREFEITURA MUNICIPAL|CÂMARA MUNICIPAL|SERVIÇO AUTÔNOMO|CONSÓRCIO)",
    re.UNICODE | re.MULTILINE)

def extract_fields_municipios(window):
    f = {}
    m = _RE_MUNIC.search(window)
    if m:
        n = m.group(1).strip()
        if len(n) >= 3 and n.isupper(): f["📍 Município"] = n
    cm = _RE_COMPACT.search(window)
    if cm:
        f["🏢 Empresa"] = re.sub(r"\s+"," ",cm.group(1)).strip()[:100] if cm.group(1) else None
        f["📦 Objeto"]  = re.sub(r"\s+"," ",cm.group(2)).strip()[:200] if cm.group(2) else None
        f["⏱️ Prazo"]  = re.sub(r"\s+"," ",cm.group(3)).strip()        if cm.group(3) else None
        f["💰 Valor"]  = cm.group(4)                                     if cm.group(4) else None
        f["📅 Data"]   = cm.group(5)                                     if cm.group(5) else None
    if mc := _RE_CNPJ.search(window): f["📄 CNPJ"] = _fmt_cnpj(mc.group(0))
    if not f.get("💰 Valor"):
        if mv := _RE_MONEY.search(window): f["💰 Valor"] = mv.group(0)
    if not f.get("🏢 Empresa"):
        if v := _get_empresa(window): f["🏢 Empresa"] = v
    mp = re.search(r"Processo\s*(?:SEI)?\s*[snº°.]*\s*[\d./-]{6,}", window, re.I)
    if mp: f["🔖 Processo"] = mp.group(0)[:50]
    return {k: v for k, v in f.items() if v}

# ===========================================================================
# TV SCORING (adapted from DOC-SP v9.3)
# ===========================================================================
def tv_score(body_low, keyword, fields, caderno_label=""):
    score = 0; fatores = []
    cat = KEYWORD_CATEGORIES.get(keyword, "general")
    tier = CATEGORY_TV.get(cat, (3,"",""))[0]
    score += {1:4, 2:2, 3:0}.get(tier, 0)

    val = fields.get("💰 Valor","")
    if val:
        amt = parse_brl(val)
        if   amt >= 50_000_000: score += 5; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt >= 10_000_000: score += 4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt >= 1_000_000:  score += 3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt >= 100_000:    score += 1; fatores.append(f"R${amt/1e3:.0f}k")

    if any(t in body_low for t in ["emergencial","urgente","urgência"]):
        score += 3; fatores.append("EMERGENCIAL")
    # "irregularidade" removed — too common in PAD/sindicância contexts
    if any(t in body_low for t in ["superfaturamento","sobrepreço","fraude em licitação","improbidade administrativa"]):
        score += 4; fatores.append("SUSPEITO")
    if any(t in body_low for t in ["hospital das clínicas","upa ","leito de uti","organização social de saúde",
                                    "pronto-socorro","serviço de saúde"]):
        score += 2; fatores.append("SAÚDE")
    if any(t in body_low for t in ["escola estadual","merenda","alimentação escolar"]):
        score += 2; fatores.append("EDUCAÇÃO")
    if any(t in body_low for t in ["unidade prisional","policial penal","penitenciária","complexo penal"]):
        score += 1; fatores.append("PENITENCIÁRIO")
    if any(t in body_low for t in ["demissão","suspensão por","aposentadoria compulsória"]):
        score += 2; fatores.append("SANÇÃO FUNCIONAL")
    if any(t in body_low for t in ["processo administrativo disciplinar","sindicância"]):
        score += 1; fatores.append("DISCIPLINAR")
    if any(t in body_low for t in ["concessão rodoviária","sabesp","metrô","cptm"]):
        score += 1; fatores.append("INFRAESTRUTURA")
    if any(t in body_low for t in ["cetesb","contaminada","embargo","área de risco"]):
        score += 1; fatores.append("MEIO AMBIENTE")
    if caderno_label == "Municípios": score -= 1
    if fields.get("🏢 Empresa") or fields.get("👤 Servidor"):
        score += 1; fatores.append("IDENTIFICADO")
    if fields.get("📄 CNPJ"): score += 1; fatores.append("CNPJ")
    if fields.get("⏱️ Prazo") or fields.get("📅 Data"):
        score += 1; fatores.append("DATA")

    if   score >= 8: return "🟢 APROVADA",    score, fatores
    elif score >= 5: return "🟡 PODE RENDER", score, fatores
    else:            return "🔴 ARQUIVAR",     score, fatores

# ===========================================================================
# FILTER (ported from DOC-SP v9.3)
# ===========================================================================
def passes_filter(kw, body_low, fields):
    rules = KEYWORD_FILTERS.get(kw, {})
    req = rules.get("require_any", [])
    if req and not any(normalize(p) in body_low for p in req): return False
    for ph in rules.get("skip_if", []):
        if normalize(ph) in body_low: return False
    mv = rules.get("min_value")
    if mv:
        val = fields.get("💰 Valor","")
        if val and 0 < parse_brl(val) < mv: return False
    return True

# ===========================================================================
# SCAN — atos-first for Executivo, window-based for Municípios
# (adapted from DOC-SP v9.3 scan_atos())
# ===========================================================================
def scan_atos(atos, caderno_label=""):
    results = []; seen = set()
    kw_cnt = {kw:0 for kw in KEYWORDS}; cnpj_cnt = {}

    for ato in atos:
        body_low = normalize(ato["body"])
        fields = extract_fields_from_ato(ato, caderno_label)

        for kw in KEYWORDS:
            if normalize(kw) not in body_low: continue
            cat = KEYWORD_CATEGORIES.get(kw, "general")
            dedup = (ato["hash"], cat)
            if dedup in seen: continue
            rules = KEYWORD_FILTERS.get(kw, {})
            if kw_cnt[kw] >= rules.get("max_hits", 999): continue
            mc_limit = rules.get("max_hits_per_cnpj", 999)
            if mc_limit < 999:
                cnpj = re.sub(r"\D","", fields.get("📄 CNPJ",""))
                if cnpj:
                    ck = (kw, cnpj)
                    if cnpj_cnt.get(ck, 0) >= mc_limit: continue
                    cnpj_cnt[ck] = cnpj_cnt.get(ck, 0) + 1
            if not passes_filter(kw, body_low, fields): continue
            veredito, score, fatores = tv_score(body_low, kw, fields, caderno_label)
            results.append({
                "keyword": kw, "category": cat, "page": ato["page"],
                "fields": fields, "body": ato["body"][:600],
                "caderno": caderno_label, "tv_veredito": veredito,
                "tv_score": score, "tv_fatores": fatores,
                "secretaria": ato.get("secretaria",""), "tipo": ato.get("tipo",""),
            })
            seen.add(dedup); kw_cnt[kw] += 1

    for kw in KEYWORDS:
        if kw_cnt[kw]: print(f"    [{caderno_label}] '{kw}': {kw_cnt[kw]}")
    return results

def scan_municipios(full_text, caderno_label="Municípios"):
    fn = normalize(full_text)
    bounds = set()
    for p in [r"\x0c", r"[─━═\-]{5,}",
              r"\nSECRETARIA\s+",
              r"\n[A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF\s]{2,35}\n(?=PREFEITURA|CÂMARA)"]:
        for m in re.finditer(p, full_text): bounds.add(m.start())
    bounds = sorted(bounds)
    pbs = [(m.start(), f"p.{i+1}") for i,m in enumerate(re.finditer(r"\x0c", full_text))]
    def pag(pos):
        p="p.1"
        for pb,pl in pbs:
            if pb<=pos: p=pl
            else: break
        return p
    results = []; kw_cnt = {kw:0 for kw in KEYWORDS}
    for kw in KEYWORDS:
        kn = normalize(kw); cat = KEYWORD_CATEGORIES.get(kw,"general")
        rules = KEYWORD_FILTERS.get(kw,{}); mh = rules.get("max_hits",999)
        sp = 0; ace = 0
        while True:
            pos = fn.find(kn, sp)
            if pos == -1: break
            lc = [b for b in bounds if b < pos]
            rc = [b for b in bounds if b > pos+len(kw)]
            l = max(max(lc, default=pos-WINDOW_SIDE), pos-WINDOW_SIDE)
            r = min(min(rc, default=pos+len(kw)+WINDOW_SIDE), pos+len(kw)+WINDOW_SIDE)
            w = re.sub(r"\s+"," ", full_text[l:r]).strip()
            fields = extract_fields_municipios(w)
            if not passes_filter(kw, normalize(w), fields):
                sp = pos+max(len(kn),600); continue
            veredito, score, fatores = tv_score(normalize(w), kw, fields, caderno_label)
            results.append({
                "keyword":kw,"category":cat,"page":pag(pos),
                "fields":fields,"body":w[:600],"caderno":caderno_label,
                "tv_veredito":veredito,"tv_score":score,"tv_fatores":fatores,
                "secretaria":"","tipo":"Municipal",
            })
            ace += 1; sp = pos+max(len(kn),600)
            if ace >= mh: break
        if ace:
            kw_cnt[kw]=ace
            print(f"    [{caderno_label}] '{kw}': {ace}")
    return results

# ===========================================================================
# FICHA BUILDER (adapted from DOC-SP v9.3)
# ===========================================================================
def build_ficha(hit, date_str):
    kw = hit["keyword"]; cat = KEYWORD_CATEGORIES.get(kw,"general")
    tier, icon, cat_nome = CATEGORY_TV.get(cat,(3,"🔍",kw))
    f = hit["fields"]; cad = hit["caderno"]
    cad_emo = next((c["emoji"] for c in CADERNOS if c["label"]==cad),"📋")
    fator_str = " · ".join(hit["tv_fatores"]) if hit["tv_fatores"] else "—"

    lines = [
        f"{SOURCE_EMOJI} *DOESP {date_str}* | {hit['page']} | {cad_emo}{cad}",
        f"{hit['tv_veredito']} | {icon} *{cat_nome}*",
        f"🔑 `{kw}` | Score {hit['tv_score']} | {fator_str}",
    ]
    if hit.get("tipo"):       lines.append(f"📑 {hit['tipo'][:80]}")
    if hit.get("secretaria"): lines.append(f"🏛️ {hit['secretaria'][:70]}")
    lines.append("─"*22)
    if f.get("📍 Município"):  lines.append(f"🏙️ *Município:* {f['📍 Município']}")
    if f.get("📦 Objeto"):     lines.append(f"📌 *Objeto:* {f['📦 Objeto'][:200]}")
    if f.get("📋 Assunto"):    lines.append(f"📌 *Assunto:* {f['📋 Assunto'][:150]}")
    if f.get("🏢 Empresa"):    lines.append(f"🏢 *Empresa:* {f['🏢 Empresa'][:100]}")
    if f.get("📄 CNPJ"):       lines.append(f"   CNPJ: {f['📄 CNPJ']}")
    if f.get("👤 Servidor"):   lines.append(f"👤 *Servidor:* {f['👤 Servidor']}")
    if f.get("🪪 RG"):         lines.append(f"   RG: {f['🪪 RG']}")
    if f.get("📋 Pena"):       lines.append(f"⚖️ *Pena:* {f['📋 Pena']}")
    if f.get("💼 Cargo"):      lines.append(f"   Cargo: {f['💼 Cargo'][:60]}")
    if f.get("👤 Interessado"): lines.append(f"👤 *Interessado:* {f['👤 Interessado'][:80]}")
    if f.get("🏛️ Secretaria") and not hit.get("secretaria"):
        lines.append(f"🏛️ *Secretaria:* {f['🏛️ Secretaria'][:70]}")
    lines.append("─"*22)
    if f.get("💰 Valor"):    lines.append(f"💰 {f['💰 Valor']}")
    if f.get("⏱️ Prazo"):   lines.append(f"⏱️ {f['⏱️ Prazo']}")
    if f.get("📄 Contrato"): lines.append(f"📄 Contrato: {f['📄 Contrato']}")
    if f.get("🔖 SEI"):      lines.append(f"🔖 SEI: {f['🔖 SEI']}")
    if f.get("🔖 Processo"): lines.append(f"🔖 Processo: {f['🔖 Processo']}")
    if f.get("📅 Data"):     lines.append(f"📅 {f['📅 Data']}")
    if f.get("⚠️ Motivo"):   lines.append(f"⚠️ {f['⚠️ Motivo'][:100]}")
    if f.get("📜 Número"):   lines.append(f"📜 {f['📜 Número']}")
    # Lacunas (ported from DOC-SP v9.3)
    lacunas = []
    if not f.get("🏢 Empresa") and not f.get("👤 Servidor"):
        lacunas.append("Empresa/servidor responsável")
    if not f.get("📄 CNPJ") and cat in ("contrato","licitacao","saude","urgencia"):
        lacunas.append("CNPJ (checar TCE-SP)")
    if not f.get("💰 Valor") and cat not in ("pessoal","disciplinar"):
        lacunas.append("Valor do contrato/penalidade")
    if not f.get("🔖 SEI") and not f.get("🔖 Processo"):
        lacunas.append("Número SEI (acesso ao processo)")
    if lacunas:
        lines.append("─"*22)
        lines.append("❓ *Faltando:* " + " · ".join(lacunas))
    lines.append(f"🔗 [Portal DOESP]({PORTAL_URL})")
    return "\n".join(lines)

def build_digesto(arquivadas, date_str):
    if not arquivadas: return None
    by_cat = {}
    for h in arquivadas: by_cat.setdefault(h["category"],[]).append(h)
    lines = [
        f"🗂️ *Background DOESP — {date_str}*",
        f"_{len(arquivadas)} ato(s) sem potencial imediato_",
        "─"*22,
    ]
    for cat, hits in by_cat.items():
        _, icon, _ = CATEGORY_TV.get(cat,(3,"🔍",""))
        for h in hits[:2]:
            val = h["fields"].get("💰 Valor","")
            emp = h["fields"].get("🏢 Empresa","") or h["fields"].get("👤 Servidor","")
            l = f"{icon} *{h['keyword']}* [{h['caderno']}] {h['page']}"
            if val: l += f" | {val[:25]}"
            if emp: l += f" | {emp[:35]}"
            lines.append(l)
    return "\n".join(lines)

def build_summary(results_by_caderno, date_str):
    total = sum(len(h) for h in results_by_caderno.values())
    all_hits = [h for hits in results_by_caderno.values() for h in hits]
    ap = sum(1 for h in all_hits if "APROVADA"    in h["tv_veredito"])
    pr = sum(1 for h in all_hits if "PODE RENDER" in h["tv_veredito"])
    lines = [
        f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
        f"📋 *{total} resultado(s)*\n",
        f"🟢 *Aprovadas:* {ap}",
        f"🟡 *Pode render:* {pr}",
        f"🔴 *Background:* {total-ap-pr}\n",
    ]
    for cad in CADERNOS:
        lbl, emo = cad["label"], cad["emoji"]
        hits = results_by_caderno.get(lbl,[])
        a2 = sum(1 for h in hits if "APROVADA"    in h["tv_veredito"])
        p2 = sum(1 for h in hits if "PODE RENDER" in h["tv_veredito"])
        lines.append(f"{emo} *{lbl}*: {len(hits)} | 🟢{a2} 🟡{p2}" if hits
                     else f"{emo} *{lbl}*: nenhum")
    lines.append("━"*20)
    top = sorted(all_hits, key=lambda x: -x["tv_score"])[:12]
    for h in top:
        _, icon, _ = CATEGORY_TV.get(h["category"],(3,"🔍",""))
        val = h["fields"].get("💰 Valor","")
        emp = h["fields"].get("🏢 Empresa","") or h["fields"].get("👤 Servidor","")
        l = f"{h['tv_veredito'][:2]} {icon} *{h['keyword']}* [{h['caderno']}] {h['page']}"
        if val: l += f" | {val[:25]}"
        if emp: l += f" | {emp[:30]}"
        lines.append(l)
    lines += ["━"*20, f"\n🔗 [Portal DOESP]({PORTAL_URL})",
              "_PDFs completos nas próximas mensagens._"]
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
                json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown",
                      "disable_web_page_preview":True,"disable_notification":silent},
                timeout=15)
            _last_send = time.time()
            if r.status_code == 200: return True
            if r.status_code == 429:
                time.sleep(r.json().get("parameters",{}).get("retry_after",30)+1); continue
            print(f"  TG {r.status_code}"); return False
        except Exception as e: print(f"  TG {e}"); time.sleep(3)
    return False

def send_pdf(pdf_bytes, caption):
    global _last_send
    gap = time.time() - _last_send
    if gap < 2.0: time.sleep(2.0 - gap)
    nome = f"doesp_{datetime.date.today():%Y-%m-%d}.pdf"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
            files={"document":(nome,pdf_bytes,"application/pdf")},
            data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"Markdown"},
            timeout=180)
        _last_send = time.time()
        ok = r.status_code == 200
        print("  PDF ok" if ok else f"  PDF {r.status_code}"); return ok
    except Exception as e: print(f"  PDF {e}"); return False

def split_long(text, max_len=3800):
    if len(text) <= max_len: return [text]
    parts=[]; current=""
    for line in text.split("\n"):
        if len(current)+len(line)+1 > max_len:
            parts.append(current); current=line
        else:
            current+=("\n" if current else "")+line
    if current: parts.append(current)
    return parts

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    hoje = datetime.date.today(); date_str = hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor v3.2 — {date_str} ===\n")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,
                            "text":f"DOESP v3.2 — {date_str} | {len(CADERNOS)} cadernos",
                            "disable_notification":True}, timeout=10)
    except: pass

    session = requests.Session()
    session.headers.update({
        "User-Agent":("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"),
        "Accept":         "text/html,application/xhtml+xml,*/*;q=0.9",
        "Accept-Language":"pt-BR,pt;q=0.9",
        "Origin":         "https://doe.sp.gov.br",
        "Referer":        "https://doe.sp.gov.br/",
    })

    results_by_caderno = {}; pdfs_sent = 0

    for caderno in CADERNOS:
        lbl, emo = caderno["label"], caderno["emoji"]
        print(f"\n{'─'*60}\n{emo}  {caderno['journalName']} / {caderno['rootSectionName']}\n{'─'*60}")

        uuid = get_uuid_for_caderno(session, caderno)
        if not uuid:
            send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}* {emo}[{lbl}]\n"
                         f"UUID não encontrado. Verificar log do Actions.")
            results_by_caderno[lbl]=[]; continue

        print(f"\n  UUID: {uuid}")
        pdf_bytes = baixar_pdf(session, uuid)
        if not pdf_bytes:
            send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}* {emo}[{lbl}]\nErro ao baixar PDF.")
            results_by_caderno[lbl]=[]; continue

        print(f"\n  Extraindo texto...")
        full_text = extract_text(pdf_bytes, label=lbl)
        if not full_text or len(full_text)<500:
            send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}* {emo}[{lbl}]\nExtração falhou.")
            results_by_caderno[lbl]=[]; continue

        print(f"\n  Scanning {lbl}...")
        if lbl == "Municípios":
            hits = scan_municipios(full_text, caderno_label=lbl)
        else:
            atos = segment_doesp_atos(full_text, caderno_label=lbl)
            hits = scan_atos(atos, caderno_label=lbl)

        results_by_caderno[lbl] = hits
        ap = sum(1 for h in hits if "APROVADA"    in h["tv_veredito"])
        pr = sum(1 for h in hits if "PODE RENDER" in h["tv_veredito"])
        print(f"  {len(hits)} hits | 🟢{ap} 🟡{pr} 🔴{len(hits)-ap-pr}")

        if pdfs_sent < 4:
            send_pdf(pdf_bytes, f"{emo} *{SOURCE_NAME} — {date_str}* [{lbl}] {len(pdf_bytes)/1e6:.1f} MB")
            pdfs_sent += 1
        time.sleep(1)

    total = sum(len(h) for h in results_by_caderno.values())
    print(f"\n{'='*60}\nTOTAL: {total}")

    if total == 0:
        send_telegram(f"✅ *{SOURCE_NAME} — {date_str}*\nNenhum resultado em {len(CADERNOS)} cadernos.")
        return

    all_hits = [h for hits in results_by_caderno.values() for h in hits]
    aprovadas   = sorted([h for h in all_hits if "APROVADA"    in h["tv_veredito"]], key=lambda x: -x["tv_score"])
    pode_render = sorted([h for h in all_hits if "PODE RENDER" in h["tv_veredito"]], key=lambda x: -x["tv_score"])
    arquivadas  =         [h for h in all_hits if "ARQUIVAR"    in h["tv_veredito"]]

    print("\nResumo...")
    send_telegram(build_summary(results_by_caderno, date_str))
    time.sleep(1)

    print(f"\nFichas aprovadas ({len(aprovadas)})...")
    for h in aprovadas:
        for part in split_long(build_ficha(h, date_str)): send_telegram(part)
        time.sleep(0.5)

    print(f"\nPode render ({len(pode_render)})...")
    for h in pode_render:
        for part in split_long(build_ficha(h, date_str)): send_telegram(part)
        time.sleep(0.5)

    if arquivadas:
        digesto = build_digesto(arquivadas, date_str)
        if digesto:
            print(f"\nDigesto ({len(arquivadas)})...")
            for part in split_long(digesto): send_telegram(part, silent=True)

if __name__ == "__main__":
    main()
