#!/usr/bin/env python3
"""Integrações somente leitura com GA4, Google Tag Manager e Google Ads.

Segredos ficam em dashboard/.env. O módulo nunca cria anúncios, altera tags ou
publica containers: consolida métricas para o painel e gera UTMs consistentes.
"""
import json
import os
import subprocess
import urllib.error
import urllib.parse
import urllib.request
import ssl
from pathlib import Path

ENV_FILE = Path(__file__).resolve().parent / ".env"
PREFIX = "GOOGLE_"
KEYS = ("GOOGLE_GA4_ACCOUNT_ID", "GOOGLE_GA4_PROPERTY_ID", "GOOGLE_GTM_ACCOUNT_ID", "GOOGLE_GTM_CONTAINER_ID",
        "GOOGLE_GA4_MEASUREMENT_ID", "GOOGLE_GA4_SITE_MEASUREMENT_ID", "GOOGLE_TAG_ID", "GOOGLE_ADS_TAG_ID",
        "GOOGLE_ADS_CUSTOMER_ID", "GOOGLE_ADS_LOGIN_CUSTOMER_ID", "GOOGLE_ADS_DEVELOPER_TOKEN")


def _env():
    values = dict(os.environ)
    if ENV_FILE.exists():
        for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
            if "=" in line and not line.lstrip().startswith("#"):
                k, v = line.split("=", 1)
                values[k.strip()] = v.strip()
    return values


def _token():
    for cmd in (["gcloud", "auth", "application-default", "print-access-token"],
                ["gcloud", "auth", "print-access-token"]):
        p = subprocess.run(cmd, capture_output=True, text=True, timeout=20)
        if p.returncode == 0 and p.stdout.strip():
            return p.stdout.strip()
    raise RuntimeError("OAuth Google não encontrado. Reautorize a conta com os escopos de mídia.")


def _request(url, method="GET", payload=None, headers=None, access_token=None):
    h = {"Authorization": "Bearer " + (access_token or _token()), "Accept": "application/json"}
    h.update(headers or {})
    data = json.dumps(payload).encode() if payload is not None else None
    if data:
        h["Content-Type"] = "application/json"
    req = urllib.request.Request(url, data=data, headers=h, method=method)
    try:
        # Python instalado pelo macOS pode não carregar o keychain como CA bundle.
        # Certifi preserva a validação TLS sem aceitar certificados não confiáveis.
        try:
            import certifi
            context = ssl.create_default_context(cafile=certifi.where())
        except Exception:
            context = ssl.create_default_context()
        with urllib.request.urlopen(req, timeout=30, context=context) as r:
            return json.loads(r.read().decode() or "{}")
    except urllib.error.HTTPError as exc:
        body = exc.read().decode(errors="replace")
        try:
            msg = json.loads(body).get("error", {}).get("message", body)
        except Exception:
            msg = body
        raise RuntimeError(f"Google API {exc.code}: {msg[:320]}")
    except urllib.error.URLError as exc:
        # Em alguns Pythons do macOS, o bundle não inclui a CA corporativa que o
        # keychain do sistema aceita. O curl do sistema valida pelo keychain; só
        # o usamos nesse caso, sem -k/--insecure e sem persistir o token.
        if "CERTIFICATE_VERIFY_FAILED" not in str(exc):
            raise RuntimeError(f"Falha de rede Google: {exc}")
        cmd = ["curl", "-fsS", "--max-time", "30", "-K", "-", "-X", method, url]
        curl_config = [f'header = "Authorization: {h["Authorization"]}"',
                       'header = "Accept: application/json"']
        for key, value in (headers or {}).items():
            curl_config.append(f'header = "{key}: {value}"')
        if payload is not None:
            curl_config.append('header = "Content-Type: application/json"')
            curl_config.append("data = " + json.dumps(json.dumps(payload)))
        p = subprocess.run(cmd, input="\n".join(curl_config) + "\n", capture_output=True,
                           text=True, timeout=35)
        if p.returncode != 0:
            raise RuntimeError(f"Google API via TLS do sistema: {(p.stderr or p.stdout)[:320]}")
        try:
            return json.loads(p.stdout or "{}")
        except json.JSONDecodeError as parse_error:
            raise RuntimeError(f"Resposta inválida do Google: {parse_error}")


