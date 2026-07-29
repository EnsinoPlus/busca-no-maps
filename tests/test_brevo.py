"""Testes da sincronização manual e segura com o Brevo, sem chamadas reais."""
import os
import sys
from datetime import datetime, timezone

import pytest
from werkzeug.exceptions import BadRequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
import database


class FakeResponse:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload

    def json(self):
        return self._payload


class RecordingSession:
    def __init__(self, responses):
        self.responses = iter(responses)
        self.calls = []
        self.trust_env = True

    def request(self, method, url, **kwargs):
        self.calls.append((method, url, kwargs))
        return next(self.responses)


def test_brevo_client_lists_and_upserts_without_sending_campaigns():
    import brevo_api

    session = RecordingSession([
        FakeResponse(200, {"count": 1, "lists": [{"id": 7, "name": "Prospecção"}]}),
        FakeResponse(201, {"id": 123}),
    ])
    client = brevo_api.BrevoClient("segredo-de-teste", session=session, timeout=4)

    lists = client.list_contact_lists()
    result = client.upsert_contact({"email": "contato@empresa.test"}, list_id=7)

    assert lists == [{"id": 7, "name": "Prospecção"}]
    assert result == {"id": 123}
    assert session.trust_env is False
    list_call, contact_call = session.calls
    assert list_call[0:2] == ("GET", "https://api.brevo.com/v3/contacts/lists")
    assert list_call[2]["params"] == {"limit": 50, "offset": 0, "sort": "desc"}
    assert contact_call[0:2] == ("POST", "https://api.brevo.com/v3/contacts")
    assert contact_call[2]["json"] == {
        "email": "contato@empresa.test",
        "listIds": [7],
        "updateEnabled": True,
        "getId": True,
    }
    assert all("campaign" not in call[1] for call in session.calls)
    assert all(call[2]["headers"]["api-key"] == "segredo-de-teste" for call in session.calls)
    assert all(call[2]["timeout"] == 4 for call in session.calls)


def test_brevo_client_rejects_invalid_email_and_never_leaks_api_error_body():
    import brevo_api

    session = RecordingSession([FakeResponse(400, {
        "message": "contato@empresa.test rejeitado; chave segredo-de-teste",
    })])
    client = brevo_api.BrevoClient("segredo-de-teste", session=session)

    with pytest.raises(ValueError, match="E-mail inválido"):
        client.upsert_contact({"email": "endereco-invalido"}, list_id=7)
    with pytest.raises(brevo_api.BrevoAPIError) as captured:
        client.upsert_contact({"email": "contato@empresa.test"}, list_id=7)

    assert "contato@empresa.test" not in str(captured.value)
    assert "segredo-de-teste" not in str(captured.value)


@pytest.mark.parametrize("payload", [{}, {"id": "não-numérico"}, {"id": 0}, {"id": True}])
def test_brevo_client_requires_valid_contact_id_before_recording_success(payload):
    import brevo_api

    session = RecordingSession([FakeResponse(201, payload)])
    client = brevo_api.BrevoClient("segredo-de-teste", session=session)

    with pytest.raises(brevo_api.BrevoAPIError, match="identificador"):
        client.upsert_contact({"email": "contato@empresa.test"}, list_id=7)


def test_database_records_brevo_success_and_preserves_it_after_later_error(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "brevo.db"))
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "p1",
        "name": "Empresa",
        "email": "contato@empresa.test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })

    database.record_brevo_sync(
        conn,
        "p1",
        contact_id="123",
        list_id=7,
        error=None,
        attempted_at="2026-07-29T16:00:00+00:00",
    )
    database.record_brevo_sync(
        conn,
        "p1",
        contact_id=None,
        list_id=8,
        error="Falha segura",
        attempted_at="2026-07-29T16:05:00+00:00",
    )

    row = conn.execute(
        "SELECT brevo_contact_id,brevo_list_id,brevo_synced_at,"
        "brevo_last_attempt_at,brevo_sync_error FROM leads WHERE place_id='p1'"
    ).fetchone()
    crm_row = database.query_lead_records(conn, {"ids": ["p1"]})[0]
    with webapp.app.test_request_context("/leads"):
        crm_html = webapp._crm_table([crm_row])
    conn.close()
    assert tuple(row) == (
        "123", 7, "2026-07-29T16:00:00+00:00",
        "2026-07-29T16:05:00+00:00", "Falha segura",
    )
    assert "Falha na última tentativa; sincronizado anteriormente" in crm_html


