"""
Uso:
    python main.py "advogado trabalhista Mossoró RN" "escritório de contabilidade Mossoró RN"

Ou com uma lista de termos em arquivo (um por linha):
    python main.py --file termos.txt

Resultados vão para leads.db (SQLite). Use --csv para também exportar um CSV.
"""
import argparse
import sys
from datetime import datetime, timezone

import places_api
import email_finder
import database


def process_query(conn, query, limit=None, somente_com_email=False):
    print(f"\n🔎 Buscando: {query}")
    try:
        results = places_api.text_search(query, max_results=limit)
    except RuntimeError as e:
        print(f"  Erro: {e}")
        return 0

    print(f"  {len(results)} resultado(s) encontrado(s). Buscando detalhes...")

    saved = 0
    for i, r in enumerate(results, 1):
        place_id = r.get("place_id")
        if not place_id:
            continue

        print(f"    [{i}/{len(results)}] {r.get('name')}")
        details = places_api.place_details(place_id)

        website = details.get("website")
        phone = details.get("formatted_phone_number")
        email, email_fonte, email_sugerido = email_finder.find_email(website) if website else (None, None, None)

        if somente_com_email and not email:
            print(f"        ⏭ sem e-mail, pulado")
            continue

        print(f"        📞 {phone or '(sem telefone)'}   ✉️ {email or '(sem e-mail encontrado)'}")

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
        saved += 1

    return saved


def main():
    parser = argparse.ArgumentParser(description="Coleta leads de negócios via Google Places API")
    parser.add_argument("queries", nargs="*", help="Termos de busca (ex: 'advogado trabalhista Mossoró RN')")
    parser.add_argument("--file", help="Arquivo .txt com um termo de busca por linha")
    parser.add_argument("--csv", action="store_true", help="Também exportar para leads_export.csv")
    parser.add_argument("--limit", type=int, default=None, help="Limitar quantidade de resultados por busca (ex: 10)")
    parser.add_argument("--somente-com-email", action="store_true", help="Só salva negócios em que encontrou e-mail (pula os sem e-mail)")
    args = parser.parse_args()

    queries = list(args.queries)
    if args.file:
        with open(args.file, encoding="utf-8") as f:
            queries.extend([line.strip() for line in f if line.strip()])

    if not queries:
        print("Nenhum termo de busca informado. Use: python main.py \"termo de busca\"")
        sys.exit(1)

    conn = database.get_connection()
    total = 0
    for q in queries:
        total += process_query(conn, q, limit=args.limit, somente_com_email=args.somente_com_email)

    print(f"\n✅ {total} lead(s) salvos no total. Banco: {database.DB_PATH} ({database.count_leads(conn)} registros únicos).")

    if args.csv:
        path = database.export_csv(conn)
        print(f"📄 Exportado para {path}")


if __name__ == "__main__":
    main()
