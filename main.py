"""
DOESP Monitor — Diário Oficial do Estado de São Paulo
======================================================
Portal: https://doe.sp.gov.br/sumario

Como funciona:
  1. Acessa o sumário do dia em doe.sp.gov.br
  2. Extrai o UUID da edição de hoje a partir do JSON embutido na página
     (__NEXT_DATA__) — sem precisar de login ou API key
  3. Baixa o PDF via do-api-publication-pdf.doe.sp.gov.br
  4. Extrai o texto, varre as palavras-chave e envia alertas no Telegram

Cadernos disponíveis (novo portal, desde março 2024):
  Executivo - Atos Normativos         ← monitorado (decretos, portarias, contratos)
  Executivo - Atos de Pessoal         ← monitorado
  Executivo - Atos de Gestão e Despesas
  Legislativo
  Municípios

Secrets necessários no repositório GitHub:
  TELEGRAM_TOKEN  — token do bot (@BotFather)
  CHAT_ID         — ID do chat de destino
"""

import requests
import datetime
import os
import sys
import re
import json
import unicodedata
import io
import time

# ---------------------------------------------------------------------------
# Credenciais
# ---------------------------------------------------------------------------
TELEGRAM_TOKEN = os.getenv("TELEGRAM_TOKEN")
CHAT_ID        = os.getenv("CHAT_ID")

if not TELEGRAM_TOKEN or not CHAT_ID:
    print("🚨 FATAL: TELEGRAM_TOKEN ou CHAT_ID ausentes.")
    sys.exit(1)

# ---------------------------------------------------------------------------
# Configuração
# ---------------------------------------------------------------------------
WINDOW_SIDE  = 1500
TG_MAX       = 4000
SOURCE_NAME  = "DOESP"
SOURCE_EMOJI = "📋"
PORTAL_URL   = "https://doe.sp.gov.br/sumario"
SUMARIO_URL  = "https://doe.sp.gov.br/sumario"
PDF_API_BASE = "https://do-api-publication-pdf.doe.sp.gov.br"

# Cadernos a monitorar (em ordem de preferência)
# O código busca pelo primeiro que encontrar na página
TARGET_CADERNOS = [
    "normativos",   # Executivo - Atos Normativos
    "executivo",    # qualquer caderno executivo se o primeiro falhar
]

# ---------------------------------------------------------------------------
# Palavras-chave — nível estadual
# ---------------------------------------------------------------------------
KEYWORD_CATEGORIES = {
    "extrato de contrato":                  "contract",
    "termo de aditamento":                  "contract",
    "contratação emergencial":              "contract",
    "rescindido o contrato":                "contract",
    "dispensa de licitação":                "procurement",
    "inexigibilidade de licitação":         "procurement",
    "licitação deserta":                    "procurement",
    "crédito adicional suplementar":        "budget",
    "aplicação de penalidade":              "penalty",
    "multa contratual":                     "penalty",
    "nomeação para cargo em comissão":      "personnel",
    "exoneração a pedido":                  "personnel",
    "exoneração de servidor":               "personnel",
    "demissão de servidor":                 "personnel",
    "aposentadoria compulsória":            "personnel",
    "sindicância":                          "legal",
    "processo administrativo disciplinar":  "legal",
    "ação civil pública":                   "legal",
    "improbidade administrativa":           "investigative",
    "superfaturamento":                     "investigative",
    "sobrepreço":                           "investigative",
    "desvio de verba":                      "investigative",
    "fraude em licitação":                  "investigative",
    "lavagem de dinheiro":                  "investigative",
    # Temático
    "merenda escolar":                      "educacao",
    "transporte escolar":                   "educacao",
    "construção de escola estadual":        "educacao",
    "fechamento de escola":                 "educacao",
    "concurso de professor":                "educacao",
    "hospital de clínicas":                 "saude",
    "medicamento de alto custo":            "saude",
    "leito de UTI":                         "saude",
    "organização social de saúde":          "saude",
    "dengue":                               "saude",
    "operação policial":                    "seguranca",
    "delegacia de polícia":                 "seguranca",
    "unidade prisional":                    "seguranca",
    "morte em custódia":                    "seguranca",
    "feminicídio":                          "seguranca",
    "obra paralisada":                      "obras",
    "concessão rodoviária":                 "obras",
    "habitação de interesse social":        "obras",
    "saneamento básico":                    "obras",
    "licença ambiental":                    "meio_ambiente",
    "auto de infração ambiental":           "meio_ambiente",
    "CETESB":                               "meio_ambiente",
    "área contaminada":                     "meio_ambiente",
}

