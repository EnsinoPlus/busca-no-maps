"""Interface web PT-BR do Leads Maps v2."""
import hmac
import json
import logging
import os
import secrets
import sqlite3
import tempfile
import threading
import time
import urllib.parse
from collections import defaultdict, deque
from datetime import date, datetime, timezone

from flask import (
    Flask,
    Response,
    abort,
    g,
    redirect,
    request,
    session,
    stream_with_context,
    url_for,
)
from markupsafe import escape


def _load_env():
    try:
        with open(os.path.join(os.path.dirname(os.path.abspath(__file__)), ".env"), encoding="utf-8") as handle:
            for raw in handle:
                line = raw.strip()
                if line and not line.startswith("#") and "=" in line:
                    key, value = line.split("=", 1)
                    os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))
    except FileNotFoundError:
        pass


_load_env()
import database
import email_finder
import lead_quality
import places_api

app = Flask(__name__)
app.secret_key = os.environ.get("APP_SECRET_KEY") or secrets.token_hex(32)
app.config.update(MAX_CONTENT_LENGTH=64 * 1024, SESSION_COOKIE_HTTPONLY=True, SESSION_COOKIE_SAMESITE="Lax")
app.config["SESSION_COOKIE_SECURE"] = os.environ.get("APP_SECURE_COOKIES") == "1"

AUTH_FAILURE_LIMIT = 5
SEARCH_RATE_LIMIT = 3
RATE_LIMIT_WINDOW = 60
MAX_SYNC_QUERIES = 2  # compatibilidade com POST legado
DEFAULT_EMAIL_LEAD_TARGET = 5
MAX_SEARCH_CANDIDATES = 60
MAX_SEARCH_VARIATIONS = 3
MAX_API_REQUESTS_PER_SEARCH = 30
PAGE_SIZE = 25
_rate_limit_events = defaultdict(deque)
_rate_limit_lock = threading.Lock()
_monotonic = time.monotonic
log = logging.getLogger("leads_maps")
logging.basicConfig(level=logging.INFO, format="%(asctime)s %(levelname)s %(message)s")

CATEGORY_TRAD = {"lawyer": "Advocacia", "accounting": "Contabilidade", "finance": "Financeiro", "health": "Saúde", "doctor": "Médico", "real_estate_agency": "Imobiliária", "restaurant": "Restaurante", "store": "Loja", "establishment": "Estabelecimento"}


def _rate_limit_allows(bucket, client_key, limit, window=RATE_LIMIT_WINDOW):
    now = _monotonic(); key = (bucket, client_key)
    with _rate_limit_lock:
        events = _rate_limit_events[key]; cutoff = now - window
        while events and events[0] <= cutoff: events.popleft()
        if len(events) >= limit: return False
        events.append(now); return True


def _reset_rate_limits():
    with _rate_limit_lock: _rate_limit_events.clear()


def _is_production():
    return os.environ.get("RENDER", "").lower() in {"1", "true", "yes"} or os.environ.get("APP_ENV", "").lower() == "production" or os.environ.get("FLASK_ENV", "").lower() == "production"


@app.before_request
def _require_auth():
    if request.endpoint == "health" or os.environ.get("APP_PUBLIC_ACCESS", "").lower() in {"1", "true", "yes"}: return None
    username, password, secret = os.environ.get("APP_USERNAME"), os.environ.get("APP_PASSWORD"), os.environ.get("APP_SECRET_KEY")
    if _is_production() and (not username or not password or not secret): return Response("Configuração de segurança incompleta.", status=503)
    if not username and not password: return None
    if not username or not password: return Response("Configuração de autenticação incompleta.", status=503)
    auth = request.authorization
    valid = bool(auth and hmac.compare_digest(auth.username or "", username) and hmac.compare_digest(auth.password or "", password))
    if valid: return None
    if not _rate_limit_allows("auth", request.remote_addr or "unknown", AUTH_FAILURE_LIMIT): return Response("Muitas tentativas de autenticação.", status=429, headers={"Retry-After": "60"})
    return Response("Autenticação necessária.", status=401, headers={"WWW-Authenticate": 'Basic realm="Leads Maps"'})