def status():
    e = _env()
    cfg = {k: bool(e.get(k, "")) for k in KEYS}
    return {
        "configurado": cfg,
        "ga4": {"account_id": _digits(e.get("GOOGLE_GA4_ACCOUNT_ID", "")),
                "property_id": _digits(e.get("GOOGLE_GA4_PROPERTY_ID", "")),
                "measurement_id": e.get("GOOGLE_GA4_MEASUREMENT_ID", ""),
                "site_measurement_id": e.get("GOOGLE_GA4_SITE_MEASUREMENT_ID", "")},
        "gtm": {"account_id": _digits(e.get("GOOGLE_GTM_ACCOUNT_ID", "")),
                "container_id": _digits(e.get("GOOGLE_GTM_CONTAINER_ID", ""))},
        "tags": {"google_tag_id": e.get("GOOGLE_TAG_ID", ""),
                 "ads_tag_id": e.get("GOOGLE_ADS_TAG_ID", "")},
        "ga4_pronto": cfg["GOOGLE_GA4_PROPERTY_ID"],
        "gtm_pronto": cfg["GOOGLE_GTM_ACCOUNT_ID"] and cfg["GOOGLE_GTM_CONTAINER_ID"],
        "ads_pronto": cfg["GOOGLE_ADS_CUSTOMER_ID"] and cfg["GOOGLE_ADS_DEVELOPER_TOKEN"],
        "oauth_comando": "gcloud auth application-default login --scopes=https://www.googleapis.com/auth/cloud-platform,https://www.googleapis.com/auth/analytics.readonly,https://www.googleapis.com/auth/analytics.edit,https://www.googleapis.com/auth/tagmanager.readonly",
        "ads_nota": "Google Ads requer OAuth próprio com escopo adwords; o gcloud não emite esse escopo.",
        "nota": "Leitura e atribuição somente; o dashboard não cria nem altera campanhas, anúncios ou tags.",
    }


def save_config(values):
    """Salva apenas chaves conhecidas; não devolve valores secretos."""
    current = ENV_FILE.read_text(encoding="utf-8") if ENV_FILE.exists() else ""
    lines = current.splitlines()
    seen = set()
    out = []
    for line in lines:
        key = line.split("=", 1)[0].strip() if "=" in line else ""
        if key in KEYS:
            seen.add(key)
            # Campo vazio na UI significa "manter o segredo atual".
            val = str(values.get(key, "")).strip() or line.split("=", 1)[1]
            out.append(key + "=" + val)
        else:
            out.append(line)
    for key in KEYS:
        if key not in seen and str(values.get(key, "")).strip():
            out.append(key + "=" + str(values[key]).strip())
    ENV_FILE.write_text("\n".join(out).rstrip() + "\n", encoding="utf-8")
    return status()


def _digits(value):
    return "".join(c for c in (value or "") if c.isdigit())


def _service_account_token(scopes):
    """Token da conta de serviço local, quando ela estiver configurada."""
    credentials_file = _env().get("GA4_CREDENTIALS_JSON", "").strip()
    if not credentials_file or not Path(credentials_file).expanduser().exists():
        return None
    from google.oauth2 import service_account
    from google.auth.transport.requests import Request
    creds = service_account.Credentials.from_service_account_file(
        str(Path(credentials_file).expanduser()),
        scopes=scopes,
    )
    creds.refresh(Request())
    return creds.token


def _ga4_read_token():
    """Token de leitura da service account da propriedade GA4, quando presente."""
    return _service_account_token(["https://www.googleapis.com/auth/analytics.readonly"])


def _gtm_read_token():
    """Token GTM: requer que a conta de serviço seja Viewer no container/conta."""
    return _service_account_token(["https://www.googleapis.com/auth/tagmanager.readonly"])