KEYWORDS = sorted(KEYWORD_CATEGORIES.keys(), key=len, reverse=True)

CATEGORY_ICONS = {
    "contract":"📝","procurement":"🛒","budget":"💼","personnel":"👤",
    "penalty":"⚖️","legal":"🏛️","investigative":"🔎","educacao":"🎓",
    "saude":"🏥","seguranca":"🚔","obras":"🏗️","meio_ambiente":"🌿","general":"🔍",
}

KEYWORD_FILTERS = {
    "extrato de contrato": {
        "min_value":500_000,"min_window":500,"max_hits":15,
        "require_any":["cnpj","contratad"],
    },
    "inexigibilidade de licitação": {"min_value":50_000},
    "aplicação de penalidade": {
        "min_value":10_000,
        "require_any":["aplico","auto de imposição","pena pecuniária","notificamos","sanção"],
        "skip_if":["retirada de nota de empenho","deixo de aplicar penalidade",
                   "nos casos de aplicação de multa moratória"],
        "max_hits_per_cnpj":3,
    },
    "organização social de saúde":{"require_any":["contrato de gestão","OS ","SPDM"]},
    "CETESB":{"require_any":["multa","embargo","auto de infração","licença"]},
    "dengue":{"require_any":["caso","foco","combate","surto","contrato"],
              "skip_if":["projeto de lei"]},
}

# ===========================================================================
# DESCOBERTA DO PDF — extrai UUID do __NEXT_DATA__
# ===========================================================================
def get_edition_uuid(session: requests.Session) -> str | None:
    """
    Baixa o HTML da página de sumário, extrai o JSON embutido (__NEXT_DATA__)
    e procura o UUID da edição Executivo/Atos Normativos de hoje.
    """
    try:
        r = session.get(SUMARIO_URL, timeout=20)
        r.encoding = "utf-8"
        html = r.text
    except Exception as e:
        print(f"  ⚠️ Erro ao acessar sumário: {e}"); return None

    # Extrair bloco __NEXT_DATA__
    m = re.search(
        r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
        html, re.DOTALL)
    if not m:
        print("  ⚠️ __NEXT_DATA__ não encontrado no HTML.")
        print("     Salvar HTML para diagnóstico:")
        with open("/tmp/doesp_sumario.html", "w", encoding="utf-8") as f:
            f.write(html)
        print("     Arquivo salvo em /tmp/doesp_sumario.html")
        return None

    try:
        data = json.loads(m.group(1))
    except json.JSONDecodeError as e:
        print(f"  ⚠️ JSON inválido: {e}"); return None

    # Busca recursiva por UUIDs de edições
    UUID_PAT = re.compile(
        r'^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$',
        re.IGNORECASE)

    def buscar(obj, parent_name="", depth=0):
        if depth > 10:
            return None
        if isinstance(obj, dict):
            # Extrair nome deste nó para identificar o caderno
            name = ""
            for k in ("name","nome","title","titulo","type","caderno"):
                v = obj.get(k, "")
                if isinstance(v, str) and v:
                    name = v.lower(); break
                elif isinstance(v, dict):
                    name = str(v.get("name","") or v.get("nome","")).lower(); break

            combined = (parent_name + " " + name).lower()

            # Se parece ser o caderno-alvo, procurar UUID neste nó
            is_target = any(t in combined for t in TARGET_CADERNOS)
            if is_target:
                for key in ("id","uuid","editionId","edition_id","fileId","pdfId"):
                    val = str(obj.get(key, ""))
                    if UUID_PAT.match(val):
                        print(f"  Caderno: '{name}' | chave: '{key}' | UUID: {val}")
                        return val

            # Continuar recursão
            for k, v in obj.items():
                r = buscar(v, combined, depth+1)
                if r: return r

        elif isinstance(obj, list):
            for item in obj:
                r = buscar(item, parent_name, depth+1)
                if r: return r
        return None

    uuid = buscar(data)
    if uuid:
        return uuid

    # Diagnóstico: listar estrutura de pageProps
    page_props = data.get("props",{}).get("pageProps",{})
    print("  ⚠️ UUID não encontrado. Chaves de pageProps:", list(page_props.keys()))
    print("  Adapte TARGET_CADERNOS ou buscar() se a estrutura do site mudou.")
    return None