def test_manual_brevo_sync_requires_unlock_confirmation_and_real_email(monkeypatch, tmp_path):
    monkeypatch.setenv("APP_PUBLIC_ACCESS", "1")
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "route.db"))
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    webapp._reset_rate_limits()
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "com-email", "name": "Empresa A", "email": "a@empresa.test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    database.upsert_lead(conn, {
        "place_id": "sem-email", "name": "Empresa B", "email": None,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    database.upsert_lead(conn, {
        "place_id": "email-invalido", "name": "Empresa C", "email": "endereco-invalido",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    database.upsert_lead(conn, {
        "place_id": "nao-confirmado", "name": "Empresa D", "email": "d@empresa-d.test",
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    conn.close()
    synced = []

    class FakeClient:
        def __init__(self, api_key):
            assert api_key == "chave-de-teste"

        def list_contact_lists(self):
            return [{"id": 7, "name": "Lista <Segura>"}]

        def upsert_contact(self, lead, list_id):
            synced.append((lead["place_id"], lead["email"], list_id))
            return {"id": 123}

    monkeypatch.setattr(webapp.brevo_api, "BrevoClient", FakeClient)
    client = webapp.app.test_client()
    login_page = client.get("/brevo")
    login_html = login_page.get_data(as_text=True)
    token = __import__("re").search(r'name="csrf_token" value="([^"]+)"', login_html).group(1)

    locked = client.post("/brevo/preparar", data={
        "csrf_token": token, "ids": ["com-email"],
    })
    unlocked = client.post("/brevo/desbloquear", data={
        "csrf_token": token, "password": "senha-de-teste",
    })
    prepare = client.post("/brevo/preparar", data={
        "csrf_token": token, "ids": ["com-email", "sem-email", "email-invalido"],
    })
    prepare_html = prepare.get_data(as_text=True)
    tampered = client.post("/brevo/sincronizar", data={
        "csrf_token": token,
        "ids": ["com-email", "sem-email", "email-invalido", "nao-confirmado"],
        "list_id": "7",
    })
    synced_response = client.post("/brevo/sincronizar", data={
        "csrf_token": token, "ids": ["com-email", "sem-email", "email-invalido"], "list_id": "7",
    })
    result_page = client.get(synced_response.headers["Location"])

    conn = database.get_connection()
    with_email = conn.execute(
        "SELECT brevo_contact_id,brevo_list_id,brevo_synced_at FROM leads WHERE place_id='com-email'"
    ).fetchone()
    ignored_rows = conn.execute(
        "SELECT brevo_contact_id FROM leads WHERE place_id IN ('sem-email','email-invalido')"
    ).fetchall()
    conn.close()

    assert login_page.status_code == 200
    assert "senha-de-teste" not in login_html
    assert locked.status_code == 403
    assert unlocked.status_code == 302
    assert prepare.status_code == 200
    assert tampered.status_code == 400
    assert "Lista &lt;Segura&gt;" in prepare_html
    assert prepare_html.count('name="ids"') == 3
    assert "1 de 3 lead(s)" in prepare_html
    assert synced_response.status_code == 302
    assert "brevo_ok=1" in synced_response.headers["Location"]
    assert "brevo_ignorado=2" in synced_response.headers["Location"]
    assert "1 sincronizado(s) com o Brevo" in result_page.get_data(as_text=True)
    assert "2 ignorado(s) sem e-mail válido" in result_page.get_data(as_text=True)
    assert synced == [("com-email", "a@empresa.test", 7)]
    assert tuple(with_email[:2]) == ("123", 7)
    assert with_email[2]
    assert all(row[0] is None for row in ignored_rows)


def test_brevo_batch_isolates_contact_error_and_caps_selection(monkeypatch, tmp_path):
    import brevo_api

    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "partial.db"))
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    conn = database.get_connection()
    for place_id in ("falha", "sucesso"):
        database.upsert_lead(conn, {
            "place_id": place_id,
            "name": f"Empresa {place_id}",
            "email": f"{place_id}@empresa.test",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    conn.close()

    class PartialClient:
        def upsert_contact(self, lead, list_id):
            if lead["place_id"] == "falha":
                raise brevo_api.BrevoAPIError("Falha segura", status_code=400)
            return {"id": 456}

    monkeypatch.setattr(webapp, "_brevo_client", PartialClient)
    webapp._reset_rate_limits()
    with webapp.app.test_request_context(
        "/brevo/sincronizar",
        method="POST",
        data={"ids": ["falha", "sucesso"], "list_id": "7"},
    ):
        webapp._mark_brevo_unlocked()
        webapp.session["brevo_list_ids"] = [7]
        webapp.session["brevo_lead_ids"] = ["falha", "sucesso"]
        response = webapp.brevo_sync()
        assert "brevo_ok=1" in response.headers["Location"]
        assert "brevo_erro=1" in response.headers["Location"]

    conn = database.get_connection()
    failed = conn.execute(
        "SELECT brevo_contact_id,brevo_sync_error FROM leads WHERE place_id='falha'"
    ).fetchone()
    succeeded = conn.execute(
        "SELECT brevo_contact_id,brevo_sync_error FROM leads WHERE place_id='sucesso'"
    ).fetchone()
    conn.close()
    assert tuple(failed) == (None, "Falha segura")
    assert tuple(succeeded) == ("456", None)

    with webapp.app.test_request_context(
        "/brevo/preparar",
        method="POST",
        data={"ids": [f"p{i}" for i in range(51)]},
    ):
        webapp._mark_brevo_unlocked()
        with pytest.raises(BadRequest):
            webapp.brevo_prepare()


def test_brevo_rate_limit_stops_batch_without_retrying_remaining_contacts(monkeypatch, tmp_path):
    import brevo_api

    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "limited.db"))
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    conn = database.get_connection()
    for index in range(3):
        database.upsert_lead(conn, {
            "place_id": f"p{index}",
            "name": f"Empresa {index}",
            "email": f"contato{index}@empresa{index}.test",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    conn.close()
    calls = []

    class LimitedClient:
        def upsert_contact(self, lead, list_id):
            calls.append(lead["place_id"])
            raise brevo_api.BrevoAPIError("Limite temporário do Brevo.", status_code=429)

    monkeypatch.setattr(webapp, "_brevo_client", LimitedClient)
    webapp._reset_rate_limits()
    with webapp.app.test_request_context(
        "/brevo/sincronizar",
        method="POST",
        data={"ids": ["p0", "p1", "p2"], "list_id": "7"},
    ):
        webapp._mark_brevo_unlocked()
        webapp.session["brevo_list_ids"] = [7]
        webapp.session["brevo_lead_ids"] = ["p0", "p1", "p2"]
        response = webapp.brevo_sync()

    assert len(calls) == 1
    assert "brevo_erro=1" in response.headers["Location"]
    assert "brevo_pendente=2" in response.headers["Location"]


@pytest.mark.parametrize("status_code", [None, 401, 403, 404, 500, 503])
def test_brevo_systemic_error_stops_batch(status_code, monkeypatch, tmp_path):
    import brevo_api

    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / f"systemic-{status_code}.db"))
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    conn = database.get_connection()
    for index in range(3):
        database.upsert_lead(conn, {
            "place_id": f"s{index}",
            "name": f"Empresa sistêmica {index}",
            "email": f"s{index}@empresa{index}.test",
            "created_at": datetime.now(timezone.utc).isoformat(),
        })
    conn.close()
    calls = []

    class SystemicFailureClient:
        def upsert_contact(self, lead, list_id):
            calls.append(lead["place_id"])
            raise brevo_api.BrevoAPIError("Falha sistêmica segura.", status_code=status_code)

    monkeypatch.setattr(webapp, "_brevo_client", SystemicFailureClient)
    webapp._reset_rate_limits()
    with webapp.app.test_request_context(
        "/brevo/sincronizar",
        method="POST",
        data={"ids": ["s0", "s1", "s2"], "list_id": "7"},
    ):
        webapp._mark_brevo_unlocked()
        webapp.session["brevo_list_ids"] = [7]
        webapp.session["brevo_lead_ids"] = ["s0", "s1", "s2"]
        response = webapp.brevo_sync()

    assert len(calls) == 1
    assert "brevo_erro=1" in response.headers["Location"]
    assert "brevo_pendente=2" in response.headers["Location"]


def test_brevo_unlock_expires_and_is_invalidated_when_password_rotates(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_ACCESS", "1")
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-original")
    webapp._reset_rate_limits()
    client = webapp.app.test_client()
    token = "csrf-de-teste"
    with client.session_transaction() as session:
        session["csrf_token"] = token
    assert client.post("/brevo/desbloquear", data={
        "csrf_token": token,
        "password": "senha-original",
    }).status_code == 302

    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-rotacionada")
    assert "Desbloquear Brevo" in client.get("/brevo").get_data(as_text=True)

    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-original")
    assert client.post("/brevo/desbloquear", data={
        "csrf_token": token,
        "password": "senha-original",
    }).status_code == 302
    with client.session_transaction() as session:
        session["brevo_unlocked_at"] = 0
    assert "Desbloquear Brevo" in client.get("/brevo").get_data(as_text=True)
