"""
Armazenamento local em SQLite. Um arquivo único (leads.db) na pasta do projeto.
"""
import os
import sqlite3

# Caminho absoluto baseado na pasta deste arquivo (evita criar banco vazio
# em outra pasta quando o script roda de um diretorio diferente).
DB_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "leads.db")


def get_db_path():
    """Retorna o banco configurado, permitindo volume persistente em produção."""
    return os.path.abspath(os.environ.get("LEADS_DB_PATH", DB_PATH))

SCHEMA = """
CREATE TABLE IF NOT EXISTS leads (
    place_id TEXT PRIMARY KEY,
    name TEXT,
    address TEXT,
    phone TEXT,
    email TEXT,
    email_fonte TEXT,
    email_sugerido TEXT,
    website TEXT,
    category TEXT,
    rating REAL,
    ratings_total INTEGER,
    latitude REAL,
    longitude REAL,
    search_query TEXT,
    created_at TEXT
);
"""

# Todas as colunas que a tabela deve ter, com o tipo de cada uma.
# Se um campo novo for adicionado aqui no futuro, get_connection() cria
# a coluna automaticamente em bancos já existentes (sem apagar dados).
EXPECTED_COLUMNS = {
    "place_id": "TEXT",
    "name": "TEXT",
    "address": "TEXT",
    "phone": "TEXT",
    "email": "TEXT",
    "email_fonte": "TEXT",
    "email_sugerido": "TEXT",
    "website": "TEXT",
    "category": "TEXT",
    "rating": "REAL",
    "ratings_total": "INTEGER",
    "latitude": "REAL",
    "longitude": "REAL",
    "search_query": "TEXT",
    "created_at": "TEXT",
}


def _migrate(conn):
    """Adiciona automaticamente colunas que faltam na tabela 'leads'."""
    existing = {row[1] for row in conn.execute("PRAGMA table_info(leads)").fetchall()}
    for column, col_type in EXPECTED_COLUMNS.items():
        if column not in existing:
            conn.execute(f"ALTER TABLE leads ADD COLUMN {column} {col_type}")
    conn.commit()


def get_connection():
    db_path = get_db_path()
    os.makedirs(os.path.dirname(db_path), exist_ok=True)
    conn = sqlite3.connect(db_path, timeout=5)
    conn.execute("PRAGMA busy_timeout = 5000")
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute(SCHEMA)
    _migrate(conn)
    return conn


def upsert_lead(conn, lead: dict):
    """Insere ou atualiza um lead pelo place_id (evita duplicados)."""
    conn.execute(
        """
        INSERT INTO leads (place_id, name, address, phone, email, email_fonte, email_sugerido,
                            website, category, rating, ratings_total, latitude, longitude,
                            search_query, created_at)
        VALUES (:place_id, :name, :address, :phone, :email, :email_fonte, :email_sugerido,
                :website, :category, :rating, :ratings_total, :latitude, :longitude,
                :search_query, :created_at)
        ON CONFLICT(place_id) DO UPDATE SET
            name=excluded.name,
            address=excluded.address,
            phone=excluded.phone,
            email=excluded.email,
            email_fonte=excluded.email_fonte,
            email_sugerido=excluded.email_sugerido,
            website=excluded.website,
            category=excluded.category,
            rating=excluded.rating,
            ratings_total=excluded.ratings_total,
            latitude=excluded.latitude,
            longitude=excluded.longitude
        """,
        lead,
    )
    conn.commit()


def count_leads(conn):
    return conn.execute("SELECT COUNT(*) FROM leads").fetchone()[0]


def query_leads(conn, filtro=None, busca=None):
    """Retorna linhas filtradas.

    filtro: None/'todos' | 'com_email' | 'sem_email'
    busca:  termo parcial do search_query (opcional)
    Colunas: name, address, phone, email, website, category, rating, search_query
    """
    sql = (
        "SELECT name, address, phone, email, website, category, rating, search_query "
        "FROM leads WHERE 1=1"
    )
    params = []
    if filtro == "com_email":
        sql += " AND email IS NOT NULL AND email != ''"
    elif filtro == "sem_email":
        sql += " AND (email IS NULL OR email = '')"
    if busca:
        sql += " AND search_query LIKE ?"
        params.append(f"%{busca}%")
    sql += " ORDER BY created_at DESC"
    return conn.execute(sql, params).fetchall()


def export_csv(conn, filepath="leads_export.csv", filtro=None, busca=None):
    import csv

    rows = conn.execute(
        "SELECT place_id, name, address, phone, email, email_fonte, email_sugerido, website, "
        "category, rating, ratings_total, latitude, longitude, search_query, created_at "
        "FROM leads WHERE 1=1"
    ).fetchall()
    # aplica o mesmo filtro usado na consulta (mantem consistencia com a tela)
    if filtro == "com_email":
        rows = [r for r in rows if r[4]]
    elif filtro == "sem_email":
        rows = [r for r in rows if not r[4]]
    if busca:
        rows = [r for r in rows if busca.lower() in (r[13] or "").lower()]

    headers = [
        "place_id", "name", "address", "phone", "email", "email_fonte", "email_sugerido",
        "website", "category", "rating", "ratings_total", "latitude", "longitude",
        "search_query", "created_at",
    ]
    def _safe_cell(value):
        if isinstance(value, str) and value.lstrip().startswith(("=", "+", "-", "@")):
            return "'" + value
        return value

    with open(filepath, "w", newline="", encoding="utf-8") as f:
        writer = csv.writer(f)
        writer.writerow(headers)
        writer.writerows([_safe_cell(value) for value in row] for row in rows)
    return filepath


def limpar_emails(conn, import_urllib=None):
    """Remove codificacao de URL (%20) e espacos de e-mails ja salvos.

    Retorna o numero de registros corrigidos.
    """
    import urllib.parse

    atualizados = 0
    for place_id, email in conn.execute("SELECT place_id, email FROM leads").fetchall():
        if not email:
            continue
        limpo = urllib.parse.unquote(email).strip()
        if limpo != email:
            conn.execute("UPDATE leads SET email = ? WHERE place_id = ?", (limpo, place_id))
            atualizados += 1
    conn.commit()
    return atualizados


def apagar_todos(conn):
    """Remove todos os leads (usado pelo botao de limpar banco)."""
    conn.execute("DELETE FROM leads")
    conn.commit()
