"""Testes do armazenamento manual e automático no Brevo, sem chamadas reais."""
import os
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import pytest
from werkzeug.exceptions import BadRequest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import app as webapp
import brevo_api
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


def test_brevo_client_paginates_contact_lists():
    first_page = [{"id": item, "name": f"Lista {item}"} for item in range(1, 51)]
    session = RecordingSession([
        FakeResponse(200, {"count": 51, "lists": first_page}),
        FakeResponse(200, {"count": 51, "lists": [{"id": 51, "name": "Destino"}]}),
    ])
    client = brevo_api.BrevoClient("segredo-de-teste", session=session)

    lists = client.list_contact_lists()

    assert len(lists) == 51
    assert lists[-1] == {"id": 51, "name": "Destino"}
    assert [call[2]["params"]["offset"] for call in session.calls] == [0, 50]


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


def test_automatic_brevo_sync_stores_relevant_lead_without_campaign(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "relevante",
        "name": "Empresa relevante",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
        "created_at": datetime.now(timezone.utc).isoformat(),
    })
    calls = []

    class FakeClient:
        def upsert_contact(self, lead, list_id):
            calls.append((lead["email"], list_id))
            return {"id": 321}

    result = webapp._auto_sync_brevo_lead(conn, "relevante", client=FakeClient())
    row = conn.execute(
        "SELECT brevo_contact_id,brevo_list_id,brevo_synced_at,brevo_sync_error "
        "FROM leads WHERE place_id='relevante'"
    ).fetchone()
    conn.close()

    assert result == "sincronizado"
    assert calls == [("contato@empresa.test", 7)]
    assert tuple(row[:2]) == ("321", 7)
    assert row[2]
    assert row[3] is None


def test_automatic_brevo_sync_records_safe_error_without_breaking_search(monkeypatch, tmp_path):
    import brevo_api

    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-error.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "falha",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })

    class FailingClient:
        def upsert_contact(self, lead, list_id):
            raise brevo_api.BrevoAPIError("Falha segura.", status_code=429)

    result = webapp._auto_sync_brevo_lead(conn, "falha", client=FailingClient())
    row = conn.execute(
        "SELECT brevo_contact_id,brevo_last_attempt_at,brevo_sync_error "
        "FROM leads WHERE place_id='falha'"
    ).fetchone()
    conn.close()

    assert result == "falha_sistemica"
    assert row[0] is None
    assert row[1]
    assert row[2] == "Falha segura."


def test_database_does_not_create_brevo_history_for_missing_lead(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "missing-lead.db"))
    conn = database.get_connection()

    recorded = database.record_brevo_sync(
        conn, "inexistente", 123, 7, synced_email="contato@empresa.test",
    )
    history_count = conn.execute("SELECT COUNT(*) FROM brevo_sync_state").fetchone()[0]
    conn.close()

    assert recorded is False
    assert history_count == 0


def test_automatic_brevo_sync_isolates_remote_422_for_validated_contact(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-422.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "falha-422",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })

    class RejectedContactClient:
        def upsert_contact(self, lead, list_id):
            raise brevo_api.BrevoAPIError("Falha segura.", status_code=422)

    result = webapp._auto_sync_brevo_lead(
        conn, "falha-422", client=RejectedContactClient(),
    )
    conn.close()

    assert result == "falha_contato"


def test_automatic_brevo_sync_stops_when_configured_list_does_not_exist(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-missing-list.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    monkeypatch.setenv("BREVO_API_KEY", "chave-lista-ausente")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "lista-ausente",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })

    class MissingListClient:
        def __init__(self, api_key, timeout):
            assert api_key == "chave-lista-ausente"

        def list_contact_lists(self):
            return [{"id": 8, "name": "Outra lista"}]

        def upsert_contact(self, lead, list_id):
            raise AssertionError("Lista ausente deve ser detectada antes do upsert")

    monkeypatch.setattr(webapp.brevo_api, "BrevoClient", MissingListClient)
    result = webapp._auto_sync_brevo_lead(conn, "lista-ausente")
    conn.close()

    assert result == "falha_sistemica"


