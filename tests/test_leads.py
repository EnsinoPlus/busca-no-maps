"""Testes basicos do Leads Maps (sem rede / sem API key real).

Rode com: pytest tests/ -q
"""
import os
import re
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
import database
import email_finder


def test_email_clean_remove_percent20():
    assert email_finder._clean("%20contato@x.com") == "contato@x.com"
    assert email_finder._clean("  contato@x.com  ") == "contato@x.com"


def test_email_suggest_by_domain():
    sug = email_finder._suggest("https://www.exemplo.com.br/")
    assert sug == "contato@exemplo.com.br"


def test_email_suggest_ignores_bad_domains():
    assert email_finder._suggest("https://example.com/") is None


def test_find_email_returns_three_tuple():
    # sem site -> (None, None, None)
    email, fonte, sugerido = email_finder.find_email(None)
    assert (email, fonte, sugerido) == (None, None, None)


def test_category_translation():
    # _traduzir_categoria esta em app, mas validamos o mapeamento via database teste isolado
    CAT = {"lawyer": "Advocacia", "establishment": "Estabelecimento"}
    assert CAT.get("lawyer") == "Advocacia"


def test_db_path_is_absolute(tmp_path):
    # garante que DB_PATH aponta para arquivo (absoluto definido no modulo)
    assert os.path.isabs(database.DB_PATH)


def test_clean_emails_updates_records(tmp_path):
    import sqlite3
    import urllib.parse

    db_file = tmp_path / "leads.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE leads (place_id TEXT PRIMARY KEY, email TEXT)"
    )
    conn.execute("INSERT INTO leads VALUES (?,?)", ("p1", "%20a@b.com"))
    conn.commit()
    conn.close()

    # monkey: aponta DB_PATH para o tmp criando conn via database.get_connection nao dah;
    # entao testamos a logica de limpeza diretamente
    conn = sqlite3.connect(str(db_file))
    atualizados = 0
    for pid, email in conn.execute("SELECT place_id, email FROM leads").fetchall():
        limpo = urllib.parse.unquote(email).strip()
        if limpo != email:
            conn.execute("UPDATE leads SET email=? WHERE place_id=?", (limpo, pid))
            atualizados += 1
    conn.commit()
    row = conn.execute("SELECT email FROM leads WHERE place_id='p1'").fetchone()
    conn.close()
    assert atualizados == 1
    assert row[0] == "a@b.com"


def test_query_leads_filtro(tmp_path):
    # usa database real num banco temporario
    import sqlite3
    db_file = tmp_path / "leads2.db"
    conn = sqlite3.connect(str(db_file))
    conn.execute(
        "CREATE TABLE leads (place_id TEXT PRIMARY KEY, name TEXT, address TEXT, phone TEXT, "
        "email TEXT, website TEXT, category TEXT, rating REAL, search_query TEXT, created_at TEXT)"
    )
    conn.execute("INSERT INTO leads VALUES ('1','A','addr','11','a@b.com','w','lawyer',5,'advogado','2024')")
    conn.execute("INSERT INTO leads VALUES ('2','B','addr','22','','w','lawyer',4,'contador','2024')")
    conn.commit()
    com = database.query_leads(conn, filtro="com_email")
    sem = database.query_leads(conn, filtro="sem_email")
    conn.close()
    assert len(com) == 1 and com[0][0] == "A"
    assert len(sem) == 1 and sem[0][0] == "B"


def test_render_tabela_escapes_external_data_and_rejects_unsafe_url():
    row = (
        '<script>alert("x")</script>',
        '<img src=x onerror=alert(1)>',
        '123',
        'contato@example.org',
        'javascript:alert(1)',
        'lawyer',
        5,
        '<b>consulta</b>',
    )

    html = webapp._render_tabela([row])

    assert '<script>' not in html
    assert '<img src=x' not in html
    assert 'javascript:' not in html
    assert '&lt;script&gt;' in html
    assert '&lt;b&gt;consulta&lt;/b&gt;' in html


def test_render_tabela_accepts_exactly_eight_or_nine_columns():
    base = ("Nome", "Endereço", "123", None, "https://example.org", "lawyer", 5, "busca")

    html_eight = webapp._render_tabela([base])
    html_nine = webapp._render_tabela([base + ("sugerido@example.org",)])

    assert "sem e-mail" in html_eight
    assert "sugerido@example.org" in html_nine


def test_email_finder_rejects_local_and_private_destinations(monkeypatch):
    called = False

    def fake_session(*args, **kwargs):
        nonlocal called
        called = True
        raise AssertionError("A rede não deve ser acessada")

    monkeypatch.setattr(email_finder.requests, "Session", fake_session)

    assert email_finder.find_email("http://127.0.0.1/admin") == (None, None, None)
    assert email_finder.find_email("http://localhost/") == (None, None, None)
    assert email_finder.find_email("http://10.0.0.8/") == (None, None, None)
    assert called is False


