"""Avaliação conservadora da qualidade de e-mails encontrados."""
import os

from dns.exception import DNSException

import database

GENERIC_PROVIDERS = {"gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "uol.com.br"}


def has_mx(domain, timeout=1.0):
    """Consulta MX com tempo estritamente limitado; falhas ficam como desconhecidas."""
    try:
        import dns.resolver
        resolver = dns.resolver.Resolver(configure=True)
        resolver.timeout = min(float(timeout), 2.0)
        resolver.lifetime = min(float(timeout), 2.0)
        return bool(resolver.resolve(domain, "MX"))
    except (DNSException, OSError, ValueError):
        return None


def assess_email(email, source, website, check_mx=None):
    domain = database.normalize_domain(email)
    site_domain = database.normalize_domain(website)
    aligned = bool(domain and site_domain and (domain == site_domain or domain.endswith("." + site_domain)))
    if check_mx is None:
        check_mx = os.environ.get("EMAIL_MX_CHECK", "1").lower() not in {"0", "false", "no"}
    mx_valid = has_mx(domain, timeout=1.0) if check_mx and domain else None

    confidence = 45 if source and str(source).startswith(("http://", "https://")) else 30
    if aligned: confidence += 30
    if mx_valid is True: confidence += 20
    elif mx_valid is False: confidence -= 20
    if domain in GENERIC_PROVIDERS: confidence -= 15
    confidence = max(0, min(100, confidence))
    quality = "alta" if confidence >= 80 else "media" if confidence >= 50 else "baixa"
    return {"quality": quality, "confidence": confidence, "domain_aligned": aligned, "mx_valid": mx_valid}
