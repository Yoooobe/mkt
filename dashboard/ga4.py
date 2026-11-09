#!/usr/bin/env python3
"""Yoobe Mail — GA4: leitura via Data API v1beta (REST + service account) e
espelhamento de campanhas via Measurement Protocol.

Sem credencial configurada, tudo degrada graciosamente ({"configurado": False}).
Relatórios passam por cache em db.ga4_cache (TTL) para proteger a cota da API.
"""
import json
import ssl
import urllib.parse
import urllib.request
from datetime import datetime

import db
import google_media
from engine import ENV, log

try:
    import certifi
    _SSL_CTX = ssl.create_default_context(cafile=certifi.where())
except ImportError:  # sem certifi, usa o default do sistema
    _SSL_CTX = ssl.create_default_context()

PROPERTY_ID = ENV.get("GA4_PROPERTY_ID", "") or ENV.get("GOOGLE_GA4_PROPERTY_ID", "")
CREDENTIALS_JSON = ENV.get("GA4_CREDENTIALS_JSON", "")
HOSTS = {
    "plataforma": ENV.get("GA4_HOSTNAME_PLATAFORMA", "corporate.yoobe.co"),
    "site": ENV.get("GA4_HOSTNAME_LOJA", "catalogo.yoobe.co"),
}
MEASUREMENT_ID = ENV.get("GA4_MEASUREMENT_ID", "") or ENV.get("GOOGLE_GA4_MEASUREMENT_ID", "")
MP_API_SECRET = ENV.get("GA4_MP_API_SECRET", "")
KEY_EVENTS = ("generate_lead", "schedule_demo", "contact_whatsapp", "contact_email")
CACHE_TTL = 900

_creds = None


def configured():
    return bool(_property_id())


def _property_id():
    """Lê a configuração atual para não exigir reinício após salvar no painel."""
    return (google_media._env().get("GA4_PROPERTY_ID", "") or
            google_media._env().get("GOOGLE_GA4_PROPERTY_ID", "") or PROPERTY_ID)


def _token():
    """Service account (GA4_CREDENTIALS_JSON) com fallback no OAuth gcloud do
    google_media — funciona com qualquer um dos dois setups.

    Aceita caminho de arquivo (local) ou JSON inline (secret no container).
    """
    global _creds
    raw = (google_media._env().get("GA4_CREDENTIALS_JSON", "") or CREDENTIALS_JSON or "").strip()
    if not raw:
        return google_media._token()
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    if _creds is None:
        scopes = ["https://www.googleapis.com/auth/analytics.readonly"]
        if raw.startswith("{"):
            _creds = service_account.Credentials.from_service_account_info(
                json.loads(raw), scopes=scopes)
        else:
            _creds = service_account.Credentials.from_service_account_file(
                raw, scopes=scopes)
    if not _creds.valid:
        _creds.refresh(Request())
    return _creds.token


def _post(url, body, headers):
    req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                 headers={"Content-Type": "application/json", **headers})
    with urllib.request.urlopen(req, timeout=30, context=_SSL_CTX) as r:
        return json.loads(r.read().decode())


def _batch_run_reports(requests_):
    url = f"https://analyticsdata.googleapis.com/v1beta/properties/{_property_id()}:batchRunReports"
    body = {"requests": requests_}
    # A API de dados exige analytics.readonly. Não reutilizar o OAuth genérico
    # do painel, pois ele pode ter sido autorizado apenas para integrações de
    # mídia. A service account configurada para a propriedade é a fonte certa.
    return google_media._request(url, method="POST", payload=body, access_token=_token())


def _cached(key, fetch_fn, ttl=CACHE_TTL):
    c = db.conn()
    row = c.execute("SELECT payload, fetched_at FROM ga4_cache WHERE key=?", (key,)).fetchone()
    if row:
        age = datetime.now() - datetime.fromisoformat(row["fetched_at"])
        if age.total_seconds() < ttl:
            c.close()
            return json.loads(row["payload"]), True
    c.close()
    data = fetch_fn()
    c = db.conn()
    c.execute("INSERT OR REPLACE INTO ga4_cache (key, payload, fetched_at) VALUES (?,?,?)",
              (key, json.dumps(data), datetime.now().isoformat()))
    c.commit()
    c.close()
    return data, False