def baixar_pdf(session: requests.Session, uuid: str) -> bytes | None:
    url = f"{PDF_API_BASE}/v1/editions/{uuid}"
    print(f"  URL: {url}")
    try:
        r = session.get(url, timeout=120, stream=True)
        if r.status_code != 200:
            print(f"  ⚠️ HTTP {r.status_code}"); return None
        data = b"".join(r.iter_content(65536))
        if data[:4] != b"%PDF":
            print(f"  ⚠️ Não é PDF"); return None
        print(f"  ✅ {len(data):,} bytes")
        return data
    except Exception as e:
        print(f"  ⚠️ {e}"); return None

# ===========================================================================
# HELPERS
# ===========================================================================
def normalize(t):
    return "".join(c for c in unicodedata.normalize("NFKD",t)
                   if not unicodedata.combining(c)).lower()

def parse_brl(s):
    if not s: return 0.0
    m = re.search(r'R\$\s*([\d.,]+)', s, re.IGNORECASE)
    if not m: return 0.0
    v = re.sub(r'\.(?=\d{3}(\D|$))','',m.group(1)).replace(',','.')
    try: return float(v)
    except: return 0.0

# ===========================================================================
# TELEGRAM
# ===========================================================================
def send_telegram(text, silent=False):
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
            json={"chat_id":CHAT_ID,"text":text,"parse_mode":"Markdown",
                  "disable_web_page_preview":True,"disable_notification":silent},
            timeout=10)
        ok = r.status_code==200
        print("  ✅ OK" if ok else f"  🚨 {r.status_code}")
        return ok
    except Exception as e:
        print(f"  🚨 {e}"); return False

def send_pdf(pdf_bytes, caption):
    nome = f"doesp_{datetime.date.today():%Y-%m-%d}.pdf"
    try:
        r = requests.post(
            f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendDocument",
            files={"document":(nome,pdf_bytes,"application/pdf")},
            data={"chat_id":CHAT_ID,"caption":caption,"parse_mode":"Markdown"},
            timeout=180)
        ok = r.status_code==200
        print("  ✅ PDF" if ok else f"  🚨 PDF {r.status_code}")
        return ok
    except Exception as e:
        print(f"  🚨 PDF {e}"); return False

def send_parts(parts):
    for p in parts:
        send_telegram(p); time.sleep(0.6)

def split_messages(header, blocks, footer="", kw="", total=0):
    msgs=[]; current=header; parte=1
    icon = CATEGORY_ICONS.get(KEYWORD_CATEGORIES.get(kw,"general"),"🔍")
    l1   = header.split("\n")[0]
    for bloco in blocks:
        if len(current+"\n"+bloco)+len(footer) > TG_MAX:
            msgs.append(current); parte+=1
            current = f"{l1}\n{icon} 🔍 *Busca: {kw.upper()}* _{parte}ª parte — {total} total_\n"+bloco
        else:
            current = current+"\n"+bloco
    if current.strip(): msgs.append(current+("\n"+footer if footer else ""))
    return msgs