def ga4_report(days=30):
    prop = _env().get("GOOGLE_GA4_PROPERTY_ID", "").strip()
    if not prop:
        raise RuntimeError("Informe a Property ID do GA4 para sincronizar.")
    body = {"dateRanges": [{"startDate": f"{max(1, min(int(days), 365))}daysAgo", "endDate": "today"}],
            "metrics": [{"name": "sessions"}, {"name": "activeUsers"},
                        {"name": "conversions"}, {"name": "eventCount"}]}
    data = _request(f"https://analyticsdata.googleapis.com/v1beta/properties/{_digits(prop)}:runReport",
                    method="POST", payload=body, access_token=_ga4_read_token())
    vals = (data.get("rows") or [{}])[0].get("metricValues") or []
    names = ("sessoes", "usuarios_ativos", "conversoes", "eventos")
    return {name: int(vals[i].get("value", 0)) if i < len(vals) else 0 for i, name in enumerate(names)} | {"dias": days}


def ga4_resources():
    """Lista propriedades e streams pela conta de serviço configurada."""
    token = _ga4_read_token()
    data = _request("https://analyticsadmin.googleapis.com/v1alpha/accountSummaries", access_token=token)
    out = []
    for account in data.get("accountSummaries", []):
        for prop in account.get("propertySummaries", []):
            pid = _digits(prop.get("property", ""))
            streams = []
            if pid:
                try:
                    s = _request(f"https://analyticsadmin.googleapis.com/v1alpha/properties/{pid}/dataStreams",
                                 access_token=token)
                    streams = [{"id": _digits(x.get("name", "")), "nome": x.get("displayName", ""),
                                "measurement_id": x.get("webStreamData", {}).get("measurementId", "")}
                               for x in s.get("dataStreams", []) if x.get("type") == "WEB_DATA_STREAM"]
                except Exception:
                    pass
            out.append({"account": account.get("displayName", ""),
                        "account_id": _digits(account.get("account", "")), "id": pid,
                        "nome": prop.get("displayName", ""), "streams": streams})
    return {"propriedades": out}


GA4_ACQUISITION_EVENTS = (
    "generate_lead",
    "schedule_demo",
    "contact_whatsapp",
    "contact_email",
)


def ensure_ga4_key_events(event_names=GA4_ACQUISITION_EVENTS):
    """Cria eventos-chave de aquisição no GA4 sem alterar anúncios ou tags.

    Os nomes refletem somente eventos instrumentados no site. ``form_submit``
    fica fora da lista porque pode ser um evento de medição avançada do GA4 e
    não representa, por si só, um lead qualificado.
    """
    prop = _digits(_env().get("GOOGLE_GA4_PROPERTY_ID", ""))
    if not prop:
        raise RuntimeError("Informe a Property ID do GA4 antes de configurar as metas.")
    # Prefere a service account configurada, que não depende do cliente OAuth
    # local bloqueado pelo Google. Ela ainda precisa ter função Editor no GA4.
    edit_token = None
    credentials_file = _env().get("GA4_CREDENTIALS_JSON", "").strip()
    if credentials_file and Path(credentials_file).expanduser().exists():
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
            creds = service_account.Credentials.from_service_account_file(
                str(Path(credentials_file).expanduser()),
                scopes=["https://www.googleapis.com/auth/analytics.edit"],
            )
            creds.refresh(Request())
            edit_token = creds.token
        except Exception as exc:
            raise RuntimeError(f"Service account GA4 não pôde autorizar edição: {exc}")
    base = f"https://analyticsadmin.googleapis.com/v1beta/properties/{prop}/keyEvents"
    existing_data = _request(base + "?pageSize=200", access_token=edit_token)
    existing = {x.get("eventName") for x in existing_data.get("keyEvents", [])}
    created, kept, errors = [], [], {}
    for name in event_names:
        if name in existing:
            kept.append(name)
            continue
        try:
            _request(base, method="POST", payload={"eventName": name, "countingMethod": "ONCE_PER_EVENT"},
                     access_token=edit_token)
            created.append(name)
        except Exception as exc:
            errors[name] = str(exc)
    return {"property_id": prop, "criadas": created, "ja_existiam": kept, "erros": errors,
            "metas": list(event_names)}


