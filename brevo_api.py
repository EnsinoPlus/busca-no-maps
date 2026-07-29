"""Cliente mínimo da API Brevo para sincronização manual de contatos."""

import re

import requests

BASE_URL = "https://api.brevo.com/v3"
EMAIL_PATTERN = re.compile(r"^[^@\s]+@[^@\s]+\.[^@\s]+$")


def is_valid_email(value):
    email = str(value or "").strip()
    return bool(email and len(email) <= 254 and EMAIL_PATTERN.fullmatch(email))


class BrevoAPIError(RuntimeError):
    """Erro seguro da API, sem incluir chave ou dados do contato."""

    def __init__(self, message, status_code=None):
        super().__init__(message)
        self.status_code = status_code


class BrevoClient:
    def __init__(self, api_key, session=None, timeout=10):
        if not str(api_key or "").strip():
            raise ValueError("BREVO_API_KEY não configurada.")
        self.api_key = str(api_key).strip()
        self.session = session or requests.Session()
        self.session.trust_env = False
        self.timeout = timeout

    def _request(self, method, path, **kwargs):
        headers = {
            "accept": "application/json",
            "api-key": self.api_key,
            "content-type": "application/json",
        }
        try:
            response = self.session.request(
                method,
                BASE_URL + path,
                headers=headers,
                timeout=self.timeout,
                **kwargs,
            )
        except requests.RequestException as error:
            raise BrevoAPIError("Não foi possível conectar ao Brevo.") from error
        if not 200 <= response.status_code < 300:
            raise BrevoAPIError(
                f"O Brevo recusou a operação (HTTP {response.status_code}).",
                status_code=response.status_code,
            )
        if response.status_code == 204:
            return {}
        try:
            return response.json()
        except ValueError as error:
            raise BrevoAPIError("O Brevo retornou uma resposta inválida.") from error

    def list_contact_lists(self):
        payload = self._request(
            "GET",
            "/contacts/lists",
            params={"limit": 50, "offset": 0, "sort": "desc"},
        )
        lists = []
        for item in payload.get("lists", []):
            try:
                list_id = int(item.get("id"))
            except (TypeError, ValueError):
                continue
            if list_id > 0:
                lists.append({"id": list_id, "name": str(item.get("name") or f"Lista {list_id}")})
        return lists

    def upsert_contact(self, lead, list_id):
        email = str(lead.get("email") or "").strip().lower()
        try:
            list_id = int(list_id)
        except (TypeError, ValueError) as error:
            raise ValueError("Lista do Brevo inválida.") from error
        if not is_valid_email(email):
            raise ValueError("E-mail inválido.")
        if list_id < 1:
            raise ValueError("Lista do Brevo inválida.")
        result = self._request(
            "POST",
            "/contacts",
            json={
                "email": email,
                "listIds": [list_id],
                "updateEnabled": True,
                "getId": True,
            },
        )
        raw_contact_id = result.get("id") if isinstance(result, dict) else None
        try:
            contact_id = int(raw_contact_id)
        except (TypeError, ValueError):
            contact_id = 0
        if isinstance(raw_contact_id, bool) or contact_id < 1:
            raise BrevoAPIError("O Brevo não confirmou o identificador do contato.")
        result["id"] = contact_id
        return result