# ===========================================================================
# REGEX + EXTRAÇÃO DE CAMPOS
# ===========================================================================
_RE_MONEY   = re.compile(r'R\$\s*[\d.,]+(?:\s*\([^)]{0,80}\))?',re.IGNORECASE)
_RE_CNPJ    = re.compile(r'\d{2}[\.\s]?\d{3}[\.\s]?\d{3}[/\s]?\d{4}[-\s]?\d{2}')
_RE_RF      = re.compile(r'R\.?F\.?[:\s]*[\d.]+(?:/\d+)?',re.IGNORECASE)
_RE_DATE    = re.compile(r'\b\d{2}/\d{2}/\d{4}\b|\b\d{1,2}\s+de\s+\w+\s+de\s+\d{4}\b',re.IGNORECASE)
_RE_PROCESS = re.compile(r'(?:processo|SEI|proc\.?)\s*[snº°.]*\s*[\d./-]{6,}',re.IGNORECASE)
_RE_CONT    = re.compile(r'(?:contrato|termo)\s*[nº°.]*\s*[\w/.-]+',re.IGNORECASE)

_LBL = re.compile(
    r'(?:APLICO\s+[àa]\s+(?:empresa\s+|Empresa\s+)|CONTRATAD[AO]:\s+|Contratad[ao]:\s+'
    r'|(?:empresa|Empresa)\s+|Infrator:\s+|INTERESSAD[AO]:\s+|Interessad[ao]:\s+'
    r'|em\s+nome\s+de\s+|em\s+favor\s+de\s+)'
    r'([A-Za-záéíóúàâêîôûãõçÁÉÍÓÚÀÂÊÎÔÛÃÕÇ][^,\n─│|]{4,100}?'
    r'(?:LTDA|S/?A|EIRELI|ME|EPP|Ltda\.?|S\.A\.?|Eireli|COOPERATIVA|ASSOCIAÇÃO)\.?)',
    re.IGNORECASE|re.UNICODE)
_CAPS = re.compile(
    r'([A-ZÁÉÍÓÚÀÂÊÎÔÛÃÕÇ][A-ZÁÉÍÓÚÀÂÊÎÔÛÃÕÇ\s&,./()-]{8,80}'
    r'(?:LTDA|S/?A|EIRELI|ME|EPP|SERVIÇOS|CONSTRUÇÕES|CONSULTORIA|ENGENHARIA|EMPREENDIMENTOS)\.?)',
    re.UNICODE)
_NCAPS = {'EXTRATO','OBJETO','DEPARTAMENTO','CLÁUSULA','PROCESSO','SECRETARIA',
          'PREFEITURA','DIRETORIA','COMISSÃO','CONTRATANTE','CONTRATADA','PORTARIA','DECRETO'}
_NLBL = {'empresa','contratada','contratado','interessada','interessado',
         'para','de','do','da','que','notificada','notificado'}

def first(pat, text):
    m = pat.search(text)
    return m.group(0).strip() if m else None

def get_company(w):
    for m in _LBL.finditer(w):
        n = m.group(1).strip().rstrip('.,; ')
        if len(n)<8: continue
        if n.split()[0].lower().rstrip('.,:-') in _NLBL: continue
        if re.match(r'(?:para|de\s|do\s|da\s)',n,re.I): continue
        return n
    for m in _CAPS.finditer(w):
        n=m.group(0).strip()
        if re.split(r'[\s\-]',n)[0].rstrip('.,:-').upper() not in _NCAPS: return n
    return None

def get_org(w):
    m=re.search(r'Secretaria\s+(?:de\s+Estado\s+d[aeo]|d[aeo]\s+)'
                r'([A-Za-záéíóúàâêîôûãõç][^\n│─|,]{3,60}?)(?=\s*[-\n│─|,]|$)',w,re.I)
    if m: return f"Sec. {m.group(1).strip()}"
    m2=re.search(r'\b(SSP|SES|SEE|SEDS|SIMA|SABESP|CETESB|CDHU|DER)\b',w)
    if m2: return m2.group(1)
    return None

