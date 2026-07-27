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
import ipaddress
import json
import re
import socket
import urllib.parse

import requests
from requests.adapters import HTTPAdapter

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
MAX_RESPONSE_BYTES = 1_000_000


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
        netloc = netloc.removeprefix("www.")
        return netloc or None
    except (TypeError, ValueError):
        return None


def _resolve_public_url(url):
    """Valida a URL e retorna os componentes mais um IP público já resolvido."""
    try:
        parsed = urllib.parse.urlsplit(url)
        if parsed.scheme.lower() not in {"http", "https"}:
            return None
        if not parsed.hostname or parsed.username or parsed.password:
            return None
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = sorted({
            item[4][0]
            for item in socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
        })
        if not addresses or not all(ipaddress.ip_address(address).is_global for address in addresses):
            return None
        return parsed, addresses[0]
    except (ValueError, OSError):
        return None


def _is_safe_public_url(url):
    """Aceita apenas HTTP(S) cujo DNS resolva exclusivamente para IPs públicos."""
    return _resolve_public_url(url) is not None


class _PinnedIPAdapter(HTTPAdapter):
    """Conecta ao IP validado sem perder SNI/verificação TLS do hostname original."""

    def __init__(self, tls_hostname):
        self._tls_hostname = tls_hostname
        super().__init__()

    def init_poolmanager(self, connections, maxsize, block=False, **pool_kwargs):
        pool_kwargs["server_hostname"] = self._tls_hostname
        pool_kwargs["assert_hostname"] = self._tls_hostname
        return super().init_poolmanager(connections, maxsize, block=block, **pool_kwargs)


def _fetch_html(url, timeout):
    resolved = _resolve_public_url(url)
    if resolved is None:
        return None
    parsed, address = resolved
    ip_netloc = f"[{address}]" if ipaddress.ip_address(address).version == 6 else address
    if parsed.port is not None:
        ip_netloc = f"{ip_netloc}:{parsed.port}"
    pinned_url = urllib.parse.urlunsplit(
        (parsed.scheme, ip_netloc, parsed.path or "/", parsed.query, "")
    )
    default_port = 443 if parsed.scheme.lower() == "https" else 80
    host_header = parsed.hostname
    if parsed.port is not None and parsed.port != default_port:
        host_header = f"{host_header}:{parsed.port}"
    session = requests.Session()
    session.trust_env = False
    if parsed.scheme.lower() == "https":
        session.mount("https://", _PinnedIPAdapter(parsed.hostname))
    try:
        resp = session.get(
            pinned_url,
            headers={**HEADERS, "Host": host_header},
            timeout=timeout,
            allow_redirects=False,
            stream=True,
        )
        if resp.status_code != 200:
            return None
        content_type = resp.headers.get("Content-Type", "").lower()
        if content_type and "text/html" not in content_type and "application/xhtml+xml" not in content_type:
            return None
        chunks = []
        total = 0
        for chunk in resp.iter_content(chunk_size=16_384):
            total += len(chunk)
            if total > MAX_RESPONSE_BYTES:
                return None
            chunks.append(chunk)
        encoding = resp.encoding or "utf-8"
        return b"".join(chunks).decode(encoding, errors="replace")
    except requests.RequestException:
        return None
    finally:
        session.close()


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

    if not _is_safe_public_url(website_url):
        return None, None, None

    base = website_url.rstrip("/")
    confirmados = set()

    for path in CANDIDATE_PATHS:
        url = base + path
        html = _fetch_html(url, timeout)
        if html is None:
            continue

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