def test_automatic_brevo_sync_revalidates_list_after_contact_rejection(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-revalidate-list.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    monkeypatch.setenv("BREVO_API_KEY", "chave-revalidacao")
    conn = database.get_connection()
    for place_id, email in (("primeiro", "um@empresa.test"), ("segundo", "dois@empresa.test")):
        database.upsert_lead(conn, {
            "place_id": place_id,
            "email": email,
            "email_quality": "alta",
            "email_confidence": 95,
            "email_domain_aligned": 1,
            "email_mx_valid": 1,
        })
    list_checks = []
    upserts = []

    class RevalidatingClient:
        def __init__(self, api_key, timeout):
            assert api_key == "chave-revalidacao"

        def list_contact_lists(self):
            list_checks.append(True)
            return [{"id": 7, "name": "Lista válida"}]

        def upsert_contact(self, lead, list_id):
            upserts.append(lead["place_id"])
            if len(upserts) == 1:
                raise brevo_api.BrevoAPIError("Contato rejeitado.", status_code=422)
            return {"id": 702}

    monkeypatch.setattr(webapp.brevo_api, "BrevoClient", RevalidatingClient)

    first = webapp._auto_sync_brevo_lead(conn, "primeiro")
    second = webapp._auto_sync_brevo_lead(conn, "segundo")
    conn.close()

    assert (first, second) == ("falha_contato", "sincronizado")
    assert len(list_checks) == 2
    assert upserts == ["primeiro", "segundo"]


def test_automatic_brevo_sync_respects_configured_relevance_threshold(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-threshold.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    monkeypatch.setenv("BREVO_AUTO_SYNC_MIN_CONFIDENCE", "90")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "abaixo-do-limite",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 85,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })

    class UnexpectedClient:
        def upsert_contact(self, lead, list_id):
            raise AssertionError("Lead abaixo da confiança mínima não deve ir ao Brevo")

    result = webapp._auto_sync_brevo_lead(
        conn, "abaixo-do-limite", client=UnexpectedClient(),
    )
    row = conn.execute(
        "SELECT brevo_last_attempt_at FROM leads WHERE place_id='abaixo-do-limite'"
    ).fetchone()
    conn.close()

    assert result == "ignorado"
    assert row[0] is None


def test_automatic_brevo_sync_uses_short_configurable_timeout(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-timeout.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    monkeypatch.setenv("BREVO_AUTO_SYNC_TIMEOUT", "4")
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "relevante",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })
    timeouts = []

    class FakeClient:
        def __init__(self, api_key, timeout):
            assert api_key == "chave-de-teste"
            timeouts.append(timeout)

        def list_contact_lists(self):
            return [{"id": 7, "name": "Lista válida"}]

        def upsert_contact(self, lead, list_id):
            return {"id": 654}

    monkeypatch.setattr(webapp.brevo_api, "BrevoClient", FakeClient)

    result = webapp._auto_sync_brevo_lead(conn, "relevante")
    conn.close()

    assert result == "sincronizado"
    assert timeouts == [4.0]


def test_automatic_brevo_sync_does_not_repeat_contact_already_in_target_list(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-idempotent.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "sincronizado",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })
    database.record_brevo_sync(conn, "sincronizado", 777, 7)

    class UnexpectedClient:
        def upsert_contact(self, lead, list_id):
            raise AssertionError("Contato já sincronizado não deve gerar nova chamada")

    result = webapp._auto_sync_brevo_lead(
        conn, "sincronizado", client=UnexpectedClient(),
    )
    conn.close()

    assert result == "ja_sincronizado"


def test_automatic_brevo_sync_remembers_success_per_list_and_email(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-list-history.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "historico-listas",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })
    database.record_brevo_sync(
        conn, "historico-listas", 701, 7, synced_email="contato@empresa.test",
    )
    database.record_brevo_sync(
        conn, "historico-listas", 801, 8, synced_email="contato@empresa.test",
    )

    class UnexpectedClient:
        def upsert_contact(self, lead, list_id):
            raise AssertionError("Sucesso anterior na lista automática deve ser preservado")

    result = webapp._auto_sync_brevo_lead(
        conn, "historico-listas", client=UnexpectedClient(),
    )
    conn.close()

    assert result == "ja_sincronizado"


def test_automatic_brevo_sync_updates_when_relevant_email_changes(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-email-change.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "email-alterado",
        "email": "antigo@empresa.test",
        "email_quality": "alta",
        "email_confidence": 90,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })
    database.record_brevo_sync(conn, "email-alterado", 777, 7)
    conn.execute(
        "UPDATE leads SET email=?,normalized_email=?,email_confidence=? WHERE place_id=?",
        ("novo@empresa.test", "novo@empresa.test", 95, "email-alterado"),
    )
    conn.commit()
    calls = []

    class FakeClient:
        def upsert_contact(self, lead, list_id):
            calls.append((lead["email"], list_id))
            return {"id": 888}

    result = webapp._auto_sync_brevo_lead(
        conn, "email-alterado", client=FakeClient(),
    )
    conn.close()

    assert result == "sincronizado"
    assert calls == [("novo@empresa.test", 7)]


def test_automatic_brevo_sync_records_the_email_snapshot_actually_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-email-race.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "corrida",
        "email": "enviado@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })

    class ConcurrentChangeClient:
        def upsert_contact(self, lead, list_id):
            assert lead["email"] == "enviado@empresa.test"
            conn.execute(
                "UPDATE leads SET email=?,normalized_email=? WHERE place_id=?",
                ("novo@empresa.test", "novo@empresa.test", "corrida"),
            )
            conn.commit()
            return {"id": 999}

    result = webapp._auto_sync_brevo_lead(
        conn, "corrida", client=ConcurrentChangeClient(),
    )
    row = conn.execute(
        "SELECT normalized_email,brevo_synced_email FROM leads WHERE place_id='corrida'"
    ).fetchone()
    conn.close()

    assert result == "sincronizado"
    assert tuple(row) == ("novo@empresa.test", "enviado@empresa.test")


