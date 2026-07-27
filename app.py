"""
Interface web local para o Leads Maps.

Como rodar:
    pip install flask
    set GOOGLE_MAPS_API_KEY=sua_chave_aqui   (Windows)
    python app.py

Depois abra no navegador: http://127.0.0.1:5000
"""
import hmac
import logging
import os
import secrets
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import datetime, timezone

from flask import (
    Flask,
    Response,
    abort,
    g,
    jsonify,
    redirect,
    request,
    session,
    url_for,
)
from markupsafe import escape


def _load_env():
    """Carrega variáveis do arquivo .env (sem dependências externas)."""
    try:
        path = os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env")
        with open(path, encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line or line.startswith("#") or "=" not in line:
                    continue
                key, value = line.split("=", 1)
                key, value = key.strip(), value.strip().strip('"').strip("'")
                os.environ.setdefault(key, value)
    except FileNotFoundError:
        pass


_load_env()


import database
import email_finder
import places_api

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.config["MAX_CONTENT_LENGTH"] = 64 * 1024
app.config["SESSION_COOKIE_HTTPONLY"] = True
app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("APP_SECURE_COOKIES") == "1"

AUTH_FAILURE_LIMIT = 5
SEARCH_RATE_LIMIT = 3
RATE_LIMIT_WINDOW = 60
_rate_limit_events = defaultdict(deque)
_rate_limit_lock = threading.Lock()
_monotonic = time.monotonic


def _rate_limit_allows(bucket, client_key, limit, window=RATE_LIMIT_WINDOW):
    now = _monotonic()
    key = (bucket, client_key)
    with _rate_limit_lock:
        events = _rate_limit_events[key]
        cutoff = now - window
        while events and events[0] <= cutoff:
            events.popleft()
        if len(events) >= limit:
            return False
        events.append(now)
        return True


def _reset_rate_limits():
    """Limpa o limitador em memória (também torna os testes determinísticos)."""
    with _rate_limit_lock:
        _rate_limit_events.clear()


def _is_production():
    return (
        os.environ.get("RENDER", "").lower() in {"1", "true", "yes"}
        or os.environ.get("APP_ENV", "").lower() == "production"
        or os.environ.get("FLASK_ENV", "").lower() == "production"
    )


@app.before_request
def _require_auth():
    """Protege produção e mantém o uso local sem autenticação opcional."""
    if request.endpoint == "health":
        return None
    username = os.environ.get("APP_USERNAME")
    password = os.environ.get("APP_PASSWORD")
    secret_key = os.environ.get("APP_SECRET_KEY")
    if _is_production() and (not username or not password or not secret_key):
        return Response("Configuração de segurança incompleta.", status=503)
    if not username and not password:
        return None
    if not username or not password:
        return Response("Configuração de autenticação incompleta.", status=503)
    auth = request.authorization
    valid = bool(
        auth
        and hmac.compare_digest(auth.username or "", username)
        and hmac.compare_digest(auth.password or "", password)
    )
    if not valid:
        if not _rate_limit_allows("auth", request.remote_addr or "unknown", AUTH_FAILURE_LIMIT):
            return Response(
                "Muitas tentativas de autenticação.",
                status=429,
                headers={"Retry-After": "60"},
            )
        return Response(
            "Autenticação necessária.",
            status=401,
            headers={"WWW-Authenticate": 'Basic realm="Leads Maps"'},
        )
    return None


@app.before_request
def _limit_search_posts():
    if (
        request.endpoint == "buscar"
        and request.method == "POST"
        and not _rate_limit_allows("buscar", request.remote_addr or "unknown", SEARCH_RATE_LIMIT)
    ):
        return Response(
            "Muitas buscas. Tente novamente em instantes.",
            status=429,
            headers={"Retry-After": "60"},
        )
    return None


@app.before_request
def _csrf_protect():
    if request.method != "POST":
        return
    expected = session.get("csrf_token", "")
    received = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not received or not hmac.compare_digest(expected, received):
        abort(400, description="Token de segurança inválido ou ausente.")
    return


def _csrf_token():
    token = session.get("csrf_token")
    if not token:
        token = secrets.token_urlsafe(32)
        session["csrf_token"] = token
    return token


def _csrf_input():
    return f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">'


@app.after_request
def _security_headers(response):
    response.headers["Content-Security-Policy"] = (
        "default-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' data:; "
        "object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    )
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


@app.get("/health")
def health():
    return jsonify(status="ok")


def _get_db():
    if "db" not in g:
        g.db = database.get_connection()
    return g.db


@app.teardown_appcontext
def _close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None:
        conn.close()


# Log em arquivo + console
logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    handlers=[
        logging.FileHandler(os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.log"), encoding="utf-8"),
        logging.StreamHandler(),
    ],
)
log = logging.getLogger("leads_maps")

PAGE_SIZE = 10
MAX_SYNC_QUERIES = 2
MAX_SYNC_RESULTS_PER_QUERY = 5

CATEGORY_TRAD = {
    "lawyer": "Advocacia",
    "establishment": "Estabelecimento",
    "point_of_interest": "Ponto de interesse",
    "store": "Loja",
    "restaurant": "Restaurante",
    "accounting": "Contabilidade",
    "finance": "Financeiro",
    "health": "Saúde",
    "doctor": "Médico",
    "real_estate_agency": "Imobiliária",
    "travel_agency": "Agência de viagem",
    "insurance_agency": "Seguros",
    "local_government_office": "Órgão público",
}


def _traduzir_categoria(types_str):
    if not types_str:
        return "-"
    parts = [t.strip() for t in types_str.split(",") if t.strip()]
    trads = []
    for p in parts[:3]:
        trads.append(CATEGORY_TRAD.get(p, p.replace("_", " ").title()))
    return ", ".join(trads)


def _checar_api_key():
    if not os.environ.get("GOOGLE_MAPS_API_KEY"):
        log.warning("GOOGLE_MAPS_API_KEY nao configurada!")
        return False
    return True


PAGE_STYLE = """
<style>
  :root {
    --bg: #0b0d12;
    --bg-soft: #11141b;
    --card: #151922;
    --card-hover: #1b2030;
    --border: #232a39;
    --border-soft: #1c2230;
    --text: #e6e9ef;
    --text-muted: #8b94a7;
    --primary: #6366f1;
    --primary-2: #8b5cf6;
    --primary-soft: rgba(99,102,241,0.14);
    --success: #34d399;
    --success-bg: rgba(52,211,153,0.12);
    --danger: #f87171;
    --danger-bg: rgba(248,113,113,0.12);
    --shadow: 0 10px 30px rgba(0,0,0,0.35);
    --radius: 16px;
  }
  * { box-sizing: border-box; }
  html, body { margin: 0; padding: 0; }
  body {
    font-family: 'Inter', -apple-system, BlinkMacSystemFont, 'Segoe UI', Roboto, Arial, sans-serif;
    background:
      radial-gradient(1200px 600px at 80% -10%, rgba(139,92,246,0.10), transparent 60%),
      radial-gradient(900px 500px at -10% 10%, rgba(99,102,241,0.10), transparent 55%),
      var(--bg);
    color: var(--text);
    min-height: 100vh;
    -webkit-font-smoothing: antialiased;
  }
  .topbar {
    display: flex; align-items: center; justify-content: space-between;
    gap: 16px;
    padding: 22px clamp(20px, 5vw, 56px);
    border-bottom: 1px solid var(--border-soft);
    background: rgba(11,13,18,0.7);
    backdrop-filter: blur(10px);
    position: sticky; top: 0; z-index: 10;
  }
  .brand { display: flex; align-items: center; gap: 12px; }
  .brand .logo {
    width: 38px; height: 38px; border-radius: 11px;
    background: linear-gradient(135deg, var(--primary), var(--primary-2));
    display: grid; place-items: center; font-size: 19px;
    box-shadow: 0 6px 18px rgba(99,102,241,0.45);
  }
  .brand h1 { margin: 0; font-size: 17px; font-weight: 700; letter-spacing: -0.01em; }
  .brand p { margin: 2px 0 0; font-size: 12.5px; color: var(--text-muted); }
  .nav { display: flex; gap: 10px; flex-wrap: wrap; }
  .nav a {
    color: var(--text-muted); text-decoration: none; font-size: 13px; font-weight: 600;
    padding: 9px 14px; border-radius: 10px; border: 1px solid var(--border-soft);
    transition: all .15s ease;
  }
  .nav a:hover { color: var(--text); border-color: var(--border); background: var(--card); }
  .wrap { max-width: 1120px; margin: 0 auto; padding: 32px clamp(20px, 5vw, 56px) 64px; }
  .card {
    background: linear-gradient(180deg, var(--card), var(--bg-soft));
    border: 1px solid var(--border);
    border-radius: var(--radius);
    padding: 26px;
    box-shadow: var(--shadow);
    margin-bottom: 22px;
  }
  .stat {
    display: inline-flex; align-items: center; gap: 9px;
    background: var(--primary-soft); color: #c7c9ff;
    padding: 8px 15px; border-radius: 999px; font-size: 13px; font-weight: 600;
    border: 1px solid rgba(99,102,241,0.25);
  }
  .section-title { font-size: 14px; font-weight: 700; margin: 0 0 14px; color: var(--text); }
  label { display: block; font-weight: 600; font-size: 13px; margin: 18px 0 7px; color: var(--text-muted); }
  textarea, input[type=number], input[type=text], select {
    width: 100%; padding: 13px 15px;
    border: 1px solid var(--border); border-radius: 12px;
    font-size: 14px; font-family: inherit; color: var(--text);
    background: var(--bg); transition: border-color .15s, box-shadow .15s;
    resize: vertical;
  }
  textarea::placeholder, input::placeholder { color: #5b6478; }
  textarea:focus, input:focus, select:focus {
    outline: none; border-color: var(--primary);
    box-shadow: 0 0 0 3px var(--primary-soft); background: var(--bg-soft);
  }
  .check {
    display: flex; align-items: center; gap: 10px; margin-top: 18px;
    font-size: 13.5px; color: var(--text); cursor: pointer; user-select: none;
  }
  .check input { width: 18px; height: 18px; accent-color: var(--primary); cursor: pointer; }
  .btn {
    display: inline-flex; align-items: center; gap: 8px;
    background: linear-gradient(135deg, var(--primary), var(--primary-2));
    color: white; border: none; padding: 12px 22px;
    border-radius: 12px; font-size: 14px; font-weight: 600;
    cursor: pointer; text-decoration: none; margin-top: 20px;
    box-shadow: 0 8px 20px rgba(99,102,241,0.35);
    transition: transform .12s ease, box-shadow .15s ease, filter .15s ease;
  }
  .btn:hover { transform: translateY(-1px); filter: brightness(1.06); box-shadow: 0 12px 26px rgba(99,102,241,0.45); }
  .btn:active { transform: translateY(0) scale(0.99); }
  .btn-secondary {
    background: var(--card); color: var(--text);
    border: 1px solid var(--border); box-shadow: none;
  }
  .btn-secondary:hover { background: var(--card-hover); filter: none; }
  .btn-success {
    background: linear-gradient(135deg, #10b981, #34d399);
    box-shadow: 0 8px 20px rgba(16,185,129,0.3);
  }
  .btn-danger {
    background: linear-gradient(135deg, #ef4444, #f87171);
    box-shadow: 0 8px 20px rgba(239,68,68,0.3);
  }
  .btn-row { display: flex; gap: 12px; flex-wrap: wrap; }
  table {
    width: 100%; border-collapse: separate; border-spacing: 0;
    font-size: 13px; overflow: hidden; border-radius: 12px;
    border: 1px solid var(--border);
  }
  th {
    background: var(--bg-soft); color: var(--text-muted);
    text-transform: uppercase; font-size: 10.5px; font-weight: 700;
    letter-spacing: 0.06em; text-align: left; padding: 13px 14px;
    border-bottom: 1px solid var(--border);
  }
  td {
    padding: 13px 14px; border-bottom: 1px solid var(--border-soft);
    vertical-align: top; color: var(--text);
  }
  tbody tr { transition: background .12s ease; }
  tbody tr:hover td { background: var(--card-hover); }
  tbody tr:last-child td { border-bottom: none; }
  .badge {
    display: inline-block; padding: 4px 11px; border-radius: 999px;
    font-size: 12px; font-weight: 600; max-width: 240px;
    overflow: hidden; text-overflow: ellipsis; white-space: nowrap; vertical-align: middle;
  }
  .badge-yes { background: var(--success-bg); color: var(--success); }
  .badge-no { background: var(--danger-bg); color: var(--danger); }
  .badge-sug { background: rgba(250,204,21,0.12); color: #facc15; }
  .site-link { color: #a5b4fc; text-decoration: none; font-weight: 600; }
  .site-link:hover { text-decoration: underline; }
  .log-box {
    background: #06080d; color: #cdd3e0;
    padding: 18px 20px; border-radius: 12px;
    border: 1px solid var(--border-soft);
    font-family: 'JetBrains Mono', 'Consolas', monospace;
    font-size: 12.5px; line-height: 1.75; white-space: pre-wrap;
  }
  .empty {
    text-align: center; padding: 48px 20px; color: var(--text-muted);
    border: 1px dashed var(--border); border-radius: 12px;
  }
  .empty strong { color: var(--text); }
  .pager { display: flex; gap: 10px; align-items: center; justify-content: center; margin-top: 14px; flex-wrap: wrap; }
  .pager a, .pager span { padding: 8px 14px; border-radius: 10px; border: 1px solid var(--border); color: var(--text-muted); text-decoration: none; font-size: 13px; }
  .pager a:hover { color: var(--text); background: var(--card); }
  .pager .current { color: var(--text); background: var(--card); border-color: var(--primary); }
  .warn {
    background: var(--danger-bg); color: var(--danger); border: 1px solid rgba(248,113,113,0.3);
    padding: 12px 16px; border-radius: 12px; font-size: 13px; margin-bottom: 18px;
  }
  @media (max-width: 640px) {
    .topbar { flex-direction: column; align-items: flex-start; }
    .table-scroll { overflow-x: auto; }
  }
</style>
"""


def render_page(title, subtitle, body):
    key_ok = bool(os.environ.get("GOOGLE_MAPS_API_KEY"))
    warn = "" if key_ok else '<div class="warn">⚠️ GOOGLE_MAPS_API_KEY não configurada. As buscas vão falhar. Ajuste o arquivo .env.</div>'
    return f"""
    <!DOCTYPE html>
    <html lang="pt-br">
    <head>
      <meta charset="UTF-8">
      <meta name="viewport" content="width=device-width, initial-scale=1">
      <title>Leads Maps</title>
      {PAGE_STYLE}
    </head>
    <body>
      <header class="topbar">
        <div class="brand">
          <div class="logo">🔎</div>
          <div>
            <h1>Leads Maps</h1>
            <p>{escape(subtitle)}</p>
          </div>
        </div>
        <nav class="nav">
          <a href="/">Buscar</a>
          <a href="/leads">Leads</a>
          <a href="/exportar">Exportar</a>
        </nav>
      </header>
      <div class="wrap">{warn}{body}</div>
    </body>
    </html>
    """


def _safe_http_url(value):
    if not value:
        return None
    try:
        parsed = urllib.parse.urlsplit(str(value))
    except ValueError:
        return None
    if parsed.scheme.lower() not in {"http", "https"} or not parsed.hostname:
        return None
    return str(value)


def _render_tabela(rows):
    if not rows:
        return '<div class="empty">Nenhum lead neste grupo.</div>'
    table_rows = ""
    for row in rows:
        if len(row) not in (8, 9):
            raise ValueError("Cada linha deve ter 8 ou 9 colunas.")
        name, address, phone, email, website, category, rating, search_query = row[:8]
        email_sugerido = row[8] if len(row) == 9 else None
        if email:
            email_badge = f'<span class="badge badge-yes">{escape(email)}</span>'
        elif email_sugerido:
            email_badge = f'<span class="badge badge-sug">sugerido: {escape(email_sugerido)}</span>'
        else:
            email_badge = '<span class="badge badge-no">sem e-mail</span>'
        website_safe = _safe_http_url(website)
        website_link = (
            f'<a class="site-link" href="{escape(website_safe)}" target="_blank" '
            'rel="noopener noreferrer">site ↗</a>'
            if website_safe else "-"
        )
        cat = _traduzir_categoria(category)
        table_rows += f"""
        <tr>
          <td><b>{escape(name or '-')}</b></td>
          <td>{escape(address or '-')}</td>
          <td>{escape(phone or '-')}</td>
          <td>{email_badge}</td>
          <td>{website_link}</td>
          <td>{escape(cat)}</td>
          <td>{escape(rating or '-')}</td>
          <td>{escape(search_query or '-')}</td>
        </tr>
        """
    return f"""
    <div class="table-scroll">
    <table>
      <thead><tr><th>Nome</th><th>Endereço</th><th>Telefone</th><th>E-mail</th><th>Site</th><th>Categoria</th><th>Nota</th><th>Busca</th></tr></thead>
      <tbody>{table_rows}</tbody>
    </table>
    </div>
    """


def _paginar(rows, pagina, size=PAGE_SIZE):
    total_pages = max(1, (len(rows) + size - 1) // size)
    pagina = max(1, min(pagina, total_pages))
    start = (pagina - 1) * size
    return rows[start:start + size], pagina, total_pages


def _pager_html(pagina, total_pages, base_args):
    if total_pages <= 1:
        return ""
    parts = []
    if pagina > 1:
        parts.append(f'<a href="?{base_args}&pagina={pagina-1}">← Anterior</a>')
    parts.append(f'<span class="current">Página {pagina} de {total_pages}</span>')
    if pagina < total_pages:
        parts.append(f'<a href="?{base_args}&pagina={pagina+1}">Próxima →</a>')
    return f'<div class="pager">{"".join(parts)}</div>'


@app.route("/")
def home():
    conn = _get_db()
    total = database.count_leads(conn)
    body = f"""
    <div class="card">
      <div class="stat">📊 {total} lead(s) salvos até agora</div>
      <p style="color:var(--text-muted); font-size:13px; margin-top:16px;">
        Encontre negócios no Google Maps e capture telefone, site e e-mail de contato.
      </p>
      <form method="POST" action="/buscar">
        {_csrf_input()}
        <label>Termos de busca (um por linha)</label>
        <textarea name="queries" rows="4" placeholder="advogado trabalhista Mossoro RN&#10;contador Mossoro RN" required></textarea>

        <label>Limite de resultados por termo (opcional)</label>
        <input type="number" name="limit" min="1" max="{MAX_SYNC_RESULTS_PER_QUERY}" placeholder="ex: 5">

        <label class="check">
          <input type="checkbox" name="somente_com_email" checked>
          Só salvar negócios em que encontrou e-mail (pula os sem e-mail)
        </label>

        <button class="btn" type="submit">🔎 Buscar leads</button>
      </form>
    </div>

    <div class="btn-row">
      <a class="btn btn-secondary" href="/leads">📋 Ver leads salvos</a>
    </div>
    """
    return render_page("Leads Maps", "Prospecção via Google Maps", body)


@app.route("/buscar", methods=["POST"])
def buscar():
    queries = [q.strip() for q in request.form.get("queries", "").splitlines() if q.strip()]
    limit_raw = request.form.get("limit", "").strip()
    if not queries or len(queries) > MAX_SYNC_QUERIES or any(len(query) > 200 for query in queries):
        abort(400, description=f"Informe de 1 a {MAX_SYNC_QUERIES} termos, com até 200 caracteres cada.")
    try:
        limit = int(limit_raw) if limit_raw else MAX_SYNC_RESULTS_PER_QUERY
    except ValueError:
        abort(400, description="O limite precisa ser um número inteiro.")
    if not 1 <= limit <= MAX_SYNC_RESULTS_PER_QUERY:
        abort(400, description=f"O limite deve estar entre 1 e {MAX_SYNC_RESULTS_PER_QUERY}.")
    somente_com_email = request.form.get("somente_com_email") == "on"

    conn = _get_db()
    log_lines = []
    _checar_api_key()

    for query in queries:
        log_lines.append(f"🔎 Buscando: {query}")
        try:
            results = places_api.text_search(query, max_results=limit)
        except RuntimeError as e:
            log_lines.append(f"  ⚠ Erro: {e}")
            log.error("Erro na busca '%s': %s", query, e)
            continue

        log_lines.append(f"  {len(results)} resultado(s) encontrado(s).")

        salvos = 0
        pulados = 0
        for r in results:
            place_id = r.get("place_id")
            if not place_id:
                continue

            details = places_api.place_details(place_id)
            website = details.get("website")
            phone = details.get("formatted_phone_number")
            email, email_fonte, email_sugerido = email_finder.find_email(website) if website else (None, None, None)

            if somente_com_email and not email:
                pulados += 1
                log_lines.append(f"    ⏭ {details.get('name') or r.get('name')}  (sem e-mail, pulado)")
                continue

            lead = {
                "place_id": place_id,
                "name": details.get("name") or r.get("name"),
                "address": details.get("formatted_address") or r.get("formatted_address"),
                "phone": phone,
                "email": email,
                "email_fonte": email_fonte,
                "email_sugerido": email_sugerido,
                "website": website,
                "category": ", ".join(r.get("types", [])[:3]),
                "rating": details.get("rating") or r.get("rating"),
                "ratings_total": details.get("user_ratings_total") or r.get("user_ratings_total"),
                "latitude": r.get("geometry", {}).get("location", {}).get("lat"),
                "longitude": r.get("geometry", {}).get("location", {}).get("lng"),
                "search_query": query,
                "created_at": datetime.now(timezone.utc).isoformat(),
            }
            database.upsert_lead(conn, lead)
            salvos += 1
            log_lines.append(f"    ✔ {lead['name']}  |  📞 {phone or '-'}  |  ✉️ {email or email_sugerido or '-'}")

        if somente_com_email:
            log_lines.append(f"  → {salvos} salvo(s), {pulados} pulado(s) por falta de e-mail.")

    log_html = escape("\n".join(log_lines))
    body = f"""
    <div class="card">
      <h3 class="section-title">Log da busca</h3>
      <div class="log-box">{log_html}</div>
    </div>
    <div class="btn-row">
      <a class="btn" href="/leads">📋 Ver leads salvos</a>
      <a class="btn btn-secondary" href="/">🔎 Nova busca</a>
    </div>
    """
    return render_page("Resultado", "Busca concluída", body)


@app.route("/leads")
def leads():
    conn = _get_db()
    filtro = request.args.get("filtro", "todos")
    busca = request.args.get("busca", "").strip()
    try:
        pagina = int(request.args.get("pagina", "1"))
    except ValueError:
        pagina = 1

    com = database.query_leads(conn, filtro="com_email", busca=busca)
    sem = database.query_leads(conn, filtro="sem_email", busca=busca)
    total = len(com) + len(sem)

    # aplica filtro de e-mail na visualizacao
    if filtro == "com_email":
        sem = []
    elif filtro == "sem_email":
        com = []

    com_p, pg_c, tot_c = _paginar(com, pagina)
    sem_p, pg_s, tot_s = _paginar(sem, pagina)

    base_args = urllib.parse.urlencode({"filtro": filtro, "busca": busca})
    body = f"""
    <div class="btn-row" style="margin-bottom:22px;">
      <a class="btn btn-secondary" href="/">🔎 Nova busca</a>
      <a class="btn btn-success" href="/exportar">⬇️ Exportar CSV (com filtros)</a>
      <a class="btn btn-danger" href="/limpar">🗑️ Limpar banco</a>
    </div>

    <div class="card">
      <form method="GET" action="/leads" style="display:flex; gap:12px; flex-wrap:wrap; align-items:flex-end;">
        <div style="flex:1; min-width:200px;">
          <label style="margin-top:0;">Filtrar por e-mail</label>
          <select name="filtro">
            <option value="todos" {'selected' if filtro=='todos' else ''}>Todos</option>
            <option value="com_email" {'selected' if filtro=='com_email' else ''}>Só com e-mail</option>
            <option value="sem_email" {'selected' if filtro=='sem_email' else ''}>Só sem e-mail</option>
          </select>
        </div>
        <div style="flex:2; min-width:240px;">
          <label style="margin-top:0;">Buscar por termo</label>
          <input type="text" name="busca" value="{escape(busca)}" placeholder="ex: Mossoró, Fortaleza...">
        </div>
        <button class="btn" type="submit" style="margin-top:0;">🔍 Filtrar</button>
      </form>
    </div>
    """

    if com:
        body += f"""
        <div class="card">
          <h3 class="section-title">✉️ Com e-mail <span class="stat" style="margin-left:8px;">{len(com)}</span></h3>
          {_render_tabela(com_p)}
          {_pager_html(pg_c, tot_c, base_args)}
        </div>
        """
    if sem:
        body += f"""
        <div class="card">
          <h3 class="section-title">🚫 Sem e-mail <span class="stat" style="margin-left:8px;">{len(sem)}</span></h3>
          {_render_tabela(sem_p)}
          {_pager_html(pg_s, tot_s, base_args)}
        </div>
        """
    if not com and not sem:
        body += '<div class="empty">Nenhum lead encontrado com esses filtros. <strong>Faça uma busca</strong> pra começar.</div>'

    return render_page("Leads salvos", f"{total} lead(s) no total", body)


@app.route("/exportar")
def exportar_pagina():
    conn = _get_db()
    total = database.count_leads(conn)
    com = len(database.query_leads(conn, filtro="com_email"))
    sem = len(database.query_leads(conn, filtro="sem_email"))
    body = f"""
    <div class="card">
      <h3 class="section-title">⬇️ Exportar leads para CSV</h3>
      <p style="color:var(--text-muted); font-size:13px; margin-top:0;">
        {total} lead(s) no total — {com} com e-mail, {sem} sem e-mail.
      </p>
      <form method="GET" action="/exportar.csv">
        <label>Filtrar por e-mail</label>
        <select name="filtro">
          <option value="todos">Todos os leads</option>
          <option value="com_email">Só com e-mail</option>
          <option value="sem_email">Só sem e-mail</option>
        </select>

        <label>Filtrar por termo de busca (opcional)</label>
        <input type="text" name="busca" placeholder="ex: Mossoró, Fortaleza, advogado...">

        <button class="btn" type="submit">⬇️ Baixar CSV filtrado</button>
      </form>
    </div>
    <div class="btn-row">
      <a class="btn btn-secondary" href="/leads">📋 Ver leads salvos</a>
      <a class="btn btn-secondary" href="/">🔎 Nova busca</a>
    </div>
    """
    return render_page("Exportar CSV", "Exporte com filtros", body)


@app.route("/exportar.csv")
def exportar_csv():
    filtro = request.args.get("filtro", "todos")
    busca = request.args.get("busca", "").strip()
    if filtro not in ("todos", "com_email", "sem_email"):
        filtro = "todos"

    conn = _get_db()
    with tempfile.NamedTemporaryFile(prefix="leads_export_", suffix=".csv", delete=False) as temp_file:
        path = temp_file.name
    try:
        database.export_csv(conn, filepath=path, filtro=filtro or None, busca=busca or None)
        with open(path, "r", encoding="utf-8") as f:
            content = f.read()
    finally:
        try:
            os.unlink(path)
        except FileNotFoundError:
            pass
    return Response(
        content,
        mimetype="text/csv",
        headers={"Content-Disposition": f"attachment; filename=leads_export_{filtro}.csv"},
    )


@app.route("/limpar", methods=["GET", "POST"])
def limpar():
    if request.method == "POST":
        confirm = request.form.get("confirm", "")
        if confirm == "LIMPAR":
            conn = _get_db()
            database.apagar_todos(conn)
            log.warning("Banco de leads apagado pelo usuario.")
            return redirect(url_for("leads"))
        body = '<div class="warn">Confirmação incorreta. Digite LIMPAR no campo abaixo.</div>' + _limpar_form()
        return render_page("Limpar banco", "Confirme", body)
    return render_page("Limpar banco", "Confirme a ação", _limpar_form())


def _limpar_form():
    return f"""
    <div class="card">
      <h3 class="section-title">🗑️ Apagar todos os leads</h3>
      <p style="color:var(--text-muted); font-size:13px;">
        Esta ação remove permanentemente todos os leads do banco local (leads.db).
        Não pode ser desfeita.
      </p>
      <form method="POST" action="/limpar">
        {_csrf_input()}
        <label>Digite <b>LIMPAR</b> para confirmar</label>
        <input type="text" name="confirm" placeholder="LIMPAR">
        <button class="btn btn-danger" type="submit">Apagar tudo</button>
      </form>
    </div>
    <div class="btn-row">
      <a class="btn btn-secondary" href="/leads">← Voltar</a>
    </div>
    """


if __name__ == "__main__":
    if not _checar_api_key():
        print("\n⚠️  AVISO: GOOGLE_MAPS_API_KEY não configurada. As buscas vão falhar.\n")
    print("\n🚀 Abra no navegador: http://127.0.0.1:5000\n")
    app.run(debug=False, port=5000)