def extract_fields(window, category, keyword=""):
    f={}
    if category=="contract":
        f["💰 Valor"]=first(_RE_MONEY,window); f["🏢 Empresa"]=get_company(window)
        f["📄 Contrato"]=first(_RE_CONT,window); f["🔖 Processo"]=first(_RE_PROCESS,window)
        dc=re.search(r'(?:vig[êe]ncia|prazo).{0,100}?(\d{2}/\d{2}/\d{4})',window,re.I)
        f["📅 Vigência"]=dc.group(1) if dc else first(_RE_DATE,window)
        ob=re.search(r'objeto[:\s]+(.{10,150}?)(?:\.|$)',window,re.I)
        f["📦 Objeto"]=ob.group(1).strip() if ob else None
    elif category=="procurement":
        f["💰 Valor"]=first(_RE_MONEY,window); f["🏢 Empresa"]=get_company(window)
        f["📄 CNPJ"]=first(_RE_CNPJ,window); f["🔖 Processo"]=first(_RE_PROCESS,window)
        ob=re.search(r'objeto[:\s]+(.{10,150}?)(?:\.|,|$)',window,re.I)
        f["📦 Objeto"]=ob.group(1).strip() if ob else None
    elif category=="budget":
        f["💰 Valor"]=first(_RE_MONEY,window); f["📅 Data"]=first(_RE_DATE,window)
        dc=re.search(r'(DECRETO|PORTARIA)\s+[Nnº°.]*\s*([\d.,/]+)',window,re.I)
        f["📜 Instrumento"]=f"{dc.group(1).upper()} Nº {dc.group(2)}" if dc else None
        f["🏛️ Órgão"]=get_org(window)
    elif category=="personnel":
        caps=re.search(r'(?:EXONERANDO|NOMEANDO|EXONERO|NOMEAR?)'
                       r'[^:]{0,60}:\s*([A-ZÁÉÍÓÚ][A-ZÁÉÍÓÚ\s]{5,60}?)'
                       r'(?=\s+R\.?F\.?|\s+RF\b)',window,re.I|re.U)
        f["👤 Servidor"]=caps.group(1).strip() if caps else None
        f["🪪 RF"]=first(_RE_RF,window); f["🏛️ Órgão"]=get_org(window)
        cg=re.search(r'(?:cargo\s+de|CARGO:)[:\s]+(.{5,80}?)(?:\.|,|;|$)',window,re.I)
        f["💼 Cargo"]=cg.group(1).strip() if cg else None
        f["📅 Data"]=first(_RE_DATE,window)
    elif category=="penalty":
        tp=re.search(r'(?:pena(?:lidade)?\s+de\s+)(MULTA|ADVERTÊNCIA|SUSPENSÃO)',window,re.I)
        f["📋 Tipo"]=tp.group(1).upper() if tp else None
        f["💰 Valor"]=first(_RE_MONEY,window); f["🏢 Empresa"]=get_company(window)
        f["📄 CNPJ"]=first(_RE_CNPJ,window); f["🏛️ Órgão"]=get_org(window)
        f["🔖 Processo"]=first(_RE_PROCESS,window)
    elif category in ("legal","investigative"):
        f["🔖 Processo"]=first(_RE_PROCESS,window); f["🏢 Empresa"]=get_company(window)
        f["📄 CNPJ"]=first(_RE_CNPJ,window); f["💰 Valor"]=first(_RE_MONEY,window)
        f["📅 Data"]=first(_RE_DATE,window)
    else:
        f["💰 Valor"]=first(_RE_MONEY,window); f["🏢 Empresa"]=get_company(window)
        f["🔖 Processo"]=first(_RE_PROCESS,window); f["📅 Data"]=first(_RE_DATE,window)
    return {k:v for k,v in f.items() if v}

