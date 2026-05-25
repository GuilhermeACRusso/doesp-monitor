"""DOESP Monitor v5.3 — Clean structured extraction, no raw context dumps."""

import requests, datetime, os, sys, re, json, unicodedata, io, time

TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")
if not TELEGRAM_TOKEN or not CHAT_ID:
    print("FATAL: TELEGRAM_TOKEN ou CHAT_ID ausentes."); sys.exit(1)

CADERNOS = [
    {"journalName":"Executivo",  "rootSectionName":"Atos Normativos",    "label":"Normativos","emoji":"📋"},
    {"journalName":"Executivo",  "rootSectionName":"Atos de Pessoal",    "label":"Pessoal",   "emoji":"👤"},
    {"journalName":"Executivo",  "rootSectionName":"Atos de Gestão e Despesas","label":"Gestão","emoji":"💼"},
    {"journalName":"Municípios", "rootSectionName":"Atos Municipais",    "label":"Municípios","emoji":"🏛️"},
]

PORTAL_URL = "https://doe.sp.gov.br/sumario"
PDF_API    = "https://do-api-publication-pdf.doe.sp.gov.br"
UUID_RE    = re.compile(r'[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}', re.I)

# ── KEYWORDS ──────────────────────────────────────────────────────
KEYWORD_CATEGORIES = {
    # ── TIER 1: INVESTIGATIVO / URGÊNCIA ──
    "contratação emergencial":              "urgencia",
    "calamidade pública":                   "urgencia",
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
    "lavagem de dinheiro":                  "investigativo",
    "apuração de irregularidade":           "investigativo",
    "inquérito civil":                      "investigativo",
    # ── TIER 2: PRIVATIZAÇÃO / CONCESSÃO (Tarcísio signature policy) ──
    "privatização":                         "privatizacao",
    "desestatização":                       "privatizacao",
    "concessão de serviço público":         "privatizacao",
    "parceria público-privada":             "privatizacao",
    "reequilíbrio econômico":               "privatizacao",
    "reajuste de tarifa":                   "privatizacao",
    # ── TIER 2: FISCAL ──
    "renúncia fiscal":                      "fiscal",
    "benefício fiscal":                     "fiscal",
    "isenção de ICMS":                      "fiscal",
    "crédito adicional suplementar":        "orcamento",
    # ── TIER 2: LICITAÇÃO / CONTRATO ──
    "dispensa de licitação":                "licitacao",
    "inexigibilidade de licitação":         "licitacao",
    "licitação deserta":                    "licitacao",
    "concorrência eletrônica":              "licitacao",
    "pregão eletrônico":                    "licitacao",
    "extrato de contrato":                  "contrato",
    "rescisão de contrato":                 "contrato",
    "termo de aditamento":                  "contrato",
    "rescindido o contrato":                "contrato",
    # ── TIER 2: OBRAS / INFRAESTRUTURA ──
    "obra paralisada":                      "obras",
    "habitação de interesse social":        "obras",
    "unidades habitacionais":               "obras",
    "programa habitacional":                "obras",
    "saneamento básico":                    "obras",
    "pavimentação":                         "obras",
    "recapeamento asfáltico":               "obras",
    "canalização":                          "obras",
    "concessão rodoviária":                 "obras",
    "desapropriação":                       "obras",
    "trem intercidades":                    "obras",
    "pedágio":                              "obras",
    "túnel Santos":                         "obras",
    # ── TIER 2: DISCIPLINAR / PENALIDADE ──
    "demissão de servidor":                 "disciplinar",
    "aposentadoria compulsória":            "disciplinar",
    "processo administrativo disciplinar":  "disciplinar",
    "sindicância":                          "disciplinar",
    "aplicação de penalidade":              "penalidade",
    "multa contratual":                     "penalidade",
    # ── TIER 2: SEGURANÇA ──
    "operação policial":                    "seguranca",
    "unidade prisional":                    "seguranca",
    "morte em custódia":                    "seguranca",
    "feminicídio":                          "seguranca",
    "delegacia de polícia":                 "seguranca",
    "letalidade policial":                  "seguranca",
    "intervenção policial":                 "seguranca",
    "câmera corporal":                      "seguranca",
    # ── TIER 2: LEGAL ──
    "ação civil pública":                   "legal",
    "decisão judicial":                     "legal",
    # ── TIER 2: EDUCAÇÃO ──
    "merenda escolar":                      "educacao",
    "transporte escolar":                   "educacao",
    "construção de escola estadual":        "educacao",
    "fechamento de escola":                 "educacao",
    "concurso de professor":                "educacao",
    # ── TIER 2: SAÚDE ──
    "dengue":                               "saude",
    "vigilância sanitária":                 "saude",
    # ── TIER 2: MEIO AMBIENTE ──
    "licença ambiental":                    "meio_ambiente",
    "auto de infração ambiental":           "meio_ambiente",
    "CETESB":                               "meio_ambiente",
    "área contaminada":                     "meio_ambiente",
    # ── TIER 2: ENTIDADES ESTADUAIS ──
    "CDHU":                                 "obras",
    "DER":                                  "obras",
    "ARSESP":                               "privatizacao",
    # ── TIER 3: PESSOAL ──
    "nomeação para cargo em comissão":      "pessoal",
    "exoneração a pedido":                  "pessoal",
    "exoneração de servidor":               "pessoal",
    "auditoria":                            "investigativo",
}