def gtm_summary(days=30):
    e = _env(); account = _digits(e.get("GOOGLE_GTM_ACCOUNT_ID")); container = _digits(e.get("GOOGLE_GTM_CONTAINER_ID"))
    if not account or not container:
        raise RuntimeError("Informe Account ID e Container ID do Google Tag Manager.")
    base = f"https://tagmanager.googleapis.com/tagmanager/v1/accounts/{account}/containers/{container}"
    token = _gtm_read_token()
    tags = _request(base + "/tags", access_token=token).get("tags", [])
    triggers = _request(base + "/triggers", access_token=token).get("triggers", [])
    return {"tags": len(tags), "triggers": len(triggers), "container": container}


def gtm_resources():
    """Lista contas e containers GTM acessíveis, sem editar nada."""
    token = _gtm_read_token()
    data = _request("https://tagmanager.googleapis.com/tagmanager/v1/accounts", access_token=token)
    contas = []
    for account in data.get("accounts", []):
        aid = _digits(account.get("accountId", ""))
        if not aid:
            continue
        containers = _request(
            f"https://tagmanager.googleapis.com/tagmanager/v1/accounts/{aid}/containers",
            access_token=token,
        ).get("containers", [])
        contas.append({
            "id": aid, "nome": account.get("name", ""),
            "containers": [{"id": _digits(x.get("containerId", "")), "nome": x.get("name", ""),
                            "public_id": x.get("publicId", "")}
                           for x in containers],
        })
    return {"contas": contas}


def ads_report(days=30):
    e = _env(); customer = _digits(e.get("GOOGLE_ADS_CUSTOMER_ID")); token = e.get("GOOGLE_ADS_DEVELOPER_TOKEN", "").strip()
    if not customer or not token:
        raise RuntimeError("Informe Customer ID e Developer Token do Google Ads.")
    query = ("SELECT metrics.impressions, metrics.clicks, metrics.cost_micros, metrics.conversions "
             "FROM customer WHERE segments.date DURING LAST_30_DAYS")
    headers = {"developer-token": token}
    login = _digits(e.get("GOOGLE_ADS_LOGIN_CUSTOMER_ID"))
    if login:
        headers["login-customer-id"] = login
    rows = _request(f"https://googleads.googleapis.com/v20/customers/{customer}/googleAds:searchStream",
                    method="POST", payload={"query": query}, headers=headers)
    totals = {"impressoes": 0, "cliques": 0, "custo": 0.0, "conversoes": 0.0, "dias": days}
    for batch in rows if isinstance(rows, list) else [rows]:
        for row in batch.get("results", []):
            m = row.get("metrics", {})
            totals["impressoes"] += int(m.get("impressions", 0)); totals["cliques"] += int(m.get("clicks", 0))
            totals["custo"] += int(m.get("costMicros", 0)) / 1_000_000; totals["conversoes"] += float(m.get("conversions", 0))
    totals["custo"] = round(totals["custo"], 2); totals["conversoes"] = round(totals["conversoes"], 2)
    return totals


def sync(days=30):
    result = {"status": status(), "ga4": None, "gtm": None, "ads": None, "erros": {}}
    for name, fn, ready in (("ga4", ga4_report, result["status"]["ga4_pronto"]),
                            ("gtm", gtm_summary, result["status"]["gtm_pronto"]),
                            ("ads", ads_report, result["status"]["ads_pronto"])):
        if not ready:
            continue
        try:
            result[name] = fn(days)
        except Exception as exc:
            result["erros"][name] = str(exc)
    result["atualizado_em"] = __import__("datetime").datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    return result


def utm_url(url, campaign, source="google", medium="cpc"):
    parts = urllib.parse.urlsplit(url)
    query = dict(urllib.parse.parse_qsl(parts.query, keep_blank_values=True))
    query.update({"utm_source": source, "utm_medium": medium, "utm_campaign": campaign})
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, parts.path,
                                    urllib.parse.urlencode(query), parts.fragment))