@app.before_request
def _limit_search_posts():
    if request.endpoint == "buscar" and request.method == "POST" and not _rate_limit_allows("buscar", request.remote_addr or "unknown", SEARCH_RATE_LIMIT):
        return Response("Muitas buscas. Tente novamente em instantes.", status=429, headers={"Retry-After": "60"})
    return None


@app.before_request
def _csrf_protect():
    if request.method != "POST": return
    expected = session.get("csrf_token", "")
    received = request.form.get("csrf_token", "") or request.headers.get("X-CSRF-Token", "")
    if not expected or not received or not hmac.compare_digest(expected, received): abort(400, description="Token de segurança inválido ou ausente.")
    return


@app.after_request
def _security_headers(response):
    response.headers["Content-Security-Policy"] = "default-src 'self'; style-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'self'; form-action 'self'; frame-ancestors 'none'"
    response.headers["X-Content-Type-Options"] = "nosniff"
    response.headers["Referrer-Policy"] = "no-referrer"
    response.headers["Permissions-Policy"] = "geolocation=(), microphone=(), camera=()"
    return response


def _csrf_token():
    if "csrf_token" not in session: session["csrf_token"] = secrets.token_urlsafe(32)
    return session["csrf_token"]


def _csrf_input():
    return f'<input type="hidden" name="csrf_token" value="{_csrf_token()}">'


def _get_db():
    if "db" not in g: g.db = database.get_connection()
    return g.db


@app.teardown_appcontext
def _close_db(_error=None):
    conn = g.pop("db", None)
    if conn is not None: conn.close()


@app.get("/health")
def health():
    return {"status": "ok"}


def _checar_api_key():
    return bool(os.environ.get("GOOGLE_MAPS_API_KEY"))


def _traduzir_categoria(types_str):
    parts = [part.strip() for part in (types_str or "").split(",") if part.strip()]
    return ", ".join(CATEGORY_TRAD.get(part, part.replace("_", " ").title()) for part in parts[:3]) or "-"


def _safe_http_url(value):
    try: parsed = urllib.parse.urlsplit(str(value or ""))
    except ValueError: return None
    return str(value) if parsed.scheme.lower() in {"http", "https"} and parsed.hostname else None


def render_page(title, subtitle, body):
    warning = "" if _checar_api_key() else '<div class="warn">GOOGLE_MAPS_API_KEY não configurada. As buscas não funcionarão.</div>'
    return f'''<!doctype html><html lang="pt-BR"><head><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><title>{escape(title)} · Leads Maps</title><link rel="stylesheet" href="/static/app.css"></head><body><header class="topbar"><a class="brand" href="/">Leads Maps <small>{escape(subtitle)}</small></a><nav><a href="/">Buscar</a><a href="/leads">Leads</a><a href="/exportar">Exportar</a><a href="/dashboard">Uso da API</a><a href="/backups">Backups</a></nav></header><main class="wrap">{warning}{body}</main></body></html>'''


def _render_tabela(rows):
    if not rows: return '<div class="empty">Nenhum lead neste grupo.</div>'
    output = []
    for row in rows:
        if len(row) not in (8, 9): raise ValueError("Cada linha deve ter 8 ou 9 colunas.")
        name, address, phone, email, website, category, rating, search_query = row[:8]
        suggested = row[8] if len(row) == 9 else None
        if email: email_html = f'<span class="badge good">{escape(email)}</span>'
        elif suggested: email_html = f'<span class="badge medium">sugerido: {escape(suggested)}</span>'
        else: email_html = '<span class="badge bad">sem e-mail</span>'
        site = _safe_http_url(website)
        site_html = f'<a href="{escape(site)}" target="_blank" rel="noopener noreferrer">site ↗</a>' if site else "-"
        output.append(f"<tr><td><b>{escape(name or '-')}</b></td><td>{escape(address or '-')}</td><td>{escape(phone or '-')}</td><td>{email_html}</td><td>{site_html}</td><td>{escape(_traduzir_categoria(category))}</td><td>{escape(rating or '-')}</td><td>{escape(search_query or '-')}</td></tr>")
    return '<div class="table-scroll"><table><thead><tr><th>Nome</th><th>Endereço</th><th>Telefone</th><th>E-mail</th><th>Site</th><th>Segmento</th><th>Nota</th><th>Busca</th></tr></thead><tbody>' + "".join(output) + "</tbody></table></div>"