# ===========================================================================
# EXTRAÇÃO DE TEXTO (PDF coluna dupla)
# ===========================================================================
def extract_text(pdf_bytes):
    from pdfminer.high_level import extract_pages, extract_text as pm_extract
    from pdfminer.layout import LTTextBox, LAParams
    lp = LAParams(line_margin=0.4,char_margin=2.0,word_margin=0.1,boxes_flow=None)
    parts=[]
    try:
        for page in extract_pages(io.BytesIO(pdf_bytes),laparams=lp):
            mid = page.width*0.52
            left =sorted([(el.bbox[3],el.get_text()) for el in page
                          if isinstance(el,LTTextBox) and el.get_text().strip()
                          and (el.bbox[0]+el.bbox[2])/2<mid],reverse=True)
            right=sorted([(el.bbox[3],el.get_text()) for el in page
                          if isinstance(el,LTTextBox) and el.get_text().strip()
                          and (el.bbox[0]+el.bbox[2])/2>=mid],reverse=True)
            parts.append("\n".join(t for _,t in left+right))
            parts.append("\x0c")
        texto="\n".join(parts)
        print(f"  {len(texto):,} chars"); return texto
    except Exception as e:
        print(f"  Fallback ({e})")
        try: return pm_extract(io.BytesIO(pdf_bytes))
        except: return ""

# ===========================================================================
# VARREDURA
# ===========================================================================
BPATS=[r'\x0c',r'[─━═\-]{5,}',r'\n[^\n|]{3,60}\|\s*Documento:\s*\d+',
       r'\n\s*(?:RESOLUÇÃO|PORTARIA|DECRETO)\s+']

def build_boundaries(text):
    bs=set()
    for p in BPATS:
        for m in re.finditer(p,text): bs.add(m.start())
    return sorted(bs)

def get_window(text,pos,kl,boundaries):
    lc=[b for b in boundaries if b<pos]
    rc=[b for b in boundaries if b>pos+kl]
    l=max(max(lc,default=pos-WINDOW_SIDE),pos-WINDOW_SIDE)
    r=min(min(rc,default=pos+kl+WINDOW_SIDE),pos+kl+WINDOW_SIDE)
    return re.sub(r'\s+',' ',text[l:r]).strip()

def passes(kw,window):
    rules=KEYWORD_FILTERS.get(kw,{}); wl=window.lower()
    mn=rules.get("min_window")
    if mn and len(window)<mn: return False
    req=rules.get("require_any",[])
    if req and not any(p.lower() in wl for p in req): return False
    for ph in rules.get("skip_if",[]):
        if ph.lower() in wl: return False
    mv=rules.get("min_value")
    if mv:
        vs=first(_RE_MONEY,window)
        if vs:
            a=parse_brl(vs)
            if 0<a<mv: return False
    return True

def scan(full_text):
    fn=normalize(full_text); bs=build_boundaries(full_text)
    print(f"  Delimitadores: {len(bs)}")
    pbs=[(m.start(),f"p.{i+1}") for i,m in enumerate(re.finditer(r"\x0c",full_text))]
    def pag(pos):
        p="p.1"
        for pp,pl in pbs:
            if pp<=pos: p=pl
            else: break
        return p
    res=[]
    for kw in KEYWORDS:
        kn=normalize(kw); cat=KEYWORD_CATEGORIES.get(kw,"general")
        rules=KEYWORD_FILTERS.get(kw,{})
        mh=rules.get("max_hits",999); mc=rules.get("max_hits_per_cnpj",999)
        sp=0; ace=0; cc={}
        while True:
            pos=fn.find(kn,sp)
            if pos==-1: break
            w=get_window(full_text,pos,len(kw),bs)
            if not passes(kw,w): sp=pos+max(len(kn),600); continue
            if mc<999:
                cr=first(_RE_CNPJ,w)
                if cr:
                    ck=re.sub(r'[\s.]','',cr)
                    if cc.get(ck,0)>=mc: sp=pos+max(len(kn),600); continue
                    cc[ck]=cc.get(ck,0)+1
            res.append({"keyword":kw,"category":cat,"page":pag(pos),
                        "fields":extract_fields(w,cat,kw),"window":w})
            ace+=1; sp=pos+max(len(kn),600)
            if ace>=mh: break
        if ace: print(f"  ✅ '{kw}': {ace}")
    return res