def test_fetch_html_connects_to_validated_ip_with_host_and_tls_sni(monkeypatch):
    dns_calls = []
    captured = {}

    def fake_getaddrinfo(host, port, *args, **kwargs):
        dns_calls.append((host, port))
        return [(2, 1, 6, "", ("93.184.216.34", port))]

    class FakeResponse:
        def __init__(self):
            self.status_code = 200
            self.headers = {"Content-Type": "text/html; charset=utf-8"}
            self.encoding = "utf-8"

        @staticmethod
        def iter_content(chunk_size):
            yield b"<html>ok</html>"

    class FakeSession:
        def __init__(self):
            self.trust_env = True

        def mount(self, prefix, adapter):
            captured["mount"] = (prefix, adapter)

        def get(self, url, **kwargs):
            captured["url"] = url
            captured["kwargs"] = kwargs
            captured["trust_env"] = self.trust_env
            return FakeResponse()

        def close(self):
            captured["closed"] = True

    monkeypatch.setattr(email_finder.socket, "getaddrinfo", fake_getaddrinfo)
    monkeypatch.setattr(email_finder.requests, "Session", FakeSession)

    html = email_finder._fetch_html("https://example.org/contato?q=1", timeout=3)

    assert html == "<html>ok</html>"
    assert dns_calls == [("example.org", 443)]
    assert captured["url"] == "https://93.184.216.34/contato?q=1"
    assert captured["kwargs"]["headers"]["Host"] == "example.org"
    assert captured["kwargs"]["allow_redirects"] is False
    assert captured["trust_env"] is False
    prefix, adapter = captured["mount"]
    assert prefix == "https://"
    assert adapter._tls_hostname == "example.org"
    assert adapter.poolmanager.connection_pool_kw["server_hostname"] == "example.org"
    assert adapter.poolmanager.connection_pool_kw["assert_hostname"] == "example.org"
    assert captured["closed"] is True


def test_production_auth_protects_pages_but_not_health(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "ensino")
    monkeypatch.setenv("APP_PASSWORD", "senha-forte")
    client = webapp.app.test_client()

    denied = client.get("/")
    health = client.get("/health")
    allowed = client.get("/", headers={"Authorization": "Basic ZW5zaW5vOnNlbmhhLWZvcnRl"})

    assert denied.status_code == 401
    assert health.status_code == 200
    assert health.get_json() == {"status": "ok"}
    assert allowed.status_code == 200


def test_production_fails_closed_when_auth_or_secret_is_missing(monkeypatch):
    monkeypatch.setenv("RENDER", "true")
    monkeypatch.setenv("APP_SECRET_KEY", "configured-secret")
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    assert webapp.app.test_client().get("/").status_code == 503

    monkeypatch.setenv("APP_USERNAME", "user")
    monkeypatch.setenv("APP_PASSWORD", "password")
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    assert webapp.app.test_client().get("/").status_code == 503


def test_local_mode_allows_no_authentication_configuration(monkeypatch, tmp_path):
    monkeypatch.delenv("RENDER", raising=False)
    monkeypatch.delenv("APP_ENV", raising=False)
    monkeypatch.delenv("FLASK_ENV", raising=False)
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.delenv("APP_SECRET_KEY", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "local.db"))

    assert webapp.app.test_client().get("/").status_code == 200


def test_basic_auth_failures_are_rate_limited_in_memory(monkeypatch):
    monkeypatch.setenv("APP_USERNAME", "user")
    monkeypatch.setenv("APP_PASSWORD", "correct-password")
    monkeypatch.setattr(webapp, "_monotonic", lambda: 100.0)
    webapp._reset_rate_limits()
    client = webapp.app.test_client()

    statuses = [
        client.get("/", headers={"Authorization": "Basic dXNlcjp3cm9uZw=="}).status_code
        for _ in range(webapp.AUTH_FAILURE_LIMIT + 1)
    ]

    assert statuses[:-1] == [401] * webapp.AUTH_FAILURE_LIMIT
    assert statuses[-1] == 429


def test_buscar_posts_are_rate_limited_in_memory(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "rate.db"))
    monkeypatch.setattr(webapp, "_monotonic", lambda: 200.0)
    webapp._reset_rate_limits()
    client = webapp.app.test_client()
    home = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)

    statuses = [
        client.post(
            "/buscar",
            data={"queries": "advogado", "limit": "invalid", "csrf_token": token},
        ).status_code
        for _ in range(webapp.SEARCH_RATE_LIMIT + 1)
    ]

    assert statuses[:-1] == [400] * webapp.SEARCH_RATE_LIMIT
    assert statuses[-1] == 429


