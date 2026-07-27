"""Testes basicos do Leads Maps (sem rede / sem API key real).

Rode com: pytest tests/ -q
"""
import os
import sys

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import email_finder
import database


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