@app.get("/")
def home():
    conn = _get_db()
    try:
        database.maybe_daily_backup(conn)
    except (OSError, sqlite3.Error, AttributeError) as error:
        log.warning("Backup diário não pôde ser criado: %s", error)
    with_email = database.count_leads(conn, "com_email"); without_email = database.count_leads(conn, "sem_email")
    body = f'''<section class="hero"><div><span class="eyebrow">Prospecção inteligente</span><h1>Encontre leads reais, sem contar duplicados.</h1><p>A quantidade solicitada conta somente contatos com e-mail. Contatos sem e-mail ficam separados mais abaixo na lista. O alvo considera apenas leads novos ou aprimorados com e-mail real.</p></div><div class="stats">{with_email} contato(s) com e-mail · {without_email} contato(s) sem e-mail, em lista separada</div></section><section class="card search-card"><form id="search-form" method="post" action="/buscar">{_csrf_input()}<div class="grid"><label>Segmento<input name="segment" maxlength="100" placeholder="Ex.: advocacia" required></label><label>Cidade<input name="city" maxlength="100" placeholder="Ex.: Mossoró" required></label><label>UF<input name="uf" maxlength="2" pattern="[A-Za-z]{{2}}" placeholder="RN" required></label><label>Localização específica<input name="location" maxlength="120" placeholder="Bairro, avenida ou região"></label></div><label>Quantidade de novos leads com e-mail<input type="number" name="limit" min="1" value="{DEFAULT_EMAIL_LEAD_TARGET}" required></label><button id="search-button" class="btn" type="submit">Iniciar varredura</button></form><section id="search-panel" class="progress-panel" hidden aria-live="polite"><div id="search-radar" class="radar"><i></i></div><div class="progress-content"><b id="search-phase">Preparando...</b><progress id="search-progress" max="100" value="0"></progress><div class="counters"><span><b id="count-scanned">0</b> analisados</span><span><b id="count-found">0</b> novos e-mails</span><span><b id="count-api">0</b> chamadas API</span></div><button id="search-cancel" class="btn secondary" type="button">Cancelar busca</button><pre id="search-log"></pre></div></section></section><script src="/static/search-v2.js" defer></script>'''
    return render_page("Buscar", "prospecção v2", body)


def _parse_search_form():
    segment = request.form.get("segment", "").strip()
    city = request.form.get("city", "").strip()
    uf = request.form.get("uf", "").strip().upper()
    location = request.form.get("location", "").strip()
    legacy = [q.strip() for q in request.form.get("queries", "").splitlines() if q.strip()]
    if legacy:
        if len(legacy) > MAX_SYNC_QUERIES or any(len(q) > 200 for q in legacy): abort(400, description=f"Informe de 1 a {MAX_SYNC_QUERIES} termos, com até 200 caracteres cada.")
        variations = legacy
    else:
        if not segment or not city or not re_fullmatch_uf(uf) or max(map(len, (segment, city, location)), default=0) > 120: abort(400, description="Informe segmento, cidade e uma UF válida.")
        place = ", ".join(value for value in (location, city, uf) if value)
        variations = [f"{segment} em {place}", f"{segment} {city} {uf}", f"melhores {segment} em {city} {uf}"][:MAX_SEARCH_VARIATIONS]
    raw = request.form.get("limit", "").strip()
    try: target = int(raw) if raw else DEFAULT_EMAIL_LEAD_TARGET
    except ValueError: abort(400, description="O limite precisa ser um número inteiro.")
    if target < 1: abort(400, description="A quantidade deve ser de pelo menos 1 lead.")
    return segment, city, uf, variations, target


