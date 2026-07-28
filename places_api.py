"""
Wrapper para a Google Places API.

Usa a Places API (New) por padrao:
  - Text Search:  POST https://places.googleapis.com/v1/places:searchText
  - Place Details: GET  https://places.googleapis.com/v1/places/{placeId}
Se a chave nao tiver acesso a nova API (erro de permissao), cai automaticamente
para a API legada (textsearch/details).

As funcoes retornam dicionarios no formato da API legada para nao quebrar o resto
do codigo (main.py / app.py usam place_id, name, formatted_address, etc).
"""
import os
import time

import requests

LEGACY_TEXT_URL = "https://maps.googleapis.com/maps/api/place/textsearch/json"
LEGACY_DETAILS_URL = "https://maps.googleapis.com/maps/api/place/details/json"
NEW_TEXT_URL = "https://places.googleapis.com/v1/places:searchText"
NEW_DETAILS_URL = "https://places.googleapis.com/v1/places/{place_id}"

FIELD_MASK = (
    "places.id,places.displayName,places.formattedAddress,places.internationalPhoneNumber,"
    "places.nationalPhoneNumber,places.websiteUri,places.rating,places.userRatingCount,"
    "places.location,places.types"
)
DETAILS_FIELD_MASK = (
    "id,displayName,formattedAddress,internationalPhoneNumber,nationalPhoneNumber,"
    "websiteUri,rating,userRatingCount,location,types,regularOpeningHours,editorialSummary"
)

API_KEY = os.environ.get("GOOGLE_MAPS_API_KEY")

MAX_RETRIES = 3
RETRY_BACKOFF = 2  # segundos (dobra a cada tentativa)


def _check_key():
    if not API_KEY:
        raise RuntimeError(
            "Variavel de ambiente GOOGLE_MAPS_API_KEY não configurada. "
            "Veja o README.md para instrucoes."
        )


def _get(url, params=None, headers=None, json_body=None, timeout=15):
    """GET/POST com retry/backoff em falhas de rede ou 5xx."""
    last_err = None
    for attempt in range(MAX_RETRIES):
        try:
            if json_body is not None:
                resp = requests.post(url, headers=headers, json=json_body, timeout=timeout)
            else:
                resp = requests.get(url, params=params, headers=headers, timeout=timeout)
        except requests.RequestException as e:
            last_err = e
            time.sleep(RETRY_BACKOFF * (attempt + 1))
            continue
        # 5xx -> tenta de novo
        if resp.status_code >= 500:
            last_err = RuntimeError(f"HTTP {resp.status_code}")
            time.sleep(RETRY_BACKOFF * (attempt + 1))
            continue
        return resp
    raise RuntimeError(f"Falha de rede apos {MAX_RETRIES} tentativas: {last_err}")


# ----------------------------------------------------------------------------
# Places API (New)
# ----------------------------------------------------------------------------
def _new_text_search(query, max_results=None):
    _check_key()
    headers = {
        "X-Goog-Api-Key": API_KEY,
        "X-Goog-FieldMask": f"{FIELD_MASK},nextPageToken",
        "Content-Type": "application/json",
    }
    results = []
    page_token = None
    while True:
        remaining = max_results - len(results) if max_results else 20
        body = {"textQuery": query, "pageSize": min(20, remaining)}
        if page_token:
            body["pageToken"] = page_token
        # pageSize maximo da API New e 20; paginamos se precisar de mais
        resp = _get(NEW_TEXT_URL, headers=headers, json_body=body)
        data = resp.json()
        if data.get("error"):
            raise RuntimeError(f"Places API (New): {data['error'].get('message', data['error'])}")
        for p in data.get("places", []):
            results.append(_new_to_legacy(p))
        if max_results and len(results) >= max_results:
            break
        page_token = data.get("nextPageToken")
        if not page_token:
            break
        time.sleep(2)  # a API exige esperar antes de usar o nextPageToken
    if max_results:
        results = results[:max_results]
    return results