def test_manual_brevo_sync_records_the_email_snapshot_actually_sent(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "manual-email-race.db"))
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "corrida-manual",
        "email": "enviado@empresa.test",
    })
    conn.close()

    class ConcurrentChangeClient:
        def upsert_contact(self, lead, list_id):
            assert lead["email"] == "enviado@empresa.test"
            update_conn = database.get_connection()
            update_conn.execute(
                "UPDATE leads SET email=?,normalized_email=? WHERE place_id=?",
                ("novo@empresa.test", "novo@empresa.test", "corrida-manual"),
            )
            update_conn.commit()
            update_conn.close()
            return {"id": 1001}

    monkeypatch.setattr(webapp, "_brevo_client", ConcurrentChangeClient)
    webapp._reset_rate_limits()
    with webapp.app.test_request_context(
        "/brevo/sincronizar",
        method="POST",
        data={"ids": ["corrida-manual"], "list_id": "7"},
    ):
        webapp._mark_brevo_unlocked()
        webapp.session["brevo_list_ids"] = [7]
        webapp.session["brevo_lead_ids"] = ["corrida-manual"]
        response = webapp.brevo_sync()

    conn = database.get_connection()
    row = conn.execute(
        "SELECT normalized_email,brevo_synced_email "
        "FROM leads WHERE place_id='corrida-manual'"
    ).fetchone()
    conn.close()

    assert "brevo_ok=1" in response.headers["Location"]
    assert tuple(row) == ("novo@empresa.test", "enviado@empresa.test")


def test_automatic_brevo_sync_serializes_same_contact_across_search_threads(monkeypatch, tmp_path):
    monkeypatch.setenv("LEADS_DB_PATH", str(tmp_path / "automatic-concurrent.db"))
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")
    conn = database.get_connection()
    database.upsert_lead(conn, {
        "place_id": "concorrente",
        "email": "contato@empresa.test",
        "email_quality": "alta",
        "email_confidence": 95,
        "email_domain_aligned": 1,
        "email_mx_valid": 1,
    })
    conn.close()
    first_entered = threading.Event()
    release_first = threading.Event()

    class SlowClient:
        def __init__(self):
            self.calls = 0
            self.lock = threading.Lock()

        def upsert_contact(self, lead, list_id):
            with self.lock:
                self.calls += 1
                call_number = self.calls
            if call_number == 1:
                first_entered.set()
                assert release_first.wait(timeout=3)
            return {"id": 1000 + call_number}

    client = SlowClient()

    def sync_once():
        thread_conn = database.get_connection()
        try:
            return webapp._auto_sync_brevo_lead(
                thread_conn, "concorrente", client=client,
            )
        finally:
            thread_conn.close()

    with ThreadPoolExecutor(max_workers=2) as pool:
        first = pool.submit(sync_once)
        assert first_entered.wait(timeout=3)
        second = pool.submit(sync_once)
        time.sleep(0.1)
        release_first.set()
        results = [first.result(timeout=3), second.result(timeout=3)]

    assert client.calls == 1
    assert sorted(results) == ["ja_sincronizado", "sincronizado"]


@pytest.mark.parametrize(
    "failure",
    [
        RuntimeError("privado"), KeyError("privado"), IndexError("privado"),
        OverflowError("privado"), AssertionError("privado"),
    ],
)
def test_unexpected_automatic_brevo_failure_is_isolated_from_search(monkeypatch, failure):
    def unexpected_failure(conn, place_id, client=None):
        raise failure

    monkeypatch.setattr(webapp, "_auto_sync_brevo_lead", unexpected_failure)

    assert webapp._attempt_auto_sync_brevo(None, "lead-privado") == "falha_sistemica"


def test_brevo_page_reports_when_controlled_automatic_storage_is_active(monkeypatch):
    monkeypatch.setenv("APP_PUBLIC_ACCESS", "1")
    monkeypatch.setenv("BREVO_API_KEY", "chave-de-teste")
    monkeypatch.setenv("BREVO_SYNC_PASSWORD", "senha-de-teste")
    monkeypatch.setenv("BREVO_AUTO_SYNC", "1")
    monkeypatch.setenv("BREVO_AUTO_SYNC_LIST_ID", "7")

    html = webapp.app.test_client().get("/brevo").get_data(as_text=True)

    assert "Armazenamento automático controlado: ativo" in html
    assert "não dispara campanhas" in html.lower()


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
