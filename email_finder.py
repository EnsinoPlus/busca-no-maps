"""
Tenta encontrar um e-mail de contato visitando o site do negócio.
Estrategia, em ordem de confianca:
  1. Links mailto: (o dono do site colocou ali de proposito)
  2. Dados estruturados JSON-LD / schema.org (campo "email")
  3. Texto solto na pagina (regex)
Se nada for encontrado, gera uma SUGESTAO de e-mail baseada no dominio
(ex: contato@dominio.com.br) - isso NAO e um dado confirmado, e so um
palpite de formato comum, retornado separadamente em 'email_sugerido'.

Retorno: (email_confirmado, fonte, email_sugerido)
  - email_confirmado: melhor e-mail encontrado no site, ou None
  - fonte: 'mailto' | 'jsonld' | 'texto' | None
  - email_sugerido: palpite tipo contato@dominio.com.br, ou None
"""
import re
import json
import requests
import urllib.parse

EMAIL_REGEX = re.compile(r"[a-zA-Z0-9._%+\-]+@[a-zA-Z0-9.\-]+\.[a-zA-Z]{2,}")
MAILTO_REGEX = re.compile(r'mailto:([^"\'\?\s>]+)', re.IGNORECASE)

IGNORE_DOMAINS = (
    "sentry.io", "example.com", "wixpress.com", "godaddy.com", "schema.org",
    "w3.org", "gravatar.com", "sentry-next.wixpress.com",
)

# Apenas as paginas mais provaveis de ter contato -> menos requisicoes por site
CANDIDATE_PATHS = ["", "/contato", "/fale-conosco"]

# Formatos comuns de palpite quando nao achamos nada confirmado
SUGGEST_PATTERNS = ["contato", "atendimento", "faleconosco", "suporte", "contato@"]

HEADERS = {"User-Agent": "Mozilla/5.0 (compatible; LeadsMapsBot/1.0)"}

# Backoff simples para nao sobrecarregar o site
REQUEST_TIMEOUT = 8


def _valid(email):
    return not any(d in email.lower() for d in IGNORE_DOMAINS)


def _from_mailto(html):
    found = {m.strip() for m in MAILTO_REGEX.findall(html)}
    return {e for e in found if _valid(e)}


def _from_jsonld(html):
    emails = set()
    for match in re.findall(
        r'<script[^>]*type=["\']application/ld\+json["\'][^>]*>(.*?)</script>',
        html, re.DOTALL | re.IGNORECASE,
    ):
        try:
            data = json.loads(match.strip())
        except (json.JSONDecodeError, TypeError):
            continue
        items = data if isinstance(data, list) else [data]
        for item in items:
            if isinstance(item, dict):
                email = item.get("email")
                if email and _valid(email):
                    emails.add(email)
    return emails


def _from_text(html):
    found = set(EMAIL_REGEX.findall(html))
    return {e for e in found if _valid(e)}


def _clean(email):
    """Remove codificacao de URL (ex: %20) e espacos antes/depois do e-mail."""
    return urllib.parse.unquote(email).strip()


def _domain(website_url):
    try:
        netloc = urllib.parse.urlparse(website_url).netloc.lower()
        if netloc.startswith("www."):
            netloc = netloc[4:]
        return netloc or None
    except Exception:
        return None


def _suggest(website_url):
    domain = _domain(website_url)
    if not domain:
        return None
    for prefix in ("contato", "atendimento", "faleconosco", "suporte"):
        cand = f"{prefix}@{domain}"
        if _valid(cand):
            return cand
    return None


def find_email(website_url, timeout=REQUEST_TIMEOUT):
    """Retorna (email_confirmado, fonte, email_sugerido)."""
    if not website_url:
        return None, None, None

    base = website_url.rstrip("/")
    domain = _domain(website_url)
    confirmados = set()

    for path in CANDIDATE_PATHS:
        url = base + path
        try:
            resp = requests.get(url, headers=HEADERS, timeout=timeout)
            if resp.status_code != 200:
                continue
        except requests.RequestException:
            continue

        html = resp.text

        mailto = _from_mailto(html)
        if mailto:
            confirmados |= mailto
        jsonld = _from_jsonld(html)
        if jsonld:
            confirmados |= jsonld
        texto = _from_text(html)
        if texto:
            confirmados |= texto

        # achou confirmado na home/contato? ja pode parar
        if confirmados:
            break

    confirmados = {_clean(e) for e in confirmados if _clean(e)}
    if confirmados:
        # preferir o que parece mais "comercial" (contato/atendimento) se houver
        ordenado = sorted(confirmados)
        for e in ordenado:
            if any(p in e.lower() for p in ("contato", "atendimento", "fale", "suporte", "comercial")):
                return e, "mailto", None
        return ordenado[0], "texto", None

    return None, None, _suggest(website_url)