def _new_to_legacy(p):
    name = (p.get("displayName") or {}).get("text") or ""
    loc = p.get("location") or {}
    return {
        "place_id": p.get("id"),
        "name": name,
        "formatted_address": p.get("formattedAddress"),
        "formatted_phone_number": p.get("internationalPhoneNumber") or p.get("nationalPhoneNumber"),
        "website": p.get("websiteUri"),
        "rating": p.get("rating"),
        "user_ratings_total": p.get("userRatingCount"),
        "types": p.get("types", []),
        "geometry": {
            "location": {"lat": loc.get("latitude"), "lng": loc.get("longitude")}
        },
    }


def _new_details(place_id):
    _check_key()
    headers = {"X-Goog-Api-Key": API_KEY, "X-Goog-FieldMask": DETAILS_FIELD_MASK}
    url = NEW_DETAILS_URL.format(place_id=place_id)
    resp = _get(url, headers=headers)
    data = resp.json()
    if data.get("error"):
        raise RuntimeError(f"Places API (New) Details: {data['error'].get('message', data['error'])}")
    return _new_to_legacy(data)


# ----------------------------------------------------------------------------
# Legacy (fallback)
# ----------------------------------------------------------------------------
def _legacy_text_search(query, max_results=None):
    _check_key()
    results = []
    params = {"query": query, "key": API_KEY}
    while True:
        resp = _get(LEGACY_TEXT_URL, params=params)
        data = resp.json()
        status = data.get("status")
        if status == "INVALID_REQUEST" and "pagetoken" in params:
            for _ in range(3):
                time.sleep(2)
                resp = _get(LEGACY_TEXT_URL, params=params)
                data = resp.json()
                status = data.get("status")
                if status != "INVALID_REQUEST":
                    break
        if status not in ("OK", "ZERO_RESULTS"):
            raise RuntimeError(
                f"Erro na Places API (legacy): {status} - {data.get('error_message', '(sem mensagem)')}"
            )
        results.extend(data.get("results", []))
        if max_results and len(results) >= max_results:
            break
        next_token = data.get("next_page_token")
        if not next_token:
            break
        time.sleep(2)
        params = {"pagetoken": next_token, "key": API_KEY}
    if max_results:
        results = results[:max_results]
    return results


def _legacy_details(place_id):
    _check_key()
    fields = ("formatted_phone_number,international_phone_number,website,opening_hours,rating,"
              "user_ratings_total,formatted_address,geometry,name")
    params = {"place_id": place_id, "fields": fields, "key": API_KEY}
    resp = _get(LEGACY_DETAILS_URL, params=params)
    data = resp.json()
    if data.get("status") != "OK":
        return {}
    return data.get("result", {})


def _legacy_permission_error(err: str) -> bool:
    e = err.lower()
    return ("request had invalid authentication credentials" in e or
            "api key expired" in e or
            "this api project is not authorized" in e or
            "places api is not enabled" in e or
            "method doesn't allow" in e or
            "request contains an invalid argument" in e and "key" in e)


# ----------------------------------------------------------------------------
# API publica
# ----------------------------------------------------------------------------
def text_search(query, region=None, max_results=None):
    """Busca por termo. Tenta API New; cai para legacy em erro de permissao."""
    try:
        return _new_text_search(query, max_results=max_results)
    except RuntimeError as e:
        if _legacy_permission_error(str(e)):
            print("  ( Places API New indisponivel para esta chave — usando legacy )")
            return _legacy_text_search(query, max_results=max_results)
        raise


def place_details(place_id):
    """Detalhes de um place_id. Tenta API New; cai para legacy em erro de permissao."""
    try:
        return _new_details(place_id)
    except RuntimeError as e:
        if _legacy_permission_error(str(e)):
            print("  ( Places API New indisponivel para esta chave — usando legacy )")
            return _legacy_details(place_id)
        raise