# ===========================================================================
# MENSAGENS
# ===========================================================================
def build_messages(kw,hits,date_str):
    cat=hits[0]["category"]; icon=CATEGORY_ICONS.get(cat,"🔍")
    header=f"🚨 *{SOURCE_NAME} — {date_str}*\n{icon} 🔍 *Busca: {kw.upper()}* — {len(hits)} ocorrência(s)"
    footer=f"🔗 [Abrir portal]({PORTAL_URL})"
    blocks=[]
    for i,h in enumerate(hits,1):
        lines=[f"\n{'━'*20}",f"*[{i}/{len(hits)}]* 📍 {h['page']}"]
        if h["fields"]:
            for label,value in h["fields"].items(): lines.append(f"  {label}: {value}")
        else: lines.append("  _Sem campos extraídos_")
        lines.append("\n  📄 *Contexto:*")
        raw=re.sub(r'([_*\[\]()~`>#+\-=|{}.!])',r'\\\1',h["window"])
        lines.append(f"  {raw}"); blocks.append("\n".join(lines))
    return split_messages(header,blocks,footer,kw=kw,total=len(hits))

def build_summary(matches,date_str):
    by_kw={}
    for m in matches: by_kw.setdefault(m["keyword"],[]).append(m)
    lines=[f"{SOURCE_EMOJI} *{SOURCE_NAME} — {date_str}*",
           f"📋 *{len(by_kw)} termo(s)* — {len(matches)} ocorrência(s)\n",
           "━━━━━━━━━━━━━━━━━━━━"]
    for kw,hits in by_kw.items():
        lines.append(f"{CATEGORY_ICONS.get(hits[0]['category'],'🔍')} 🔍 *{kw}*: {len(hits)}")
    lines+=["━━━━━━━━━━━━━━━━━━━━",f"\n🔗 [Abrir portal]({PORTAL_URL})",
            "_PDF completo na próxima mensagem._"]
    return "\n".join(lines)

# ===========================================================================
# MAIN
# ===========================================================================
def main():
    hoje=datetime.date.today(); date_str=hoje.strftime("%d/%m/%Y")
    print(f"=== {SOURCE_NAME} Monitor — {date_str} ===\n")
    try:
        requests.post(f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage",
                      json={"chat_id":CHAT_ID,"text":f"🤖 {SOURCE_NAME} iniciado — {date_str}",
                            "disable_notification":True},timeout=10)
    except: pass

    session=requests.Session()
    session.headers.update({
        "User-Agent":"Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
                     "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
        "Accept-Language":"pt-BR,pt;q=0.9",
    })

    print("🔍 Buscando edição de hoje no sumário…")
    uuid = get_edition_uuid(session)
    if not uuid:
        send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}*\n"
                      "Não foi possível localizar o UUID da edição.\n"
                      "Verifique o log do GitHub Actions.\n"
                      f"Portal: {PORTAL_URL}")
        sys.exit(1)

    print(f"\n⬇️  Baixando PDF (UUID: {uuid})…")
    pdf_bytes=baixar_pdf(session,uuid)
    if not pdf_bytes:
        send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}*\nErro ao baixar PDF.")
        sys.exit(1)

    print("\n📖 Extraindo texto…")
    full_text=extract_text(pdf_bytes)
    if not full_text or len(full_text)<500:
        send_telegram(f"⚠️ *{SOURCE_NAME} — {date_str}*\nExtração falhou.")
        sys.exit(1)

    print("\n🔎 Varrendo…")
    matches=scan(full_text)
    print(f"\n  Total: {len(matches)}")

    if not matches:
        send_telegram(f"✅ *{SOURCE_NAME} — {date_str}*\nNenhum termo encontrado.")
        return

    print("\n📨 Resumo…")
    send_telegram(build_summary(matches,date_str))
    time.sleep(0.5)

    print("📎 PDF…")
    send_pdf(pdf_bytes,f"📋 *{SOURCE_NAME} — {date_str}* · {len(pdf_bytes)/1e6:.1f} MB")
    time.sleep(1)

    by_kw={}
    for m in matches: by_kw.setdefault(m["keyword"],[]).append(m)
    for kw,hits in by_kw.items():
        print(f"\n📨 '{kw}' ({len(hits)})…")
        send_parts(build_messages(kw,hits,date_str))
        time.sleep(0.5)

if __name__=="__main__":
    main()