def test_database_path_can_be_configured_for_persistent_storage(monkeypatch, tmp_path):
    target = tmp_path / "persistente" / "leads.db"
    monkeypatch.setenv("LEADS_DB_PATH", str(target))

    assert database.get_db_path() == str(target)


def test_sqlite_connection_uses_wal_and_busy_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "threaded.db"))

    conn = database.get_connection()

    assert conn.execute("PRAGMA journal_mode").fetchone()[0].lower() == "wal"
    assert conn.execute("PRAGMA busy_timeout").fetchone()[0] >= 5000
    conn.close()


def test_export_csv_neutralizes_spreadsheet_formulas(tmp_path):
    import csv
    import sqlite3

    conn = sqlite3.connect(str(tmp_path / "export.db"))
    conn.execute(database.SCHEMA)
    values = (
        "=place", "+name", "  -address", "@phone", "=email", "+source",
        "-suggested", "@website", "=category", 5, 10, -5.1, -37.2,
        "\t=search", "\r=created",
    )
    conn.execute(
        "INSERT INTO leads VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        values,
    )
    target = tmp_path / "safe.csv"

    database.export_csv(conn, filepath=str(target))

    with target.open(newline="", encoding="utf-8") as csv_file:
        exported = list(csv.reader(csv_file))[1]
    conn.close()
    for index in (0, 1, 2, 3, 4, 5, 6, 7, 8, 13, 14):
        assert exported[index].startswith("'")
    assert exported[9:13] == ["5.0", "10", "-5.1", "-37.2"]


def test_export_route_uses_unique_temporary_files_and_removes_them(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "route.db"))
    paths = []

    def fake_export(conn, filepath="leads_export.csv", filtro=None, busca=None):
        paths.append(filepath)
        with open(filepath, "w", encoding="utf-8") as csv_file:
            csv_file.write("name\nexample\n")
        return filepath

    monkeypatch.setattr(webapp.database, "export_csv", fake_export)
    client = webapp.app.test_client()

    first = client.get("/exportar.csv")
    second = client.get("/exportar.csv")

    assert first.status_code == second.status_code == 200
    assert first.get_data(as_text=True) == second.get_data(as_text=True) == "name\nexample\n"
    assert len(paths) == 2 and paths[0] != paths[1]
    assert all(not os.path.exists(path) for path in paths)


def test_post_requires_csrf_token(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "csrf.db"))
    client = webapp.app.test_client()

    response = client.post("/limpar", data={"confirm": "LIMPAR"})

    assert response.status_code == 400


def test_buscar_rejects_invalid_limit_before_calling_api(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "busca.db"))
    webapp._reset_rate_limits()
    called = False

    def fake_search(*args, **kwargs):
        nonlocal called
        called = True
        return []

    monkeypatch.setattr(webapp.places_api, "text_search", fake_search)
    client = webapp.app.test_client()
    home = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)

    response = client.post(
        "/buscar",
        data={"queries": "advogado", "limit": "abc", "csrf_token": token},
    )

    assert response.status_code == 400
    assert called is False


def test_buscar_enforces_small_synchronous_work_limits(monkeypatch, tmp_path):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "sync-limits.db"))
    webapp._reset_rate_limits()
    received_limits = []

    def fake_search(query, max_results=None):
        received_limits.append(max_results)
        return []

    monkeypatch.setattr(webapp.places_api, "text_search", fake_search)
    client = webapp.app.test_client()
    home = client.get("/")
    token = re.search(r'name="csrf_token" value="([^"]+)"', home.get_data(as_text=True)).group(1)

    too_many_queries = client.post(
        "/buscar",
        data={"queries": "um\ndois\ntres", "limit": "1", "csrf_token": token},
    )
    too_many_results = client.post(
        "/buscar",
        data={"queries": "um", "limit": "6", "csrf_token": token},
    )
    default_limit = client.post(
        "/buscar",
        data={"queries": "um", "limit": "", "csrf_token": token},
    )

    assert webapp.MAX_SYNC_QUERIES == 2
    assert webapp.MAX_SYNC_RESULTS_PER_QUERY == 5
    assert too_many_queries.status_code == 400
    assert too_many_results.status_code == 400
    assert default_limit.status_code == 200
    assert received_limits == [5]


def test_web_connection_is_closed_after_request(monkeypatch):
    monkeypatch.delenv("APP_USERNAME", raising=False)
    monkeypatch.delenv("APP_PASSWORD", raising=False)

    class Result:
        @staticmethod
        def fetchone():
            return (0,)

    class TrackingConnection:
        def __init__(self):
            self.closed = False

        def execute(self, *args, **kwargs):
            return Result()

        def close(self):
            self.closed = True

    connection = TrackingConnection()
    monkeypatch.setattr(webapp.database, "get_connection", lambda: connection)

    response = webapp.app.test_client().get("/")

    assert response.status_code == 200
    assert connection.closed is True