def re_fullmatch_uf(value):
    import re
    return bool(re.fullmatch(r"[A-Z]{2}", value))


def _env_float(name, default):
    try: return max(0.0, float(os.environ.get(name, default)))
    except ValueError: return default


def _env_int(name, default, minimum=1, maximum=None):
    try: value = int(os.environ.get(name, default))
    except ValueError: value = default
    value = max(minimum, value)
    return min(value, maximum) if maximum is not None else value


def _search_events(segment, city, uf, variations, target):
    conn = database.get_connection(); seen = set(); new_email = scanned = api_calls = no_email = 0
    hard_cap = _env_int("API_MAX_REQUESTS_PER_SEARCH", MAX_API_REQUESTS_PER_SEARCH, maximum=MAX_API_REQUESTS_PER_SEARCH)
    ceiling = _env_int("API_DAILY_REQUEST_CEILING", 100)

    def reserve_google_request(endpoint):
        nonlocal api_calls
        if api_calls >= hard_cap:
            raise places_api.UsageLimitError("Teto desta busca atingido.")
        rate_name = "GOOGLE_PLACES_TEXT_SEARCH_RATE" if endpoint == "text_search" else "GOOGLE_PLACES_DETAILS_RATE"
        if not database.record_api_call(conn, endpoint, rate=_env_float(rate_name, 0.0), ceiling=ceiling):
            raise places_api.UsageLimitError("Teto diário de requisições atingido.")
        api_calls += 1

    try:
        yield {"phase": "planejando", "message": f"{len(variations)} variação(ões) de busca", "scanned": 0, "new_email_leads": 0, "api_calls": 0, "progress": 2}
        per_variation_limit = MAX_SEARCH_CANDIDATES if len(variations) == 1 else max(1, MAX_SEARCH_CANDIDATES // len(variations))
        for variation in variations:
            if new_email >= target or api_calls >= hard_cap: break
            yield {"phase": "buscando_candidatos", "message": f"Buscando candidatos para: {variation}", "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": min(95, int(new_email / target * 100))}
            try:
                with places_api.usage_recorder(reserve_google_request):
                    candidates = places_api.text_search(variation, max_results=per_variation_limit)
            except places_api.UsageLimitError as error:
                yield {"phase": "limite_api", "message": str(error), "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": 100}; return
            except RuntimeError as error:
                yield {"phase": "erro", "message": str(error), "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": min(95, scanned * 100 // max(1, target * 4))}; continue
            for candidate in candidates:
                if new_email >= target or api_calls >= hard_cap: break
                place_id = candidate.get("place_id")
                if not place_id or place_id in seen: continue
                seen.add(place_id)
                yield {"phase": "consultando_detalhes", "message": f"Consultando detalhes do candidato {scanned + 1}", "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": min(95, int(new_email / target * 100))}
                try:
                    with places_api.usage_recorder(reserve_google_request):
                        details = places_api.place_details(place_id) or {}
                except places_api.UsageLimitError as error:
                    yield {"phase": "limite_api", "message": str(error), "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": 100}; return
                except RuntimeError as error:
                    yield {"phase": "erro_detalhes", "message": f"Falha ao analisar {place_id}: {error}", "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": min(95, scanned * 100 // max(1, target * 4))}
                    continue
                scanned += 1
                website = details.get("website"); phone = details.get("formatted_phone_number")
                if website:
                    database.record_api_call(conn, "website_check")
                    yield {"phase": "verificando_site", "message": f"Verificando o site do candidato {scanned}", "scanned": scanned, "new_email_leads": new_email, "api_calls": api_calls, "progress": min(95, int(new_email / target * 100))}
                email, source, suggested = email_finder.find_email(website) if website else (None, None, None)
                quality = lead_quality.assess_email(email, source, website) if email else {"quality": None, "confidence": None, "domain_aligned": False, "mx_valid": None}
                data = {"place_id": place_id, "name": details.get("name") or candidate.get("name"), "address": details.get("formatted_address") or candidate.get("formatted_address"), "phone": phone, "email": email, "email_fonte": source, "email_sugerido": suggested, "website": website, "category": ", ".join(candidate.get("types", [])[:3]), "rating": details.get("rating") or candidate.get("rating"), "ratings_total": details.get("user_ratings_total") or candidate.get("user_ratings_total"), "latitude": candidate.get("geometry", {}).get("location", {}).get("lat"), "longitude": candidate.get("geometry", {}).get("location", {}).get("lng"), "search_query": variation, "created_at": datetime.now(timezone.utc).isoformat(), "segment": segment, "city": city, "uf": uf, "email_quality": quality["quality"], "email_confidence": quality["confidence"], "email_domain_aligned": int(quality["domain_aligned"]), "email_mx_valid": None if quality["mx_valid"] is None else int(quality["mx_valid"])}
                outcome = database.upsert_lead(conn, data)
                counts = bool(email) and (outcome["is_new"] or outcome["gained_email"])
                if counts: new_email += 1
                elif not email: no_email += 1
                yield {"phase": "analisando", "message": f"{data['name'] or place_id}: {'e-mail confirmado' if counts else 'já existente' if email else 'sem e-mail'}", "scanned": scanned, "new_email_leads": new_email, "without_email": no_email, "api_calls": api_calls, "progress": min(98, int(new_email / target * 100))}
        yield {"phase": "concluida", "message": f"{new_email} de {target} contato(s) com e-mail encontrado(s). {no_email} contato(s) sem e-mail listado(s) separadamente.", "scanned": scanned, "new_email_leads": new_email, "without_email": no_email, "api_calls": api_calls, "progress": 100}
    finally: conn.close()


@app.post("/buscar")
def buscar():
    segment, city, uf, variations, target = _parse_search_form()
    events = _search_events(segment, city, uf, variations, target)
    if "application/x-ndjson" in request.headers.get("Accept", ""):
        @stream_with_context
        def generate():
            for event in events: yield json.dumps(event, ensure_ascii=False) + "\n"
        return Response(generate(), mimetype="application/x-ndjson", headers={"Cache-Control": "no-store", "X-Accel-Buffering": "no"})
    completed = list(events); final = completed[-1]
    log_html = escape("\n".join(event["message"] for event in completed))
    return render_page("Resultado", "busca concluída", f'<section class="card"><h1>Busca concluída</h1><p>{escape(final["message"])}</p><pre>{log_html}</pre><a class="btn" href="/leads">Ver leads salvos</a></section>')


def _filter_args():
    keys = ("email", "quality", "phone", "site", "rating_min", "segment", "city", "uf", "status", "exported", "date_from", "date_to", "followup", "busca")
    filters = {key: request.args.get(key, "").strip() for key in keys if request.args.get(key, "").strip()}
    return _validate_filter_values(filters)


def _validate_filter_values(filters):
    choices = {
        "email": {"com_email", "sem_email"},
        "quality": {"alta", "media", "baixa"},
        "phone": {"sim", "nao"}, "site": {"sim", "nao"},
        "status": set(database.FUNNEL_STATUSES), "exported": {"sim", "nao"},
        "followup": {"atrasado", "agendado"},
    }
    for key, allowed in choices.items():
        if filters.get(key) and filters[key] not in allowed:
            abort(400, description=f"Filtro {key} inválido.")
    if filters.get("rating_min"):
        try:
            rating = float(filters["rating_min"])
        except ValueError:
            abort(400, description="A nota mínima precisa ser um número.")
        if not 0 <= rating <= 5:
            abort(400, description="A nota mínima deve estar entre 0 e 5.")
    if filters.get("uf"):
        filters["uf"] = filters["uf"].upper()
        if not re_fullmatch_uf(filters["uf"]):
            abort(400, description="UF inválida.")
    for key in ("date_from", "date_to"):
        if filters.get(key):
            try: date.fromisoformat(filters[key])
            except ValueError: abort(400, description=f"Data inválida em {key}.")
    return filters


def _select(name, choices, selected):
    options = ['<option value="">Todos</option>']
    options += [f'<option value="{escape(value)}" {"selected" if value == selected else ""}>{escape(label)}</option>' for value, label in choices]
    return f'<select name="{name}">' + "".join(options) + "</select>"


def _crm_table(rows):
    if not rows: return '<div class="empty">Nenhum lead encontrado.</div>'
    items = []
    status_options = [(status, status.replace("_", " ").title()) for status in database.FUNNEL_STATUSES]
    for row in rows:
        site = _safe_http_url(row["website"])
        quality = row["email_quality"] or "-"
        items.append(f'''<tr><td><input type="checkbox" name="ids" value="{escape(row['place_id'])}" form="export-selection"></td><td><b>{escape(row['name'] or '-')}</b><small>{escape(row['city'] or '')}/{escape(row['uf'] or '')}</small></td><td>{escape(row['email'] or 'sem e-mail')}<small>Qualidade: {escape(quality)} · {escape(row['email_confidence'] if row['email_confidence'] is not None else '-')}%</small></td><td>{escape(row['phone'] or '-')} · {f'<a href="{escape(site)}" target="_blank" rel="noopener noreferrer">site</a>' if site else '-'}</td><td><form method="post" action="/leads/{urllib.parse.quote(row['place_id'], safe='')}/editar">{_csrf_input()}{_select('status', status_options, row['status'])}<input name="assignee" value="{escape(row['assignee'] or '')}" placeholder="Responsável"><input name="tags" value="{escape(row['tags'] or '')}" placeholder="Tags"><textarea name="notes" placeholder="Notas">{escape(row['notes'] or '')}</textarea><label>Último contato<input type="datetime-local" name="last_contact_at" value="{escape((row['last_contact_at'] or '')[:16])}"></label><label>Próximo follow-up<input type="datetime-local" name="next_followup_at" value="{escape((row['next_followup_at'] or '')[:16])}"></label><button class="btn small" type="submit">Salvar</button></form></td></tr>''')
    return '<div class="table-scroll"><table><thead><tr><th>Sel.</th><th>Lead</th><th>E-mail</th><th>Contato</th><th>Funil e acompanhamento</th></tr></thead><tbody>' + "".join(items) + "</tbody></table></div>"


@app.get("/leads")
def leads():
    filters = _filter_args()
    # Compatibilidade com o filtro antigo.
    if request.args.get("filtro"): filters["email"] = request.args["filtro"]
    rows = database.query_lead_records(_get_db(), filters)
    email_choices = [("com_email", "Com e-mail"), ("sem_email", "Sem e-mail")]
    quality_choices = [("alta", "Alta"), ("media", "Média"), ("baixa", "Baixa")]
    bool_choices = [("sim", "Sim"), ("nao", "Não")]
    status_choices = [(status, status.replace("_", " ").title()) for status in database.FUNNEL_STATUSES]
    filter_hidden = "".join(f'<input type="hidden" name="{escape(key)}" value="{escape(value)}">' for key, value in filters.items())
    body = f'''<section class="card"><h1>Leads</h1><form class="filters" method="get"><label>E-mail{_select('email', email_choices, filters.get('email'))}</label><label>Qualidade{_select('quality', quality_choices, filters.get('quality'))}</label><label>Telefone{_select('phone', bool_choices, filters.get('phone'))}</label><label>Site{_select('site', bool_choices, filters.get('site'))}</label><label>Nota mínima<input type="number" step="0.1" min="0" max="5" name="rating_min" value="{escape(filters.get('rating_min',''))}"></label><label>Segmento<input name="segment" value="{escape(filters.get('segment',''))}"></label><label>Cidade<input name="city" value="{escape(filters.get('city',''))}"></label><label>UF<input name="uf" maxlength="2" value="{escape(filters.get('uf',''))}"></label><label>Status{_select('status', status_choices, filters.get('status'))}</label><label>Exportado{_select('exported', bool_choices, filters.get('exported'))}</label><label>Desde<input type="date" name="date_from" value="{escape(filters.get('date_from',''))}"></label><label>Até<input type="date" name="date_to" value="{escape(filters.get('date_to',''))}"></label><label>Follow-up{_select('followup', [('atrasado','Atrasado'),('agendado','Agendado')], filters.get('followup'))}</label><label>Busca<input name="busca" value="{escape(filters.get('busca',''))}"></label><button class="btn" type="submit">Filtrar</button></form><form method="post" action="/exportar">{_csrf_input()}<input type="hidden" name="scope" value="filtered"><input type="hidden" name="format" value="xlsx">{filter_hidden}<button class="btn secondary" type="submit">Exportar filtro atual</button></form></section><section class="card"><form id="export-selection" method="post" action="/exportar"><input type="hidden" name="csrf_token" value="{_csrf_token()}"><input type="hidden" name="scope" value="selected"><button class="btn secondary" type="submit" name="format" value="xlsx">Exportar selecionados</button></form><p><b>{len(rows)}</b> resultado(s); os leads com e-mail aparecem primeiro.</p>{_crm_table(rows)}</section>'''
    return render_page("Leads", f"{len(rows)} encontrados", body)


@app.post("/leads/<path:place_id>/editar")
def edit_lead(place_id):
    fields = {key: request.form.get(key, "") for key in ("status", "notes", "assignee", "tags", "last_contact_at", "next_followup_at")}
    try: updated = database.update_lead_crm(_get_db(), place_id, fields)
    except ValueError as error: abort(400, description=str(error))
    if not updated: abort(404)
    return redirect(url_for("leads"))


@app.get("/exportar")
def exportar_pagina():
    body = f'''<section class="card"><h1>Exportar leads</h1><p>CSV seguro ou XLSX profissional. O download GET legado não altera o histórico.</p><form method="post" action="/exportar">{_csrf_input()}<label>Escopo<select name="scope"><option value="filtered">Todos filtrados</option><option value="unexported">Novos ainda não exportados</option></select></label><label>Formato<select name="format"><option value="xlsx">Excel XLSX</option><option value="csv">CSV UTF-8</option></select></label><button class="btn" type="submit">Gerar e marcar como exportado</button></form></section>'''
    return render_page("Exportar", "arquivos seguros", body)


def _export_response(fmt, filters, mark=False):
    conn = _get_db(); rows = database.query_lead_records(conn, filters); suffix = ".xlsx" if fmt == "xlsx" else ".csv"
    with tempfile.NamedTemporaryFile(prefix="leads_export_", suffix=suffix, delete=False) as temporary: path = temporary.name
    try:
        if fmt == "xlsx": database.export_xlsx(conn, path, filters)
        else:
            if set(filters) <= {"filtro", "busca"}:
                database.export_csv(conn, path, filtro=filters.get("filtro"), busca=filters.get("busca"))
            else:
                database.export_csv(conn, path, filters=filters)
        if fmt == "csv":
            with open(path, encoding="utf-8-sig") as handle: content = handle.read()
        else:
            with open(path, "rb") as handle: content = handle.read()
    finally:
        try: os.unlink(path)
        except FileNotFoundError: pass
    if mark: database.mark_exported(conn, [row["place_id"] for row in rows])
    mime = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if fmt == "xlsx" else "text/csv; charset=utf-8"
    return Response(content, mimetype=mime, headers={"Content-Disposition": f"attachment; filename=leads_export{suffix}", "Cache-Control": "no-store"})


@app.post("/exportar")
def exportar_post():
    fmt = request.form.get("format", "xlsx")
    if fmt not in {"csv", "xlsx"}: abort(400)
    scope = request.form.get("scope", "filtered")
    filters = {key: request.form.get(key, "").strip() for key in ("email", "quality", "phone", "site", "rating_min", "segment", "city", "uf", "status", "exported", "date_from", "date_to", "followup", "busca") if request.form.get(key, "").strip()}
    _validate_filter_values(filters)
    if scope == "selected":
        ids = request.form.getlist("ids")
        if not ids: abort(400, description="Selecione ao menos um lead.")
        filters["ids"] = ids
    elif scope == "unexported": filters["exported"] = "nao"
    elif scope != "filtered": abort(400)
    return _export_response(fmt, filters, mark=True)


@app.get("/exportar.csv")
def exportar_csv():
    filtro = request.args.get("filtro", "todos")
    if filtro not in {"todos", "com_email", "sem_email"}: filtro = "todos"
    return _export_response("csv", {"filtro": filtro, "busca": request.args.get("busca", "")}, mark=False)


@app.get("/dashboard")
def dashboard():
    usage = database.api_usage_summary(_get_db()); ceiling = _env_int("API_DAILY_REQUEST_CEILING", 100)
    rows = "".join(f"<tr><td>{escape(row['endpoint'])}</td><td>{row['requests']}</td><td>{row['units']}</td><td>US$ {row['estimated_cost']:.4f} (estimado)</td></tr>" for row in usage["rows"])
    body = f'''<section class="card"><h1>Uso diário da API</h1><div class="stats"><b>{usage['api_requests']}</b> / {ceiling} requisições Google · {usage['requests']} operações externas registradas · US$ {usage['estimated_cost']:.4f} estimado</div><progress max="{ceiling}" value="{usage['api_requests']}"></progress><p>Custos são estimativas calculadas com as tarifas configuradas; confira a cobrança real no Google Cloud.</p><table><thead><tr><th>Endpoint</th><th>Requisições</th><th>Unidades</th><th>Custo</th></tr></thead><tbody>{rows}</tbody></table></section>'''
    return render_page("Uso da API", usage["day"], body)


@app.route("/backups", methods=["GET", "POST"])
def backups():
    message = ""
    if request.method == "POST":
        path = database.create_backup(_get_db(), retention=30); message = f'<div class="success">Backup criado: {escape(os.path.basename(path))}</div>'
    files = database.list_backups()
    rows = "".join(f"<tr><td>{escape(item['filename'])}</td><td>{item['size_bytes']:,} bytes</td></tr>" for item in files)
    body = f'''{message}<section class="card"><h1>Backups do banco</h1><p>Cópias SQLite online são armazenadas somente na pasta <code>backups/</code>, com retenção das 30 mais recentes.</p><form method="post">{_csrf_input()}<button class="btn" type="submit">Criar backup agora</button></form><table><thead><tr><th>Arquivo</th><th>Tamanho</th></tr></thead><tbody>{rows}</tbody></table></section>'''
    return render_page("Backups", "retenção 30", body)


@app.route("/limpar", methods=["GET", "POST"])
def limpar():
    if request.method == "POST" and request.form.get("confirm") == "LIMPAR": database.apagar_todos(_get_db()); return redirect(url_for("leads"))
    return render_page("Limpar", "ação irreversível", f'<section class="card"><h1>Limpar banco</h1><form method="post">{_csrf_input()}<label>Digite LIMPAR<input name="confirm"></label><button class="btn danger">Apagar tudo</button></form></section>')


if __name__ == "__main__":
    app.run(debug=False, port=5000)