def _rows(report):
    out = []
    for r in report.get("rows", []):
        out.append([v.get("value") for v in r.get("dimensionValues", [])] +
                   [v.get("value") for v in r.get("metricValues", [])])
    return out


def _date_range(days):
    return [{"startDate": f"{days}daysAgo", "endDate": "today"}]


def _host_filter(site):
    """Filtro para os dois domínios monitorados, sem misturar hosts externos."""
    if site == "todos":
        values = [HOSTS["plataforma"], HOSTS["site"], f"www.{HOSTS['site']}"]
    else:
        # ``loja`` continua aceito para URLs antigas do dashboard.
        key = "site" if site == "loja" else site
        host = HOSTS.get(key, HOSTS["plataforma"])
        values = [host] if key == "plataforma" else [host, f"www.{host}"]
    return {"filter": {"fieldName": "hostName",
                       "inListFilter": {"values": values}}}


def _site_label(site):
    if site == "todos":
        return f"{HOSTS['plataforma']} + {HOSTS['site']}"
    return HOSTS.get("site" if site == "loja" else site, HOSTS["plataforma"])


def site_overview(days=28, site="plataforma"):
    if not configured():
        return {"configurado": False}

    def fetch():
        reqs = [
            {"dateRanges": _date_range(days), "dimensions": [{"name": "date"}],
             "metrics": [{"name": "totalUsers"}, {"name": "sessions"},
                         {"name": "screenPageViews"}, {"name": "keyEvents"}],
             "dimensionFilter": _host_filter(site),
             "orderBys": [{"dimension": {"dimensionName": "date"}}]},
            {"dateRanges": _date_range(days),
             "dimensions": [{"name": "sessionSource"}, {"name": "sessionMedium"}],
             "metrics": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
             "dimensionFilter": _host_filter(site), "limit": "10",
             "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}]},
            {"dateRanges": _date_range(days), "dimensions": [{"name": "pagePath"}],
             "metrics": [{"name": "screenPageViews"}, {"name": "totalUsers"}],
             "dimensionFilter": _host_filter(site), "limit": "10",
             "orderBys": [{"metric": {"metricName": "screenPageViews"}, "desc": True}]},
            {"dateRanges": _date_range(days), "dimensions": [{"name": "eventName"}],
             "metrics": [{"name": "eventCount"}],
             "dimensionFilter": {"andGroup": {"expressions": [
                 _host_filter(site),
                 {"filter": {"fieldName": "eventName",
                             "inListFilter": {"values": list(KEY_EVENTS)}}}]}}},
        ]
        resp = _batch_run_reports(reqs)
        reports = resp.get("reports", [])
        dias = [{"data": r[0], "usuarios": int(r[1]), "sessoes": int(r[2]),
                 "pageviews": int(r[3]), "key_events": int(float(r[4]))}
                for r in _rows(reports[0])] if len(reports) > 0 else []
        fontes = [{"fonte": r[0], "medium": r[1], "sessoes": int(r[2]),
                   "usuarios": int(r[3]), "key_events": int(float(r[4]))}
                  for r in _rows(reports[1])] if len(reports) > 1 else []
        paginas = [{"pagina": r[0], "pageviews": int(r[1]), "usuarios": int(r[2])}
                   for r in _rows(reports[2])] if len(reports) > 2 else []
        eventos = {r[0]: int(r[1]) for r in _rows(reports[3])} if len(reports) > 3 else {}
        return {"configurado": True, "site": site, "host": _site_label(site), "dias": dias,
                "fontes": fontes, "paginas": paginas, "key_events": eventos,
                "totais": {"usuarios": sum(d["usuarios"] for d in dias),
                           "sessoes": sum(d["sessoes"] for d in dias),
                           "pageviews": sum(d["pageviews"] for d in dias),
                           "key_events": sum(d["key_events"] for d in dias)}}

    try:
        data, from_cache = _cached(f"overview:{site}:{days}", fetch)
        data["cache"] = from_cache
        return data
    except Exception as e:
        log(f"ga4 overview erro: {e}")
        return {"configurado": True, "erro": str(e)}


def site_drilldown(kind, days=28, site="plataforma"):
    """Detalhes clicáveis de aquisição, visita, evento e geração de lead no GA4."""
    if not configured():
        return {"configurado": False, "linhas": []}
    specs = {
        "origens": {
            "dims": ["sessionDefaultChannelGroup", "sessionSource", "sessionMedium", "sessionCampaignName"],
            "metrics": ["sessions", "totalUsers", "keyEvents"],
            "labels": ["Canal", "Origem", "Mídia", "Campanha", "Sessões", "Usuários", "Key events"],
        },
        "visitas": {
            "dims": ["newVsReturning", "deviceCategory", "sessionDefaultChannelGroup"],
            "metrics": ["sessions", "totalUsers", "engagedSessions", "keyEvents"],
            "labels": ["Tipo", "Dispositivo", "Canal", "Sessões", "Usuários", "Engajadas", "Key events"],
        },
        "eventos": {
            "dims": ["eventName"], "metrics": ["eventCount", "totalUsers"],
            "labels": ["Evento", "Ocorrências", "Usuários"],
        },
        "leads": {
            "dims": ["eventName", "sessionSource", "sessionMedium", "landingPagePlusQueryString"],
            "metrics": ["eventCount", "totalUsers"],
            "labels": ["Tipo de lead", "Origem", "Mídia", "Página de entrada", "Leads", "Usuários"],
            "events": ["generate_lead", "form_submit", "schedule_demo", "contact_whatsapp", "contact_email"],
        },
        "paginas": {
            "dims": ["landingPagePlusQueryString", "pagePath"],
            "metrics": ["sessions", "screenPageViews", "totalUsers", "keyEvents"],
            "labels": ["Página de entrada", "Página vista", "Sessões", "Views", "Usuários", "Key events"],
        },
    }
    spec = specs.get(kind)
    if not spec:
        raise ValueError("detalhe GA4 inválido")

    def fetch():
        request = {"dateRanges": _date_range(days), "dimensions": [{"name": x} for x in spec["dims"]],
                   "metrics": [{"name": x} for x in spec["metrics"]], "limit": "100",
                   "orderBys": [{"metric": {"metricName": spec["metrics"][0]}, "desc": True}]}
        filters = [_host_filter(site)]
        if spec.get("events"):
            filters.append({"filter": {"fieldName": "eventName", "inListFilter": {"values": spec["events"]}}})
        request["dimensionFilter"] = filters[0] if len(filters) == 1 else {"andGroup": {"expressions": filters}}
        report = _batch_run_reports([request]).get("reports", [{}])[0]
        linhas = []
        for raw in _rows(report):
            vals = []
            for n, value in enumerate(raw):
                vals.append(int(float(value)) if n >= len(spec["dims"]) and str(value).replace(".", "", 1).isdigit() else (value or "(não definido)"))
            linhas.append(vals)
        return {"configurado": True, "kind": kind, "labels": spec["labels"], "linhas": linhas}

    try:
        data, cached = _cached(f"drilldown:{site}:{kind}:{days}", fetch)
        data["cache"] = cached
        return data
    except Exception as exc:
        log(f"ga4 drilldown {kind} erro: {exc}")
        return {"configurado": True, "kind": kind, "labels": spec["labels"], "linhas": [], "erro": str(exc)}


def utm_campaign_report(days=28):
    if not configured():
        return {"configurado": False}

    def fetch():
        reqs = [{
            "dateRanges": _date_range(days),
            "dimensions": [{"name": "sessionCampaignName"}],
            "metrics": [{"name": "sessions"}, {"name": "totalUsers"}, {"name": "keyEvents"}],
            "dimensionFilter": {"orGroup": {"expressions": [
                {"filter": {"fieldName": "sessionSource",
                             "stringFilter": {"value": source}}}
                for source in ("yoobe-mail", "4unik-mail")
            ]}},
            "orderBys": [{"metric": {"metricName": "sessions"}, "desc": True}],
            "limit": "50",
        }]
        resp = _batch_run_reports(reqs)
        reports = resp.get("reports", [])
        campanhas = [{"utm_campaign": r[0], "sessoes": int(r[1]), "usuarios": int(r[2]),
                      "key_events": int(float(r[3]))}
                     for r in _rows(reports[0])] if reports else []
        return {"configurado": True, "campanhas": campanhas}

    try:
        data, from_cache = _cached(f"utm:{days}", fetch)
        data["cache"] = from_cache
        return data
    except Exception as e:
        log(f"ga4 utm erro: {e}")
        return {"configurado": True, "erro": str(e)}


def mp_send_event(name, params):
    """Measurement Protocol — marca eventos do Yoobe Mail na linha do tempo do GA4.
    Falha só loga; nunca bloqueia o fluxo de campanha."""
    if not (MEASUREMENT_ID and MP_API_SECRET):
        return False
    try:
        url = (f"https://www.google-analytics.com/mp/collect"
               f"?measurement_id={MEASUREMENT_ID}&api_secret={MP_API_SECRET}")
        body = {"client_id": "yoobe-mail.server",
                "events": [{"name": name, "params": params}]}
        req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                     headers={"Content-Type": "application/json"})
        urllib.request.urlopen(req, timeout=10, context=_SSL_CTX)
        log(f"ga4 mp: evento {name} enviado ({params.get('campaign_slug', '')})")
        return True
    except Exception as e:
        log(f"ga4 mp erro: {e}")
        return False


