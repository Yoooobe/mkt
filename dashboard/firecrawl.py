#!/usr/bin/env python3
"""4unik.mail — cliente Firecrawl (HTTP) com orçamento apertado (~1 crédito/lead).

Defaults de economia:
  - scrape homepage do domínio primeiro (1 crédito)
  - search limit=1 só se scrape falhar e o plano pedir
  - max 1 página por lead
  - free-mail: zero Firecrawl
  - cache por domínio (__domain__:{host})
"""
from __future__ import annotations

import re
from typing import Any, Optional
from urllib.parse import urlparse

import requests

import db
import vertex

FIRECRAWL_BASE = "https://api.firecrawl.dev/v1"
TIMEOUT = 60
FREE_MAIL = frozenset({
    "gmail.com", "hotmail.com", "outlook.com", "yahoo.com", "icloud.com",
    "live.com", "msn.com", "uol.com.br", "bol.com.br", "terra.com.br",
})


def evidence_ttl_days() -> int:
    raw = (vertex.env_get("LEAD_EVIDENCE_TTL_DAYS", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else 30
    except ValueError:
        return 30


def credit_budget() -> int:
    raw = (vertex.env_get("FIRECRAWL_CREDIT_BUDGET", "") or "").strip()
    try:
        return max(0, int(raw)) if raw else 35
    except ValueError:
        return 35


def max_leads_per_job() -> int:
    raw = (vertex.env_get("FIRECRAWL_MAX_LEADS_PER_JOB", "") or "").strip()
    try:
        return max(1, int(raw)) if raw else 10
    except ValueError:
        return 10


def credits_used() -> int:
    raw = db.meta_get("firecrawl_credits_used", "0") or "0"
    try:
        return max(0, int(raw))
    except ValueError:
        return 0


def credits_remaining() -> int:
    return max(0, credit_budget() - credits_used())


def _record_credit(n: int = 1) -> None:
    if n <= 0:
        return
    db.meta_set("firecrawl_credits_used", str(credits_used() + n))


def budget_ok(need: int = 1) -> bool:
    return credits_remaining() >= need


def _api_key() -> str:
    return (vertex.env_get("FIRECRAWL_API_KEY", "") or "").strip()


def disponivel() -> bool:
    return bool(_api_key())


def _headers() -> dict:
    key = _api_key()
    if not key:
        raise RuntimeError("FIRECRAWL_API_KEY não configurado (dashboard/.env)")
    return {"Authorization": f"Bearer {key}", "Content-Type": "application/json"}


def domain_from_email(email: str) -> str:
    try:
        return (email or "").split("@", 1)[1].strip().lower()
    except IndexError:
        return ""


def is_free_mail(email_or_domain: str) -> bool:
    d = email_or_domain.strip().lower()
    if "@" in d:
        d = domain_from_email(d)
    return d in FREE_MAIL


def domain_cache_key(domain: str) -> str:
    return f"__domain__:{(domain or '').strip().lower()}"


def get_domain_cache(domain: str) -> Optional[dict]:
    if not domain or is_free_mail(domain):
        return None
    return db.get_lead_evidence(domain_cache_key(domain), max_age_days=evidence_ttl_days())


def save_domain_cache(domain: str, payload: dict, urls: list = None) -> None:
    if not domain or is_free_mail(domain):
        return
    db.save_lead_evidence(domain_cache_key(domain), payload, urls=urls or payload.get("urls") or [])


def search(query: str, limit: int = 1) -> list[dict]:
    """Busca pública; devolve lista de {url, title, description}. Cobra 1 crédito."""
    if not query.strip():
        return []
    if not budget_ok(1):
        raise RuntimeError("budget Firecrawl esgotado")
    r = requests.post(
        f"{FIRECRAWL_BASE}/search",
        headers=_headers(),
        json={"query": query[:400], "limit": max(1, min(limit, 3))},
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Firecrawl search {r.status_code}: {r.text[:240]}")
    _record_credit(1)
    data = r.json() if r.text else {}
    items = data.get("data") or data.get("web") or []
    out = []
    for it in items:
        if not isinstance(it, dict):
            continue
        url = (it.get("url") or it.get("link") or "").strip()
        if not url:
            continue
        out.append({
            "url": url,
            "title": (it.get("title") or "")[:200],
            "description": (it.get("description") or it.get("snippet") or "")[:500],
        })
    return out


def scrape(url: str, only_main: bool = True) -> dict:
    """Scrape uma URL. Cobra 1 crédito. Usa maxAge (cache Firecrawl) quando possível."""
    url = (url or "").strip()
    if not url.startswith("http"):
        raise ValueError(f"URL inválida: {url[:80]}")
    if not budget_ok(1):
        raise RuntimeError("budget Firecrawl esgotado")
    body = {
        "url": url,
        "formats": ["markdown", "links"],
        "onlyMainContent": only_main,
        "maxAge": 172800000,  # 48h cache do provedor (ms)
    }
    r = requests.post(
        f"{FIRECRAWL_BASE}/scrape",
        headers=_headers(),
        json=body,
        timeout=TIMEOUT,
    )
    if r.status_code >= 400:
        raise RuntimeError(f"Firecrawl scrape {r.status_code}: {r.text[:240]}")
    _record_credit(1)
    data = (r.json() or {}).get("data") or r.json() or {}
    md = (data.get("markdown") or "")[:12000]
    links = data.get("links") or []
    if isinstance(links, list):
        links = [str(x) for x in links[:40]]
    else:
        links = []
    return {"url": url, "markdown": md, "links": links,
            "title": (data.get("metadata") or {}).get("title", "")[:200]}


def gather_lead_evidence(nome: str = "", empresa: str = "", email: str = "",
                         plan: Optional[dict] = None) -> dict[str, Any]:
    """Coleta enxuta conforme plano. Máx. ~1 crédito no caminho feliz (domain_only)."""
    domain = domain_from_email(email)
    plan = plan or {}
    modo = (plan.get("modo") or "domain_only").strip().lower()
    url_pref = (plan.get("url_preferida") or "").strip()
    query = (plan.get("query") or "").strip()

    empty = {
        "email": (email or "").strip().lower(),
        "nome": nome or "",
        "empresa": empresa or "",
        "domain": domain,
        "query": query,
        "search_hits": [],
        "pages": [],
        "urls": [],
        "errors": [],
        "modo": modo,
        "credits_spent": 0,
    }

    if is_free_mail(email) or modo == "skip" or not plan.get("usar_firecrawl", True):
        empty["errors"] = ["skip: free-mail ou plano sem firecrawl"]
        empty["modo"] = "skip"
        return empty

    # Cache por domínio
    cached = get_domain_cache(domain)
    if cached and cached.get("payload"):
        payload = dict(cached["payload"])
        payload["email"] = empty["email"]
        payload["nome"] = nome or ""
        payload["empresa"] = empresa or ""
        payload["from_domain_cache"] = True
        payload["credits_spent"] = 0
        return payload

    if not disponivel():
        empty["errors"] = ["FIRECRAWL_API_KEY ausente"]
        return empty
    if not budget_ok(1):
        empty["errors"] = ["budget Firecrawl esgotado"]
        return empty

    hits: list[dict] = []
    errors: list[str] = []
    pages: list[dict] = []
    scraped_urls: list[str] = []
    credits_before = credits_used()

    # 1) scrape homepage (preferida ou https://domain)
    target = url_pref if url_pref.startswith("http") else (
        f"https://{domain}" if domain and not is_free_mail(domain) else "")
    if target and modo in ("domain_only", "search_one"):
        try:
            page = scrape(target)
            if (page.get("markdown") or "").strip():
                pages.append(page)
                scraped_urls.append(target)
        except Exception as exc:
            errors.append(f"scrape: {str(exc)[:120]}")
            # www fallback sem gastar se já falhou? só se ainda temos budget e 0 pages
            if not pages and domain and not target.startswith("https://www."):
                alt = f"https://www.{domain}"
                try:
                    page = scrape(alt)
                    if (page.get("markdown") or "").strip():
                        pages.append(page)
                        scraped_urls.append(alt)
                except Exception as exc2:
                    errors.append(f"scrape www: {str(exc2)[:100]}")

    # 2) search_one só se scrape falhou e ainda há budget (máx +1 crédito search)
    if not pages and modo == "search_one" and budget_ok(1):
        if not query:
            query = f"{empresa or domain} site:{domain}".strip() if domain else (empresa or nome)
        try:
            hits = search(query, limit=1)
            if hits:
                try:
                    page = scrape(hits[0]["url"])
                    if (page.get("markdown") or "").strip():
                        pages.append(page)
                        scraped_urls.append(hits[0]["url"])
                except Exception as exc:
                    errors.append(f"scrape hit: {str(exc)[:100]}")
        except Exception as exc:
            errors.append(f"search: {str(exc)[:120]}")

    spent = credits_used() - credits_before
    payload = {
        "email": empty["email"],
        "nome": nome or "",
        "empresa": empresa or "",
        "domain": domain,
        "query": query,
        "search_hits": hits[:2],
        "pages": [{"url": p["url"], "title": p.get("title", ""),
                   "markdown": (p.get("markdown") or "")[:4000]} for p in pages[:1]],
        "urls": scraped_urls[:1],
        "errors": errors[:6],
        "modo": modo,
        "credits_spent": spent,
    }
    if pages and domain:
        save_domain_cache(domain, payload, urls=scraped_urls[:1])
    return payload


def evidence_text_for_agent(payload: Optional[dict]) -> str:
    """Compacta evidência para prompt do lead_validator (limite de tokens)."""
    if not payload:
        return "(sem evidência pública coletada)"
    parts = []
    for hit in (payload.get("search_hits") or [])[:2]:
        parts.append(f"- SEARCH: {hit.get('title','')} | {hit.get('url','')} | {hit.get('description','')}")
    for page in (payload.get("pages") or [])[:1]:
        md = re.sub(r"\s+", " ", (page.get("markdown") or ""))[:1500]
        parts.append(f"- PAGE {page.get('url','')}: {md}")
    if payload.get("from_domain_cache"):
        parts.append("- (reutilizado cache de domínio — 0 créditos)")
    if payload.get("errors"):
        parts.append("- ERROS: " + "; ".join(payload["errors"][:4]))
    return "\n".join(parts) if parts else "(sem evidência pública coletada)"