KEYWORDS = sorted(KEYWORD_CATEGORIES.keys(), key=len, reverse=True)
CATEGORY_TV = {
    "urgencia":      (1, "🚨", "Emergência"),
    "saude":         (1, "🏥", "Saúde"),
    "investigativo": (1, "🔎", "Investigativo"),
    "privatizacao":  (1, "🏭", "Privatização"),
    "fiscal":        (1, "💸", "Fiscal"),
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
KEYWORD_FILTERS = {
    "extrato de contrato":{"max_hits":20,"require_any":["cnpj","contratad","objeto","valor"]},
    "termo de aditamento":{"max_hits":15,"require_any":["cnpj","contratad","valor","aditamento"]},
    "dispensa de licitação":{"max_hits":15,"require_any":["autorizo","homologo","contratad","valor","dispensa"],"skip_if":["resultou fracassada"]},
    "pregão eletrônico":{"max_hits":15,"require_any":["adjudic","homologo","contratad","valor","vencedora"]},
    "inexigibilidade de licitação":{"min_value":50_000},
    "aplicação de penalidade":{"max_hits":10,"require_any":["aplico","notifico","suspensão","multa","pena"]},
    "sindicância":{"max_hits":10,"require_any":["instaurar","instaurada","conclusão","arquivada","pena","aplico"]},
    "processo administrativo disciplinar":{"max_hits":8,"require_any":["instaurado","instaurada","corregedoria","demissão","suspensão","aplico"]},
    "organização social de saúde":{"require_any":["contrato de gestão","os ","spdm","hospital"]},
    "CETESB":{"require_any":["multa","embargo","auto de infração","licença","autuação"]},
    "DER":{"require_any":["rodovia","concessão","pedágio","obra","licitação"],"skip_if":["der credenciamento"]},
    "dengue":{"require_any":["caso","foco","combate","surto","contrato"],"skip_if":["projeto de lei"]},
    "superfaturamento":{"require_any":["apurou","indício","constatou","investigação","TCE","MP "],"skip_if":["evitar superfaturamento"]},
    "sobrepreço":{"require_any":["apurou","indício","constatou"],"skip_if":["evitar contratações com sobrepreço"]},
    "privatização":{"require_any":["sabesp","metrô","cptm","concessão","leilão","edital","aprova"]},
    "renúncia fiscal":{"require_any":["icms","ipva","empresa","benefício","decreto"]},
    "benefício fiscal":{"require_any":["icms","ipva","empresa","decreto","crédito"]},
    "pedágio":{"require_any":["concessão","tarifa","rodovia","free flow","lote"]},
    "desapropriação":{"require_any":["utilidade pública","decreto","imóvel","área","terreno"]},
    "calamidade pública":{"require_any":["decreto","emergência","defesa civil","situação"]},
    "reajuste de tarifa":{"require_any":["metrô","cptm","ônibus","água","esgoto","energia"]},
    "nomeação para cargo em comissão":{"max_hits":8},
    "exoneração a pedido":{"max_hits":8},
    "exoneração de servidor":{"max_hits":8},
    "demissão de servidor":{"max_hits":5},
    "auditoria":{"require_any":["TCE","tribunal de contas","irregularidade","achados","relatório"]},
    "decisão judicial":{"max_hits":10,"require_any":["determina","condena","anula","suspende","liminar"]},
}

# ── HELPERS ───────────────────────────────────────────────────────
def normalize(t):
    return "".join(c for c in unicodedata.normalize("NFKD",t) if not unicodedata.combining(c)).lower()
def parse_brl(s):
    if not s: return 0.0
    m=re.search(r"[\d.,]+",s)
    if not m: return 0.0
    v=re.sub(r"\.(?=\d{3}(\D|$))","",m.group()).replace(",",".")
    try: return float(v)
    except: return 0.0
def caderno_url(jn, rsn):
    return f"{PORTAL_URL}?journalName={requests.utils.quote(jn)}&rootSectionName={requests.utils.quote(rsn)}"

# ── ABBREVIATIONS for government terms ──────────────────
_ABBREVS = [
    ("SECRETARIA DE ESTADO DA ", "Sec. "),
    ("SECRETARIA DE ESTADO DE ", "Sec. "),
    ("SECRETARIA DE ESTADO DO ", "Sec. "),
    ("SECRETARIA DA ", "Sec. "),
    ("SECRETARIA DE ", "Sec. "),
    ("SECRETARIA DO ", "Sec. "),
    ("COORDENADORIA DE ", "Coord. "),
    ("COORDENADORIA DA ", "Coord. "),
    ("SUBSECRETARIA DE ", "Subsec. "),
    ("SUBSECRETARIA DA ", "Subsec. "),
    ("SUPERINTENDÊNCIA DE ", "Sup. "),
    ("SUPERINTENDÊNCIA DA ", "Sup. "),
    ("DIRETORIA DE ", "Dir. "),
    ("DIRETORIA DA ", "Dir. "),
    ("DEPARTAMENTO DE ", "Depto. "),
    ("DEPARTAMENTO DA ", "Depto. "),
    ("PREFEITURA MUNICIPAL DE ", "Pref. "),
    ("PREFEITURA MUNICIPAL DA ", "Pref. "),
    ("CÂMARA MUNICIPAL DE ", "Câmara "),
    ("GABINETE DO SECRETÁRIO", "Gab. Secretário"),
    ("PROCURADORIA GERAL ", "PGE "),
    ("AGÊNCIA REGULADORA DE SERVIÇOS PÚBLICOS DELEGADOS DE TRANSPORTE DO ESTADO DE SÃO PAULO", "ARTESP"),
    ("AGÊNCIA REGULADORA DE SERVIÇOS PÚBLICOS DO ESTADO DE SÃO PAULO", "ARSESP"),
    ("FACULDADE DE MEDICINA DA UNIVERSIDADE DE SÃO PAULO", "FMUSP"),
    ("FACULDADE DE MEDICINA DE ", "Fac. Med. "),
    ("HOSPITAL DAS CLÍNICAS DA ", "HC "),
    ("UNIVERSIDADE DE SÃO PAULO", "USP"),
    ("UNIVERSIDADE ESTADUAL DE CAMPINAS", "UNICAMP"),
    ("UNIVERSIDADE ESTADUAL PAULISTA", "UNESP"),
    ("ADMINISTRAÇÃO PENITENCIÁRIA", "Adm. Penitenciária"),
    ("SEGURANÇA PÚBLICA", "Seg. Pública"),
    ("MEIO AMBIENTE, INFRAESTRUTURA E LOGÍSTICA", "Meio Amb./Infra"),
    ("DESENVOLVIMENTO ECONÔMICO", "Desenv. Econômico"),
    ("DESENVOLVIMENTO SOCIAL", "Desenv. Social"),
    ("PARCERIAS EM INVESTIMENTOS", "Parcerias/Invest."),
]

def abbreviate(text, max_len=70):
    """Shorten government hierarchy names while keeping them readable."""
    if not text: return ""
    t = text
    for full, short in _ABBREVS:
        # Case-insensitive replace
        idx = t.lower().find(full.lower())
        while idx >= 0:
            t = t[:idx] + short + t[idx+len(full):]
            idx = t.lower().find(full.lower(), idx + len(short))
    # Also handle > separators: trim each segment
    if " > " in t:
        parts = t.split(" > ")
        # Keep first and last parts, abbreviate middle
        if len(parts) > 3:
            parts = [parts[0], "...", parts[-1]]
        t = " > ".join(p.strip() for p in parts)
    if len(t) > max_len:
        t = t[:max_len-1] + "…"
    return t

def extract_city(path, caderno_label):
    """Extract city name from tree path. For Municípios, it's the first child."""
    if not path: return ""
    parts = [p.strip() for p in path.split(" > ")]
    if caderno_label == "Municípios" and len(parts) >= 2:
        # First part is section name, second is city
        city = parts[1] if parts[1] != parts[0] else (parts[2] if len(parts)>2 else "")
        if city and city.isupper() and len(city) > 2:
            return city.title()  # VÁRZEA PAULISTA → Várzea Paulista
    return ""


# ── FIELD EXTRACTION REGEXES (ported from DOC-SP v9.3) ───────────
_RE_MONEY = re.compile(r'R\$\s*[\d.,]+(?:\s*\([^)]{0,80}\))?', re.I)
_RE_CNPJ  = re.compile(r'(?<!\d)\d{2}\.\d{3}\.\d{3}/\d{4}-\d{2}(?!\d)')
_RE_SEI   = re.compile(r'\d{3}\.\d{8}/\d{4}[-\u2013]\d{2}')
_RE_DATE  = re.compile(r'\b\d{2}/\d{2}/\d{4}\b')

# Company: labeled > OSC > CAPS (3-tier, from DOC-SP v9.3)
_RE_EMP_LABELED = re.compile(
    r'(?:\bempresa\s+|\bCONTRATAD[AO]\s*:?\s*|\bContratad[ao]\s*:?\s*'
    r'|\bvencedora\b[^.]{0,30}?(?:empresa\s+)?'
    r'|\bem\s+favor\s+d[ae]\s+(?:empresa\s+)?)'
    r'([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF0-9\s&,./()-]{4,100}?'
    r'\s+(?:LTDA|S/?A|S\.A\.?|EIRELI|EPP|ME)\b)\.?', re.I|re.U)
_RE_EMP_OSC = re.compile(
    r'\b(ASSOCIA[ÇC][ÃA]O|FUNDA[ÇC][ÃA]O|INSTITUTO|COOPERATIVA'
    r'|CONS[ÓO]RCIO|HOSPITAL|SINDICATO|CENTRO)'
    r'(?:\s+[A-Z\u00C0-\u00FF0-9][\w\u00C0-\u00FF&.-]{1,40}){1,8}', re.U)
_RE_EMP_CAPS = re.compile(
    r'(\b[A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF0-9&./()-]+'
    r'(?:\s+[A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FF0-9&./()-]+){1,8})'
    r'\s+(LTDA|S/?A|S\.A\.?|EIRELI|EPP|ME)\b\.?', re.U)
_CAPS_NOISE = {"EXTRATO","OBJETO","PROCESSO","SECRETARIA","PREFEITURA","DIRETORIA",
    "CONTRATANTE","CONTRATADA","PORTARIA","DECRETO","RESOLUÇÃO","DESPACHO",
    "EDITAL","COMUNICADO","PREGÃO","FORNECIMENTO","ELABORAÇÃO","CONSTRUÇÃO"}
_RE_LEAD = re.compile(r'^(?:DA|DO|DE|DOS|DAS|EM|COM|NA|NO|PELA|PELO)\s+', re.I)

def _clean_co(name):
    if not name: return None
    name=name.strip().rstrip('.,;:').lstrip(',. ')
    for _ in range(3):
        new=_RE_LEAD.sub('',name).strip()
        if new==name: break
        name=new
    if not name or len(name)<8: return None
    first=re.split(r'[\s\-]',name)[0].upper()
    if first in _CAPS_NOISE: return None
    return name

def get_empresa(text):
    for m in _RE_EMP_LABELED.finditer(text):
        n=_clean_co(m.group(1))
        if n: return n
    for m in _RE_EMP_OSC.finditer(text):
        n=_clean_co(m.group(0))
        if n and len(n.split())>=2: return n
    for m in _RE_EMP_CAPS.finditer(text):
        n=_clean_co((m.group(1)+" "+m.group(2)).strip())
        if n: return n
    return None

_RE_SERVIDOR = re.compile(
    r'(?:ao\s+)?(?:ex-)?(?:servidor[ae]?|funcion[aá]ri[oa])\s+'
    r'([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF\s]{5,60}?)'
    r'(?:,?\s*R\.?G\.?\s*[Nn]\.?[º°]?\s*)([\d.xX\-]{5,25})', re.I|re.U)

# Inline label parser: "Contratada: X - CNPJ: Y - Valor: Z" format
_RE_INLINE = re.compile(
    r'\b(Contratad[ao]|Contratante|CNPJ(?:/MF)?|Objeto|Valor(?:\s+Total)?'
    r'|Prazo|Vig[êe]ncia|Processo\s*(?:SEI)?|Modalidade'
    r'|Data\s+(?:de\s+)?[Aa]ssinatura)\s*:\s*'
    r'([^\n]{3,250}?)'
    r'(?=\s*[-\u2013]\s*[A-Z\u00C0-\u00FF][a-z\u00E0-\u00FF]{1,20}\s*:|\.\s*$|\n|$)', re.I)

def extract_fields(title, excerpt, path):
    """Extract who/what/when/how-much from title + excerpt. DOESP-specific patterns."""
    f = {}
    text = (title + " " + excerpt).strip()
    if not text: return f

    # ── GOVERNMENT BRANCH (from tree path) ──
    if path:
        short = path.split(" > ", 1)[1] if " > " in path else path
        if short: f["orgao"] = short

    # ── ACT TYPE from title ──
    m = re.match(r"(DECRETO|PORTARIA|RESOLUÇÃO|RESOLUCAO|DESPACHO|EDITAL"
                 r"|COMUNICADO|EXTRATO|APOSTILA|ATA)\b", title, re.I)
    if m: f["tipo_ato"] = m.group(1).upper()

    # ── DATE from title: "DE 18 DE MAIO DE 2026" or "DE 19-05-26" ──
    MESES = {"janeiro":"01","fevereiro":"02","março":"03","marco":"03",
             "abril":"04","maio":"05","junho":"06","julho":"07",
             "agosto":"08","setembro":"09","outubro":"10",
             "novembro":"11","dezembro":"12"}
    md = re.search(r"DE\s+(\d{1,2})\s+DE\s+(\w+)\s+DE\s+(\d{4})", title, re.I)
    if md:
        mes = MESES.get(md.group(2).lower(),"")
        if mes: f["data"] = f"{md.group(1).zfill(2)}/{mes}/{md.group(3)}"
    elif not f.get("data"):
        md2 = re.search(r"DE\s+(\d{1,2})[-/](\d{1,2})[-/](\d{2,4})", title)
        if md2:
            y = md2.group(3)
            if len(y)==2: y = "20"+y
            f["data"] = f"{md2.group(1).zfill(2)}/{md2.group(2).zfill(2)}/{y}"

    # ── PARSE INLINE LABELS from excerpt: "Contratada: X - CNPJ: Y" ──
    inline = {}
    for m in _RE_INLINE.finditer(text):
        lab = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        val = m.group(2).strip().rstrip(".,;-\u2013 ")
        if val and len(val) > 2: inline[lab] = val

    # DOESP-specific labels: "Nº do Processo: X Interessado: Y Assunto: Z"
    # Uses lookahead to stop at the next label
    _DOESP_LABELS = r"N[º°]\s*do\s*Processo|Interessado|Assunto|Fundamento\s+Legal"
    for m in re.finditer(
        r"\b(" + _DOESP_LABELS + r")\s*:\s*(.+?)(?=\s+(?:" + _DOESP_LABELS + r")\s*:|\n|$)",
        text, re.I):
        lab = re.sub(r"\s+", " ", m.group(1)).strip().lower()
        val = m.group(2).strip().rstrip(".,;- ")
        if val and len(val) > 2: inline[lab] = val[:100]

    # ── COMPANY ──
    emp = inline.get("contratada") or inline.get("contratado")
    if emp: f["empresa"] = (_clean_co(emp) or emp)[:80]
    elif v := get_empresa(text): f["empresa"] = v

    # ── CNPJ ──
    ci = inline.get("cnpj") or inline.get("cnpj/mf")
    if ci:
        m = _RE_CNPJ.search(ci)
        f["cnpj"] = m.group(0) if m else ci[:20]
    elif m := _RE_CNPJ.search(text): f["cnpj"] = m.group(0)

    # ── VALUE ──
    vi = inline.get("valor") or inline.get("valor total")
    if vi:
        m = _RE_MONEY.search(vi)
        f["valor"] = m.group(0) if m else vi[:30]
    elif m := _RE_MONEY.search(text): f["valor"] = m.group(0)

    # ── OBJECT ──
    oi = inline.get("objeto")
    if oi: f["objeto"] = oi[:150]
    else:
        m = re.search(r"[Oo]bjeto\s*:\s*(.{10,200}?)(?:\.\s+[A-Z]|\n|$)", text)
        if m: f["objeto"] = m.group(1).strip()[:150]

    # ── PROCESS / SEI ──
    proc = inline.get("nº do processo") or inline.get("processo sei") or inline.get("processo")
    if proc: f["processo"] = proc[:50]
    elif m := _RE_SEI.search(text): f["sei"] = m.group(0)

    # ── PRAZO / VIGÊNCIA ──
    pr = inline.get("prazo") or inline.get("vigência") or inline.get("vigencia")
    if pr: f["prazo"] = pr[:50]

    # ── DATE from excerpt (fallback) ──
    if not f.get("data"):
        di = inline.get("data de assinatura") or inline.get("data assinatura")
        if di: f["data"] = di[:15]
        elif m := _RE_DATE.search(excerpt): f["data"] = m.group(0)

    # ── CONTRACT NUMBER ──
    m = re.search(r"(?:Contrato|Termo|Convênio)\s*(?:n[º°.]*\s*)?" 
                  r"([\w/\-\.]+\d[\w/\-\.]*)", text, re.I)
    if m: f["contrato"] = m.group(0)[:50]

    # ── MODALIDADE ──
    mi = inline.get("modalidade")
    if mi: f["modalidade"] = mi[:50]

    # ── SERVIDOR + RG (personnel/disciplinary acts) ──
    # Pattern: "servidor NOME - RG. n.º XX.XXX.XXX-X"  or "ex-servidor NOME, RG XX"
    ms = re.search(
        r"(?:ao\s+)?(?:ex-)?servidor[ae]?\s+"
        r"([A-Z\u00C0-\u00FF][A-Z\u00C0-\u00FFa-z\u00E0-\u00FF\s]{5,60}?)"
        r"\s*[-–,]\s*RG\.?\s*[Nn]?\.?[º°]?\s*([\d.xX\-]{5,25})",
        text, re.U)
    if ms: f["servidor"] = ms.group(1).strip(); f["rg"] = ms.group(2).strip()

    # ── INTERESSADO (DOESP-specific) ──
    intr = inline.get("interessado")
    if intr and not f.get("servidor"):
        f["interessado"] = intr[:80]

    # ── ASSUNTO (DOESP-specific) ──
    assunto = inline.get("assunto")
    if assunto: f["assunto"] = assunto[:100]

    # ── PENALIDADE (DOESP: APLICA a penalidade de...) ──
    mp = re.search(
        r"APLIC[AO]\s+a\s+penalidade\s+de\s+"
        r"(\d+\s*\([^)]+\)\s*DIAS?\s+DE\s+SUSPENS[ÃA]O|DEMISS[ÃA]O|MULTA|ADVERTÊNCIA)",
        text, re.I)
    if mp: f["penalidade"] = mp.group(1).strip()

    # ── FUNDAMENTO LEGAL ──
    fl = inline.get("fundamento legal")
    if fl:
        # Stop at first verb/action (NOMEIA, RESOLVE, DETERMINA)
        cut = re.search(r"\b(NOMEIA|RESOLVE|DETERMINA|AUTORIZA|DESIGNA|DESPACHO)\b", fl, re.I)
        f["fundamento"] = fl[:cut.start()].strip().rstrip(".,; ") if cut else fl[:80]

    return {k: v for k, v in f.items() if v}


# ── TREE PARSER ───────────────────────────────────────────────────
def extract_publications_from_tree(tree_data):
    pubs = []
    def walk(node, path_parts):
        if isinstance(node, dict):
            name = node.get("name","")
            new_path = path_parts + [name] if name else path_parts
            for pub in node.get("publications",[]):
                pubs.append({"title":pub.get("title",""),"slug":pub.get("slug",""),
                    "id":pub.get("id",""),"path":" > ".join(new_path),
                    "org":new_path[-1] if len(new_path)>=2 else name})
            for key in ("children","items","itens","categories"):
                for child in node.get(key,[]): walk(child, new_path)
        elif isinstance(node, list):
            for item in node: walk(item, path_parts)
    walk(tree_data, [])
    return pubs

# ── PLAYWRIGHT ────────────────────────────────────────────────────
def process_caderno(browser, caderno):
    jn=caderno["journalName"]; rsn=caderno["rootSectionName"]; lbl=caderno["label"]
    tree_data=None; pub_excerpts={}; pdf_uuid=None; all_api=[]

    def on_response(response):
        nonlocal tree_data, pdf_uuid
        try:
            url=response.url; ct=response.headers.get("content-type","")
            if response.status==200 and "json" in ct:
                data=response.json(); all_api.append({"url":url,"data":data})
                if isinstance(data,dict) and "journalName" in data and "items" in data:
                    tree_data=data; print(f"    TREE [{url[-55:]}]")
                if isinstance(data,dict) and "publications" in data and "pages" in data:
                    for p in data["publications"]:
                        if p.get("id") and p.get("excerpt"):
                            pub_excerpts[p["id"]]=p.get("excerpt","")[:500]
                    print(f"    PUBS [{url[-55:]}] {data.get('pages',0)} pages")
                if isinstance(data,dict) and "fileName" in data and "url" in data:
                    pdf_uuid=data["fileName"]; print(f"    PDF UUID: {pdf_uuid}")
        except: pass

    ctx=browser.new_context(
        user_agent="Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        locale="pt-BR")
    page=ctx.new_page(); page.on("response", on_response)

    try:
        print(f"  [{lbl}] Loading...")
        page.goto(PORTAL_URL, wait_until="networkidle", timeout=30000)
        page.wait_for_timeout(3000)

        print(f"  [{lbl}] Click '{jn}'")
        for sel in [f"text='{jn}'",f"button:has-text('{jn}')",f"a:has-text('{jn}')"]:
            loc=page.locator(sel)
            if loc.count()>0: loc.first.click(); break
        page.wait_for_timeout(4000)

        print(f"  [{lbl}] Click '{rsn}'")
        clicked=False
        for sel in [f"text='{rsn}'",f"a:has-text('{rsn}')",f"span:has-text('{rsn}')"]:
            loc=page.locator(sel)
            if loc.count()>0: loc.first.click(); clicked=True; break
        if not clicked:
            words=rsn.split()
            for el in page.query_selector_all("a, button, div, span, li"):
                txt=(el.inner_text() or "").strip()
                if all(w.lower() in txt.lower() for w in words) and len(txt)<80:
                    el.click(); clicked=True; break
        page.wait_for_timeout(6000)

        dom_text=page.inner_text("body")
        print(f"  [{lbl}] DOM={len(dom_text):,}ch tree={'✅' if tree_data else '❌'} pubs={len(pub_excerpts)} pdf={pdf_uuid or '❌'}")
        ctx.close()
        return tree_data, pub_excerpts, pdf_uuid, dom_text
    except Exception as e:
        print(f"  [{lbl}] PW error: {e}"); ctx.close()
        return None, {}, None, ""

# ── PAGE INDEX — map publication titles to PDF page numbers ──
def build_page_index(pdf_bytes, max_pages=500):
    """Extract text per page. Returns [(page_num, normalized_text), ...]."""
    try:
        from pdfminer.high_level import extract_pages
        from pdfminer.layout import LTTextBox, LAParams
        lp = LAParams(line_margin=0.3, char_margin=2.0, word_margin=0.1, boxes_flow=None)
        index = []
        for i, page in enumerate(extract_pages(io.BytesIO(pdf_bytes), laparams=lp)):
            if i >= max_pages: break
            boxes = [el.get_text() for el in page if isinstance(el, LTTextBox) and el.get_text().strip()]
            page_text = " ".join(boxes)
            index.append((i + 1, normalize(page_text)))
        print(f"  Page index: {len(index)} pages")
        return index
    except Exception as e:
        print(f"  Page index err: {e}")
        return []

def find_page(title, page_index):
    """Find which PDF page contains this title. Handles line breaks and formatting differences."""
    if not title or not page_index: return None
    title_n = normalize(title)
    # Strategy 1: first 35 chars
    needle = title_n[:35]
    if len(needle) >= 10:
        for pn, pt in page_index:
            if needle in pt: return pn
    # Strategy 2: first 20 chars (line breaks can split mid-title)
    needle = title_n[:20]
    if len(needle) >= 10:
        for pn, pt in page_index:
            if needle in pt: return pn
    # Strategy 3: key words from title (handles reflow/extra spaces)
    # Extract significant words (>4 chars, not common prepositions)
    skip = {"para","como","sobre","entre","desde","estado","maio","junho",
            "julho","abril","agosto","setembro","outubro","novembro","dezembro",
            "janeiro","fevereiro","marco","2026","2025","2024"}
    words = [w for w in re.findall(r"[a-zà-ÿ]{4,}", title_n) if w not in skip]
    if len(words) >= 3:
        # Require at least 3 key words on the same page
        for pn, pt in page_index:
            matches = sum(1 for w in words[:6] if w in pt)
            if matches >= min(3, len(words)):
                return pn
    return None

def download_pdf_for_pages(session, uuid):
    """Download PDF using the intercepted UUID. Returns bytes or None."""
    if not uuid: return None
    url = f"{PDF_API}/v1/editions/{uuid}"
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code != 200:
            print(f"  PDF download {r.status_code} for {uuid[:20]}")
            return None
        data = b"".join(r.iter_content(65536))
        if data[:4] != b"%PDF": return None
        print(f"  PDF: {len(data)/1e6:.1f}MB")
        return data
    except Exception as e:
        print(f"  PDF err: {e}")
        return None


# ── SCAN ──────────────────────────────────────────────────────────
def scan_publications(pubs, excerpts, dom_text, caderno):
    lbl=caderno["label"]; jn=caderno["journalName"]; rsn=caderno["rootSectionName"]
    results=[]; seen=set(); kw_cnt={}; dom_low=normalize(dom_text)

    for kw in KEYWORDS:
        kn=normalize(kw); cat=KEYWORD_CATEGORIES.get(kw,"general")
        rules=KEYWORD_FILTERS.get(kw,{}); mh=rules.get("max_hits",999); cnt=0

        for pub in pubs:
            title_low=normalize(pub["title"])
            exc_text=excerpts.get(pub["id"],"")
            exc_low=normalize(exc_text)
            searchable=title_low+" "+exc_low

            if kn not in searchable: continue
            # Dedup by keyword + org path (not pub_id) to avoid 8 identical cards
            org_key = pub.get("path","")[:60]
            dedup=(kw, org_key)
            if dedup in seen: continue
            seen.add(dedup)
            if cnt>=mh: continue

            full_text=pub["title"]+" "+exc_text
            full_low=normalize(full_text)
            if not passes_filter(kw, full_low, full_text): continue

            fields=extract_fields(pub["title"], exc_text, pub["path"])
            v,sc,ft=tv_score(full_low, kw, full_text, fields)
            results.append({
                "keyword":kw,"veredito":v,"score":sc,"fatores":ft,"fields":fields,
                "ref":{"keyword":kw,"label":lbl,"journal":jn,"section":rsn,
                       "title":pub["title"][:150],"path":pub["path"],
                       "org":pub.get("org",""),"slug":pub.get("slug",""),"pub_id":pub["id"]}
            })
            cnt+=1; kw_cnt[kw]=cnt

    for kw,n in sorted(kw_cnt.items(),key=lambda x:-x[1]):
        print(f"    '{kw}': {n}")
    ap=sum(1 for r in results if r["veredito"].startswith("🟢"))
    pr=sum(1 for r in results if r["veredito"].startswith("🟡"))
    print(f"  [{lbl}] {len(pubs)} pubs → {len(results)} hits | 🟢{ap} 🟡{pr}")
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

# ── TV SCORING ────────────────────────────────────────────────────
def tv_score(text_low, keyword, text_raw, fields):
    cat=KEYWORD_CATEGORIES.get(keyword,"general")
    tier=CATEGORY_TV.get(cat,(3,"",""))[0]
    score={1:4,2:2,3:0}.get(tier,0); fatores=[]

    val=fields.get("valor","")
    if val:
        amt=parse_brl(val)
        if amt>=50_000_000: score+=5; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=10_000_000: score+=4; fatores.append(f"R${amt/1e6:.0f}M")
        elif amt>=1_000_000: score+=3; fatores.append(f"R${amt/1e6:.1f}M")
        elif amt>=100_000: score+=1; fatores.append(f"R${amt/1e3:.0f}k")

    if any(t in text_low for t in ["emergencial","urgente"]): score+=3; fatores.append("EMERGENCIAL")
    if any(t in text_low for t in ["superfaturamento","sobrepreço","fraude","improbidade"]): score+=4; fatores.append("SUSPEITO")
    if any(t in text_low for t in ["hospital das clínicas","leito de uti","organização social de saúde"]): score+=2; fatores.append("SAÚDE")
    if any(t in text_low for t in ["escola estadual","merenda","alimentação escolar"]): score+=2; fatores.append("EDUCAÇÃO")
    if any(t in text_low for t in ["unidade prisional","policial penal","penitenciária"]): score+=1; fatores.append("PENITENCIÁRIO")
    if any(t in text_low for t in ["demissão","suspensão por","aposentadoria compulsória"]): score+=2; fatores.append("SANÇÃO")
    if any(t in text_low for t in ["sabesp","metrô","cptm","dersa","trem intercidades"]): score+=2; fatores.append("INFRAESTRUTURA")
    if any(t in text_low for t in ["cetesb","contaminada","embargo"]): score+=1; fatores.append("AMBIENTAL")
    if any(t in text_low for t in ["privatização","desestatização","concessão"]): score+=2; fatores.append("PRIVATIZAÇÃO")
    if any(t in text_low for t in ["renúncia fiscal","benefício fiscal","isenção de icms"]): score+=3; fatores.append("FISCAL")
    if any(t in text_low for t in ["letalidade","morte em custódia","feminicídio"]): score+=3; fatores.append("VIOLÊNCIA")
    if any(t in text_low for t in ["tce","tribunal de contas","auditoria"]): score+=2; fatores.append("TCE")
    if fields.get("empresa") or fields.get("servidor"): score+=1; fatores.append("IDENTIFICADO")
    if fields.get("cnpj"): score+=1; fatores.append("CNPJ")
    if fields.get("processo") or fields.get("sei"): score+=1; fatores.append("SEI")

    if score>=8: v="🟢 APROVADA"
    elif score>=5: v="🟡 PODE RENDER"
    else: v="🔴 BACKGROUND"
    return v,score,fatores

# ── FICHA — clean structured card, NO raw excerpt ────────────────
def build_ficha(hit, date_str):
    ref=hit["ref"]; f=hit.get("fields",{}); kw=ref["keyword"]
    cat=KEYWORD_CATEGORIES.get(kw,"general")
    _,icon,cat_nome=CATEGORY_TV.get(cat,(3,"🔍","Geral"))
    lbl=ref.get("label",""); jn=ref.get("journal",""); rsn=ref.get("section","")
    emo=next((c["emoji"] for c in CADERNOS if c["label"]==lbl),"📋")
    link=caderno_url(jn,rsn)
    dd_mm = date_str[:5]  # "25/05" from "25/05/2026"

    # Extract city for Municípios
    city = extract_city(ref.get("path",""), lbl)

    # Abbreviate org path
    org = abbreviate(f.get("orgao",""), max_len=65)

    # Prioritized field assembly — critical fields NEVER truncated
    lines = []

    # Line 1: date + caderno (compact)
    cad_short = {"Normativos":"Normat.","Pessoal":"Pessoal","Gestão":"Gestão","Municípios":"Munic."}.get(lbl, lbl)
    jn_short = {"Executivo":"Exec.","Municípios":"Munic."}.get(jn, jn[:6])
    header = f"📋 {dd_mm} | {emo} {jn_short} › {cad_short}"
    if city: header += f" | 📍 {city}"
    lines.append(header)

    # Line 2: verdict
    lines.append(f"{hit['veredito']} | {icon} *{cat_nome}* | Score {hit['score']}")

    # Line 3: keyword
    lines.append(f"🔑 `{kw}`")
    lines.append("━━━")

    # PRIORITY 1: Government branch (abbreviated, FULL)
    if org: lines.append(f"🏛️ *{org}*")

    # PRIORITY 2: Title + page
    title = ref.get("title","")
    if title:
        if len(title) > 100:
            title = title[:97] + "…"
        lines.append(f"📄 {title}")
    if f.get("pagina"):
        lines.append(f"📖 *{f['pagina']}* do PDF")

    # PRIORITY 3: WHO — company/servidor (NEVER truncated)
    empresa = f.get("empresa","")
    if empresa:
        # Clean: don't let processo/SEI leak into empresa
        empresa = re.split(r'[,;]\s*(?:Processo|SEI|CNPJ|Valor|Objeto)', empresa, flags=re.I)[0].strip()
        lines.append(f"🏢 *{empresa[:80]}*")
    if f.get("cnpj"):       lines.append(f"   CNPJ: {f['cnpj']}")
    if f.get("servidor"):   lines.append(f"👤 *{f['servidor']}*")
    if f.get("rg"):         lines.append(f"   RG: {f['rg']}")
    if f.get("interessado") and not f.get("servidor") and not empresa:
        lines.append(f"👤 {f['interessado'][:60]}")

    # PRIORITY 4: HOW MUCH (NEVER truncated)
    if f.get("valor"):      lines.append(f"💰 *{f['valor']}*")

    # PRIORITY 5: WHAT (object — abbreviated)
    obj = f.get("objeto","")
    if obj:
        if len(obj) > 80: obj = obj[:77] + "…"
        lines.append(f"📦 {obj}")

    # PRIORITY 6: Penalty (for disciplinary)
    if f.get("penalidade"): lines.append(f"⚖️ *{f['penalidade']}*")
    if f.get("assunto"):    lines.append(f"📌 {f['assunto'][:60]}")

    # PRIORITY 7: WHEN + references
    if f.get("data"):       lines.append(f"📅 {f['data']}")
    if f.get("contrato"):   lines.append(f"📄 {f['contrato'][:40]}")
    if f.get("prazo"):      lines.append(f"⏱️ {f['prazo'][:30]}")
    if f.get("processo") or f.get("sei"):
        lines.append(f"🔖 {f.get('processo') or f.get('sei')}")
    if f.get("modalidade"): lines.append(f"📋 {f['modalidade'][:40]}")

    # MISSING — only for investigative/contract categories
    missing=[]
    if not empresa and not f.get("servidor") and not f.get("interessado"):
        if cat in ("contrato","licitacao","penalidade","urgencia","privatizacao"):
            missing.append("Empresa")
    if not f.get("cnpj") and cat in ("contrato","licitacao","penalidade"):
        missing.append("CNPJ")
    if not f.get("valor") and cat not in ("pessoal","disciplinar","educacao","seguranca","investigativo"):
        missing.append("Valor")
    if not f.get("processo") and not f.get("sei"):
        missing.append("Processo")
    if missing: lines.append(f"❓ Faltando: {' · '.join(missing)}")

    lines += ["━━━", f"🔗 [Portal]({link})"]
    return "\n".join(lines)


def build_summary(results_by_caderno, date_str, pub_counts):
    all_h=[h for v in results_by_caderno.values() for h in v]
    total=len(all_h)
    ap=sum(1 for h in all_h if h["veredito"].startswith("🟢"))
    pr=sum(1 for h in all_h if h["veredito"].startswith("🟡"))
    lines=[f"📋 *DOESP — {date_str}*",
           f"📊 *{total} resultado(s)* | 🟢 {ap}  🟡 {pr}  🔴 {total-ap-pr}\n"]
    for c in CADERNOS:
        lbl=c["label"]; hits=results_by_caderno.get(lbl,[])
        n_pubs=pub_counts.get(lbl,0)
        a=sum(1 for h in hits if h["veredito"].startswith("🟢"))
        p=sum(1 for h in hits if h["veredito"].startswith("🟡"))
        lines.append(f"{c['emoji']} *{lbl}* ({n_pubs} pub): {len(hits)} hits | 🟢{a} 🟡{p}" if hits
                     else f"{c['emoji']} *{lbl}* ({n_pubs} pub): —")
    lines.append("━"*20)
    for h in sorted(all_h,key=lambda x:-x["score"])[:12]:
        _,icon,_=CATEGORY_TV.get(KEYWORD_CATEGORIES.get(h["ref"]["keyword"],"general"),(3,"🔍",""))
        ref=h["ref"]; f=h.get("fields",{})
        emp=(f.get("empresa") or f.get("servidor") or "")[:25]
        val=f.get("valor","")[:15]
        org=(ref.get("org") or "")[:20]
        city=extract_city(ref.get("path",""), ref.get("label",""))
        pg=f.get("pagina","")
        l=f"{h['veredito'][:2]} {icon} `{ref['keyword'][:25]}`"
        if city: l+=f" 📍{city[:12]}"
        elif org: l+=f" {org[:20]}"
        else: l+=f" [{ref['label']}]"
        if emp: l+=f" | {emp[:25]}"
        if val: l+=f" | {val[:15]}"
        if pg: l+=f" | {pg}"
        lines.append(l)
    lines+=["━"*20,f"🔗 [Portal]({PORTAL_URL})"]
    return "\n".join(lines)

# ── TELEGRAM ──────────────────────────────────────────────────────
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

# ── MAIN ──────────────────────────────────────────────────────────
def main():
    hoje=datetime.date.today(); date_str=hoje.strftime("%d/%m/%Y")
    print(f"=== DOESP Monitor v5.3 — {date_str} ===\n")
    from playwright.sync_api import sync_playwright
    print("  Playwright: ✅\n")

    session = requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36",
        "Accept":"*/*","Accept-Language":"pt-BR,pt;q=0.9",
    })

    results_by_caderno={}; pub_counts={}

    with sync_playwright() as pw:
        browser=pw.chromium.launch(headless=True,
            args=["--no-sandbox","--disable-setuid-sandbox","--disable-dev-shm-usage","--disable-gpu"])

        for caderno in CADERNOS:
            lbl,emo=caderno["label"],caderno["emoji"]
            print(f"\n{'─'*60}\n{emo}  {caderno['journalName']} / {caderno['rootSectionName']}\n{'─'*60}")

            tree_data, excerpts, pdf_uuid, dom_text = process_caderno(browser, caderno)
            hits=[]

            if tree_data:
                pubs=extract_publications_from_tree(tree_data)
                pub_counts[lbl]=len(pubs)
                print(f"  [{lbl}] {len(pubs)} publicações na árvore")
                if pubs: hits=scan_publications(pubs, excerpts, dom_text, caderno)

            if not hits and len(dom_text)>2000:
                print(f"  [{lbl}] Fallback: DOM text")
                fn=normalize(dom_text)
                for kw in KEYWORDS:
                    if normalize(kw) in fn:
                        v,sc,ft=tv_score(fn,kw,dom_text,{})
                        hits.append({"keyword":kw,"veredito":v,"score":sc,"fatores":ft,"fields":{},
                            "ref":{"keyword":kw,"label":lbl,"journal":caderno["journalName"],
                                   "section":caderno["rootSectionName"],"title":"","path":"","org":"","slug":"","pub_id":""}})
                pub_counts.setdefault(lbl,0)

            # Add page numbers by matching titles against PDF
            if hits and pdf_uuid:
                print(f"  [{lbl}] Downloading PDF for page lookup...")
                pdf_bytes = download_pdf_for_pages(session, pdf_uuid)
                if pdf_bytes:
                    page_index = build_page_index(pdf_bytes)
                    mapped = 0
                    for h in hits:
                        pg = find_page(h["ref"].get("title",""), page_index)
                        if pg:
                            h["fields"]["pagina"] = f"p.{pg}"
                            mapped += 1
                    print(f"  [{lbl}] Pages mapped: {mapped}/{len(hits)}")

            if not hits:
                send_telegram(f"⚠️ *DOESP {date_str}* — {emo} [{lbl}]\nSem resultados\n🔗 [Verificar]({caderno_url(caderno['journalName'],caderno['rootSectionName'])})")
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
        for part in split_long(build_ficha(h, date_str)): send_telegram(part)
        time.sleep(0.5)

    if background:
        lines=[f"🗂️ *Background — {date_str}* — {len(background)} ref(s)"]
        for h in background[:25]:
            _,icon,_=CATEGORY_TV.get(KEYWORD_CATEGORIES.get(h["keyword"],"general"),(3,"🔍",""))
            ref=h["ref"]; f=h.get("fields",{})
            emp=f.get("empresa","") or f.get("servidor","")
            val=f.get("valor","")
            org=(ref.get("org") or "")[:25]
            pg=f.get("pagina","")
            l=f"{icon} `{h['keyword'][:25]}` [{ref['label']}]"
            if org: l+=f" {org}"
            if emp: l+=f" | {emp[:25]}"
            if val: l+=f" | {val[:15]}"
            if pg: l+=f" | {pg}"
            lines.append(l)
        send_telegram("\n".join(lines), silent=True)

if __name__=="__main__":
    main()