SANITY_PROJECT = ENV.get("SANITY_PROJECT_ID", "hin8ivz0")
SANITY_DATASET = ENV.get("SANITY_DATASET", "production")


def site_leads(limit=50):
    """Leads IDENTIFICADOS do formulário do site (nome/email/empresa/origem) — não vêm do
    GA4 (que só conta eventos anônimos): a leads-ingest-api grava cada submissão como
    documento leadSubmission no Sanity e notifica corporate@yoobe.co por email."""
    def fetch():
        q = ('*[_type=="leadSubmission"]|order(_createdAt desc)[0...%d]'
             '{_createdAt, name, nome, email, empresa, company, phone, telefone,'
             ' source, locale, message}' % max(1, min(int(limit), 200)))
        url = (f"https://{SANITY_PROJECT}.api.sanity.io/v2024-03-17/data/query/"
               f"{SANITY_DATASET}?query=" + urllib.parse.quote(q, safe=""))
        req = urllib.request.Request(url, headers={"Accept": "application/json"})
        with urllib.request.urlopen(req, timeout=20, context=_SSL_CTX) as r:
            docs = json.loads(r.read().decode()).get("result", [])
        leads = [{"quando": (d.get("_createdAt") or "")[:16].replace("T", " "),
                  "nome": d.get("name") or d.get("nome") or "",
                  "email": (d.get("email") or "").lower(),
                  "empresa": d.get("company") or d.get("empresa") or "",
                  "telefone": d.get("phone") or d.get("telefone") or "",
                  "origem": d.get("source") or "", "idioma": d.get("locale") or "",
                  "mensagem": d.get("message") or ""} for d in docs]
        return {"configurado": True, "leads": leads}

    try:
        data, from_cache = _cached("site_leads", fetch, ttl=300)
        data["cache"] = from_cache
        return data
    except Exception as e:
        log(f"sanity leads erro: {e}")
        return {"configurado": True, "erro": str(e), "leads": []}
