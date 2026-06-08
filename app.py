"""
Azure Sponsorship Monitor — Multi-Subscription Edition
~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~~
Extiende el monitor original para gestionar más de 10 suscripciones
con un único Service Principal compartido.

Nuevas rutas:
  GET /             → Vista individual (selector de suscripción + dashboard)
  GET /overview     → Panel consolidado con todas las suscripciones en paralelo

Config de suscripciones:
  subscriptions.json  →  lista de objetos {id, name, tags?, budget?}

Variables de entorno (.env):
  AZURE_TENANT_ID, AZURE_CLIENT_ID, AZURE_CLIENT_SECRET  (SP compartido)
  AZURE_OFFER_ID, AZURE_CURRENCY, AZURE_LOCALE, AZURE_REGION_INFO
  FLASK_SECRET_KEY
  MAX_WORKERS  (opcional, default 8)  — hilos para fetching en paralelo
"""

from __future__ import annotations

import json
import logging
import os
import uuid
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from functools import wraps
from pathlib import Path
from typing import Optional

import msal
from azure.identity import DefaultAzureCredential
from azure.mgmt.commerce import UsageManagementClient
from azure.mgmt.subscription import SubscriptionClient
from dotenv import load_dotenv
from flask import Flask, render_template, request, redirect, url_for, session

from apscheduler.schedulers.background import BackgroundScheduler

from werkzeug.middleware.proxy_fix import ProxyFix

load_dotenv()

app = Flask(__name__)
app.secret_key = os.environ.get("FLASK_SECRET_KEY", "dev-secret-change-me")
# Necesario para que url_for genere https cuando está detrás de un proxy/LB
app.wsgi_app = ProxyFix(app.wsgi_app, x_proto=1, x_host=1)
log = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# SSO con MSAL (Microsoft Authentication Library)
# ---------------------------------------------------------------------------

_SCOPES        = ["User.Read"]
_REDIRECT_PATH = "/auth/callback"


def _sso_enabled() -> bool:
    """Comprueba en runtime si SSO está configurado."""
    return bool(
        os.environ.get("AZURE_TENANT_ID") and
        os.environ.get("AZURE_CLIENT_ID") and
        os.environ.get("AZURE_CLIENT_SECRET")
    )


def _get_msal_app() -> msal.ConfidentialClientApplication:
    return msal.ConfidentialClientApplication(
        os.environ.get("AZURE_CLIENT_ID", ""),
        authority=f"https://login.microsoftonline.com/{os.environ.get('AZURE_TENANT_ID', '')}",
        client_credential=os.environ.get("AZURE_CLIENT_SECRET", ""),
    )


def _get_current_user() -> dict:
    """Devuelve el usuario de la sesión o un usuario anónimo si SSO está desactivado."""
    if not _sso_enabled():
        return {"name": "Dev User", "email": "", "authenticated": False}
    user = session.get("user")
    if user:
        return {**user, "authenticated": True}
    return {"name": "", "email": "", "authenticated": False}


def login_required(f):
    """Decorador que redirige al login si el usuario no está autenticado."""
    @wraps(f)
    def decorated(*args, **kwargs):
        if _sso_enabled() and not session.get("user"):
            session["next_url"] = request.url
            return redirect(url_for("login"))
        return f(*args, **kwargs)
    return decorated


@app.route("/login")
def login():
    if not _sso_enabled():
        return redirect(url_for("overview"))
    session["state"] = str(uuid.uuid4())
    auth_url = _get_msal_app().get_authorization_request_url(
        _SCOPES,
        state=session["state"],
        redirect_uri=url_for("auth_callback", _external=True),
    )
    return redirect(auth_url)


@app.route("/auth/callback")
def auth_callback():
    if request.args.get("state") != session.get("state"):
        return redirect(url_for("overview"))
    if "error" in request.args:
        return f"Error de autenticación: {request.args.get('error_description')}", 400

    result = _get_msal_app().acquire_token_by_authorization_code(
        request.args["code"],
        scopes=_SCOPES,
        redirect_uri=url_for("auth_callback", _external=True),
    )
    if "error" in result:
        return f"Error obteniendo token: {result.get('error_description')}", 400

    claims = result.get("id_token_claims", {})
    session["user"] = {
        "name":  claims.get("name") or claims.get("preferred_username", ""),
        "email": claims.get("preferred_username") or claims.get("email", ""),
        "oid":   claims.get("oid", ""),
    }
    next_url = session.pop("next_url", url_for("overview"))
    return redirect(next_url)


@app.route("/logout")
def logout():
    session.clear()
    tenant_id = os.environ.get("AZURE_TENANT_ID", "")
    logout_url = (
        f"https://login.microsoftonline.com/{tenant_id}/oauth2/v2.0/logout"
        f"?post_logout_redirect_uri={url_for('overview', _external=True)}"
    )
    return redirect(logout_url)


@app.context_processor
def inject_user():
    return {
        "current_user": _get_current_user(),
        "sso_enabled":  _sso_enabled(),
        "app_version":  os.environ.get("APP_VERSION", "dev"),
        "build_date":   os.environ.get("BUILD_DATE", ""),
    }


# ---------------------------------------------------------------------------
# Fecha fija de inicio y caché del overview automático
# ---------------------------------------------------------------------------

# Fecha de inicio fija — siempre desde el 1 de agosto de 2025
FIXED_START = datetime(2025, 8, 1, tzinfo=timezone.utc)

def _today() -> datetime:
    return datetime.now(timezone.utc).replace(hour=0, minute=0, second=0, microsecond=0)

def _yesterday() -> datetime:
    return _today() - timedelta(days=1)
# Caché compartida del overview (actualizada cada 3h por el scheduler)
_OVERVIEW_CACHE: dict = {
    "sub_summaries":   [],
    "grand_total_all": 0.0,
    "avg_daily_all":   0.0,
    "period_days":     0,
    "start":           FIXED_START.strftime("%Y-%m-%d"),
    "end":             "",
    "last_updated":    None,   # datetime o None si aún no se ha cargado
    "loading":         False,  # True mientras se está ejecutando el fetch
    "error":           None,
}

# ---------------------------------------------------------------------------
# Auto-discovery de suscripciones por Offer ID
# ---------------------------------------------------------------------------

# Offer ID que identifica las suscripciones de beneficios Partner/Sponsorship
MONITORED_OFFER_IDS: list[str] = [
    o.strip()
    for o in os.environ.get("AZURE_OFFER_IDS", "Sponsored").split(",")
    if o.strip()
]

def discover_subscriptions() -> list[dict]:
    """
    Descubre automáticamente todas las suscripciones a las que tiene acceso
    el SP y las filtra por AZURE_OFFER_IDS (por defecto Sponsored).
    No requiere ningún archivo en el repo — cero secretos en Git.
    """
    result: list[dict] = []

    try:
        credential = DefaultAzureCredential()
        sub_client = SubscriptionClient(credential)

        for sub in sub_client.subscriptions.list():
            policies  = sub.subscription_policies or {}
            quota_raw = getattr(policies, "quota_id", "") or ""
            quota_id  = quota_raw.split("_")[0] if "_" in quota_raw else quota_raw

            if quota_id not in MONITORED_OFFER_IDS:
                continue

            if str(sub.state).lower() not in ("enabled", "warned"):
                log.debug("Omitiendo suscripción %s en estado %s", sub.display_name, sub.state)
                continue

            result.append({
                "id":       sub.subscription_id,
                "name":     sub.display_name or sub.subscription_id,
                "tags":     [],
                "budget":   None,
                "currency": os.environ.get("AZURE_CURRENCY", "EUR"),
            })

        if not result:
            log.warning(
                "Auto-discovery: no se encontraron suscripciones con offer %s.",
                MONITORED_OFFER_IDS,
            )

        log.info("Auto-discovery: %d suscripciones encontradas.", len(result))

    except Exception as exc:
        log.error("Auto-discovery falló: %s", exc)

    return result


# Carga inicial al arrancar — el scheduler refresca esto junto con los datos
SUBSCRIPTIONS: list[dict] = discover_subscriptions()
SUBSCRIPTION_MAP: dict[str, dict] = {s["id"]: s for s in SUBSCRIPTIONS}

# ---------------------------------------------------------------------------
# Azure helpers
# ---------------------------------------------------------------------------

def _get_client(subscription_id: str) -> UsageManagementClient:
    """Devuelve un UsageManagementClient autenticado para la suscripción dada."""
    credential = DefaultAzureCredential()
    return UsageManagementClient(credential, subscription_id)


# Cache en memoria para detalles de suscripción (no cambian frecuentemente)
_SUB_DETAILS_CACHE: dict[str, dict] = {}

# Cache global del RateCard — idéntico para todas las subs, se fetcha una sola vez
_RATE_CARD_CACHE: dict[str, dict] = {}

_ARM_BASE = "https://management.azure.com"


def _arm_get(token: str, path: str, api_version: str, params: Optional[dict] = None) -> Optional[dict]:
    """Hace un GET al ARM REST API. Devuelve el JSON o None si falla."""
    import urllib.request, urllib.parse, ssl
    url = f"{_ARM_BASE}{path}?api-version={api_version}"
    if params:
        url += "&" + urllib.parse.urlencode(params)
    req = urllib.request.Request(url, headers={"Authorization": f"Bearer {token}", "Accept": "application/json"})
    try:
        ctx = ssl.create_default_context()
        with urllib.request.urlopen(req, context=ctx, timeout=15) as resp:
            return json.loads(resp.read().decode())
    except Exception as exc:
        log.debug("ARM GET %s failed: %s", path, exc)
        return None


def fetch_subscription_details(subscription_id: str) -> dict:
    """
    Obtiene todos los metadatos visibles en el portal Azure → Subscription → Properties:
      - status            : Active / Warned / Disabled
      - display_name      : nombre oficial en Azure
      - quota_id          : Offer ID (MS-AZR-0036P)
      - offer_name        : "Azure Sponsorship" (mapeado desde quota_id)
      - spending_limit    : On / Off / CurrentPeriodOff
      - tenant_id         : GUID del tenant
      - currency          : EUR / USD / etc. (por suscripción, desde billing API)
      - billing_period    : "3/5/2026 – 2/6/2026"
      - purchase_date     : "3/3/2025"
      - account_admin     : email del Account Administrator
      - billing_account_id: GUID de la cuenta de facturación
      - azure_tags        : tags asignados a la suscripción en Azure
    """
    if subscription_id in _SUB_DETAILS_CACHE:
        return _SUB_DETAILS_CACHE[subscription_id]

    # Mapa de offer IDs → nombres legibles
    _OFFER_NAMES = {
        "MS-AZR-0036P": "Azure Sponsorship",
        "MS-AZR-0044P": "Azure for Students",
        "MS-AZR-0022P": "Enterprise Dev/Test",
        "MS-AZR-0017P": "Enterprise Agreement",
        "MS-AZR-0148P": "Enterprise Dev/Test",
        "MS-AZR-0029P": "Visual Studio Enterprise",
        "MS-AZR-0023P": "Visual Studio Professional",
        "MS-AZR-0059P": "Visual Studio Test Professional",
        "MS-AZR-0060P": "MSDN Platforms",
    }

    result: dict = {
        "status": None,
        "display_name": None,
        "quota_id": None,
        "offer_name": None,
        "spending_limit": None,
        "tenant_id": None,
        "currency": None,
        "billing_period": None,
        "billing_period_start": None,
        "billing_period_end": None,
        "purchase_date": None,
        "account_admin": None,
        "billing_account_id": None,
        "azure_tags": {},
    }

    try:
        credential = DefaultAzureCredential()
        token = credential.get_token(f"{_ARM_BASE}/.default").token

        # ── 1. Detalles básicos (ARM Subscriptions API) ──────────────────
        sub_path = f"/subscriptions/{subscription_id}"
        data = _arm_get(token, sub_path, "2022-12-01")
        if data:
            result["status"]       = data.get("state", "")
            result["display_name"] = data.get("displayName", "")
            result["tenant_id"]    = data.get("tenantId", "")
            result["azure_tags"]   = data.get("tags") or {}
            policies = data.get("subscriptionPolicies") or {}
            quota_id = policies.get("quotaId", "")
            # quotaId suele venir como "MS-AZR-0036P_2019-04-01" — normalizamos
            quota_clean = quota_id.split("_")[0] if "_" in quota_id else quota_id
            result["quota_id"]      = quota_clean
            result["offer_name"]    = _OFFER_NAMES.get(quota_clean, quota_clean)
            sl = policies.get("spendingLimit", "")
            result["spending_limit"] = sl if isinstance(sl, str) else str(sl)

        # ── 2. Billing periods (período actual + purchase date) ──────────
        bp_data = _arm_get(
            token,
            f"{sub_path}/providers/Microsoft.Billing/billingPeriods",
            "2018-03-01-preview",
            {"$top": "1", "$orderby": "billingPeriodEndDate desc"},
        )
        if bp_data:
            periods = bp_data.get("value") or []
            if periods:
                props = periods[0].get("properties") or {}
                start = props.get("billingPeriodStartDate", "")
                end   = props.get("billingPeriodEndDate", "")
                result["billing_period_start"] = start
                result["billing_period_end"]   = end
                if start and end:
                    result["billing_period"] = f"{start} – {end}"

        # ── 3. Billing subscription → currency, purchaseDate, accountAdmin ──
        # Intentamos dos API versions distintas por compatibilidad
        for bsub_api in ("2024-04-01", "2020-05-01", "2019-10-01-preview"):
            bsub_data = _arm_get(
                token,
                f"{sub_path}/providers/Microsoft.Billing/billingAccounts",
                bsub_api,
            )
            if bsub_data:
                accounts = bsub_data.get("value") or []
                if accounts:
                    acc = accounts[0]
                    acc_props = acc.get("properties") or {}
                    result["billing_account_id"] = acc.get("name") or acc.get("id", "").split("/")[-1]
                    if not result["currency"]:
                        result["currency"] = acc_props.get("currency") or acc_props.get("displayCurrency", "")
                    # Account admin puede estar en soldTo o enrollmentDetails
                    sold_to = acc_props.get("soldTo") or {}
                    if sold_to.get("email"):
                        result["account_admin"] = sold_to["email"]
                    if result["currency"] or result["account_admin"]:
                        break

        # ── 4. Billing subscription directa (currency + purchaseDate) ────
        if not result["currency"] or not result["account_admin"]:
            for bsub_api in ("2024-04-01", "2020-05-01"):
                bsub_direct = _arm_get(
                    token,
                    f"{sub_path}/providers/Microsoft.Billing/billingSubscriptions/default",
                    bsub_api,
                )
                if bsub_direct:
                    props = bsub_direct.get("properties") or bsub_direct
                    if not result["currency"]:
                        result["currency"] = (
                            props.get("currency")
                            or props.get("displayCurrency")
                            or props.get("invoiceSectionDisplayName", "")[:3]
                        )
                    if not result["purchase_date"]:
                        result["purchase_date"] = props.get("subscriptionEnrollmentAccountStatus", "")
                    if result["currency"]:
                        break

        # ── 5. Billing subscription list (para currency y purchaseDate) ──
        if not result["currency"]:
            for acct_api in ("2020-05-01", "2019-10-01-preview"):
                bsub_list = _arm_get(
                    token,
                    f"{sub_path}/providers/Microsoft.Billing/billingSubscriptions",
                    acct_api,
                )
                if bsub_list:
                    subs_list = bsub_list.get("value") or []
                    if subs_list:
                        s0 = subs_list[0]
                        props = s0.get("properties") or s0
                        if not result["currency"]:
                            result["currency"] = props.get("currency") or props.get("displayCurrency", "")
                        if not result["purchase_date"]:
                            result["purchase_date"] = props.get("purchaseDate") or props.get("startDate", "")
                        if not result["account_admin"]:
                            result["account_admin"] = props.get("accountAdminEmail") or props.get("invoiceEmailOptIn", "")
                        if result["currency"]:
                            break

        # Normalizar status a cadena
        if result["status"]:
            # "Enabled" → "Active" para coincidir con lo que muestra el portal
            result["status"] = {"Enabled": "Active", "Warned": "Warned", "Disabled": "Disabled"}.get(
                result["status"], result["status"]
            )

    except Exception as exc:
        log.warning("fetch_subscription_details failed for %s: %s", subscription_id, exc)

    _SUB_DETAILS_CACHE[subscription_id] = result
    return result



def fetch_rate_card(subscription_id: str, currency: Optional[str] = None) -> dict[str, dict]:
    """
    Obtiene el RateCard. Como el RateCard es idéntico para todas las suscripciones
    con el mismo offer/currency/region, se cachea globalmente tras la primera llamada.
    Incluye reintentos con backoff para evitar timeouts de la API de Commerce.
    """
    offer_id = os.environ.get("AZURE_OFFER_ID", "MS-AZR-0036P")
    currency = currency or os.environ.get("AZURE_CURRENCY", "EUR")
    locale   = os.environ.get("AZURE_LOCALE", "es-ES")
    region   = os.environ.get("AZURE_REGION_INFO", "ES")

    cache_key = f"{offer_id}:{currency}:{locale}:{region}"

    # Devolver desde caché si ya lo tenemos
    if cache_key in _RATE_CARD_CACHE:
        log.debug("RateCard desde caché para %s", cache_key)
        return _RATE_CARD_CACHE[cache_key]

    rate_filter = (
        f"OfferDurableId eq '{offer_id}' and Currency eq '{currency}' "
        f"and Locale eq '{locale}' and RegionInfo eq '{region}'"
    )

    # Reintentos con backoff exponencial
    import time
    for attempt in range(3):
        try:
            client = _get_client(subscription_id)
            card = client.rate_card.get(filter=rate_filter)
            break
        except Exception as exc:
            if attempt < 2:
                wait = 5 * (2 ** attempt)   # 5s, 10s
                log.warning("RateCard intento %d falló, reintentando en %ds: %s", attempt + 1, wait, exc)
                time.sleep(wait)
            else:
                log.warning("RateCard falló tras 3 intentos para %s", cache_key, exc_info=True)
                _RATE_CARD_CACHE[cache_key] = {}
                return {}

    rates: dict[str, dict] = {}
    for meter in card.meters or []:
        mid = getattr(meter, "meter_id", None)
        if not mid:
            continue
        raw_rates = getattr(meter, "meter_rates", {}) or {}
        rates[mid] = {
            "meter_rates": {float(k): v for k, v in raw_rates.items()},
            "included_quantity": getattr(meter, "included_quantity", 0) or 0,
        }

    _RATE_CARD_CACHE[cache_key] = rates
    log.info("RateCard cargado y cacheado: %d meters (%s)", len(rates), cache_key)
    return rates


def calculate_cost(quantity: float, rate_info: dict) -> float:
    """Calcula el coste estimado aplicando tarifas por tramos."""
    if not rate_info or not rate_info.get("meter_rates"):
        return 0.0
    included = rate_info.get("included_quantity", 0) or 0
    billable = max(0.0, quantity - included)
    if billable == 0:
        return 0.0
    tiers = sorted(rate_info["meter_rates"].items())
    cost = 0.0
    remaining = billable
    for i, (threshold, rate) in enumerate(tiers):
        if remaining <= 0:
            break
        if i + 1 < len(tiers):
            next_threshold = tiers[i + 1][0]
            tier_qty = min(remaining, next_threshold - threshold)
        else:
            tier_qty = remaining
        cost += tier_qty * rate
        remaining -= tier_qty
    return cost


# BRSDT decoding (sin cambios respecto al original)
_BRSDT_RATE_LABELS: dict[float, str] = {
    0.10:  "Sora 2 · video ($/sec)",
    0.17:  "GPT-5.2 · cached input",
    0.25:  "GPT-5.4 · cached input",
    0.50:  "GPT-5.4-pp · cached input",
    1.75:  "GPT-5.2 · input",
    2.50:  "GPT-5.4 · input",
    5.00:  "GPT-5.4-pp · input",
    14.00: "GPT-5.2 · output",
    15.00: "GPT-5.4 · output",
    30.00: "GPT-5.4-pro · input / GPT-5.4-pp · output",
    180.00:"GPT-5.4-pro · output",
}
_BRSDT_OTHER_RATES: frozenset[float] = frozenset({0.04})
_BRSDT_PREFIX       = "Daily_BRSDT_"
_BRSDT_AI_CATEGORY  = "Azure OpenAI"
_BRSDT_OTHER_CATEGORY = "Sponsored (other)"
_BRSDT_RATE_TOLERANCE = 0.05


def _match_brsdt_rate(implied_rate: float) -> tuple:
    for other_rate in _BRSDT_OTHER_RATES:
        if abs(implied_rate - other_rate) < 0.005:
            return True, None
    best_label: Optional[str] = None
    best_distance = float("inf")
    for rate, label in _BRSDT_RATE_LABELS.items():
        rel_distance = abs(implied_rate - rate) / rate
        if rel_distance < _BRSDT_RATE_TOLERANCE and rel_distance < best_distance:
            best_distance = rel_distance
            best_label = label
    if best_label is not None:
        return True, best_label
    return False, None


def _is_brsdt_row(rec: dict) -> bool:
    has_brsdt_id = (
        rec.get("meter_name", "").startswith(_BRSDT_PREFIX)
        or rec.get("name", "").startswith(_BRSDT_PREFIX)
    )
    if not has_brsdt_id:
        return False
    real_name = rec.get("meter_name", "")
    if real_name and not real_name.startswith(_BRSDT_PREFIX):
        return False
    return True


def _decode_brsdt(rec: dict, cost: float) -> dict:
    if not _is_brsdt_row(rec):
        return rec
    qty = float(rec.get("quantity") or 0)
    if qty == 0:
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
        return rec
    if cost == 0:
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
        rec["meter_name"] = "BRSDT (unrated)"
        return rec
    implied_rate = round(cost / qty, 2)
    rec["brsdt_implied_rate"] = implied_rate
    matched, label = _match_brsdt_rate(implied_rate)
    if matched and label is not None:
        rec["is_brsdt"] = True
        rec["meter_name"] = label
        rec["meter_category"] = _BRSDT_AI_CATEGORY
    elif matched:
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
    else:
        log.info("BRSDT unmatched rate $%.2f/unit (meter_id=%s)", implied_rate, rec.get("meter_id", ""))
        rec["meter_name"] = f"BRSDT ${implied_rate:.2f}/unit"
        rec["meter_category"] = _BRSDT_OTHER_CATEGORY
    return rec


def _format_cost(cost: Optional[float]) -> str:
    if not cost:
        return "0.00"
    return f"{cost:.2f}"


def _currency_symbol(code: str) -> str:
    return {"EUR": "€", "USD": "$", "GBP": "£", "CHF": "CHF"}.get(str(code).upper(), code)


app.jinja_env.filters["format_cost"]      = _format_cost
app.jinja_env.filters["currency_symbol"]  = _currency_symbol


def fetch_usage(
    subscription_id: str,
    start_time: datetime,
    end_time: datetime,
    granularity: str = "Daily",
    show_details: bool = False,
) -> list[dict]:
    """Obtiene los usage aggregates para la suscripción indicada."""
    client = _get_client(subscription_id)
    results = client.usage_aggregates.list(
        reported_start_time=start_time,
        reported_end_time=end_time,
        show_details=show_details,
        aggregation_granularity=granularity,
    )
    records = []
    for item in results:
        records.append({
            "id":                 item.id,
            "name":               item.name,
            "meter_id":           getattr(item, "meter_id", "") or "",
            "meter_name":         getattr(item, "meter_name", "") or "",
            "meter_category":     getattr(item, "meter_category", "") or "",
            "meter_sub_category": getattr(item, "meter_sub_category", "") or "",
            "unit":               getattr(item, "unit", "") or "",
            "quantity":           getattr(item, "quantity", 0),
            "api_cost":           getattr(item, "cost", None),   # coste real calculado por Azure
            "usage_start":        getattr(item, "usage_start_time", ""),
            "usage_end":          getattr(item, "usage_end_time", ""),
            "subscription_id":    subscription_id,
            "info_fields":        getattr(item, "info_fields", {}),
        })
    return records


# ---------------------------------------------------------------------------
# Procesamiento de registros (lógica compartida entre / y /overview)
# ---------------------------------------------------------------------------

def process_records(records: list[dict], rate_map: dict, period_days: int) -> dict:
    """
    Aplica costes, decodifica BRSDT y calcula todos los aggregados.
    Devuelve un dict con todos los valores necesarios para los templates.
    """
    total_quantity_by_meter: dict[str, float] = {}
    total_cost_by_meter: dict[str, float]     = {}
    unit_by_meter: dict[str, str]             = {}
    grand_total_cost = 0.0
    chart_daily: dict[str, dict[str, float]]     = {}
    svc_chart_daily: dict[str, dict[str, float]] = {}
    svc_cost: dict[str, float]     = {}
    svc_quantity: dict[str, float] = {}
    svc_unit: dict[str, str]       = {}
    brsdt_detail_keys: set[str]     = set()
    brsdt_unmatched_rates: set[float] = set()
    has_brsdt = False

    for rec in records:
        qty      = float(rec["quantity"] or 0)
        meter_id = rec.get("meter_id", "")

        # Usar el coste ya calculado por Azure si está disponible (más preciso)
        # Solo usar RateCard como fallback si la API no devuelve coste
        api_cost = rec.get("api_cost")
        if api_cost is not None:
            cost = float(api_cost)
        else:
            rate_info = rate_map.get(meter_id)
            cost = calculate_cost(qty, rate_info) if rate_info else 0.0
        rec["cost"] = cost
        rec = _decode_brsdt(rec, cost)
        if not rec.get("meter_category"):
            rec["meter_category"] = "Unknown"

        is_brsdt   = rec.get("is_brsdt", False)
        detail_key = rec["meter_name"] or rec["name"] or "Unknown"

        total_quantity_by_meter[detail_key] = total_quantity_by_meter.get(detail_key, 0.0) + qty
        if detail_key not in unit_by_meter:
            unit_by_meter[detail_key] = rec.get("unit", "")
        total_cost_by_meter[detail_key] = total_cost_by_meter.get(detail_key, 0.0) + cost
        grand_total_cost += cost

        svc_key = _BRSDT_AI_CATEGORY if is_brsdt else detail_key
        svc_cost[svc_key] = svc_cost.get(svc_key, 0.0) + cost
        if not is_brsdt:
            svc_quantity[svc_key] = svc_quantity.get(svc_key, 0.0) + qty
        if svc_key not in svc_unit:
            svc_unit[svc_key] = rec.get("unit", "")

        if is_brsdt:
            has_brsdt = True
            brsdt_detail_keys.add(detail_key)
            if detail_key.startswith("BRSDT $"):
                brsdt_unmatched_rates.add(rec.get("brsdt_implied_rate", 0.0))

        _d = str(rec["usage_start"])[:10]
        if detail_key not in chart_daily:
            chart_daily[detail_key] = {}
        chart_daily[detail_key][_d] = chart_daily[detail_key].get(_d, 0.0) + cost

        if svc_key not in svc_chart_daily:
            svc_chart_daily[svc_key] = {}
        svc_chart_daily[svc_key][_d] = svc_chart_daily[svc_key].get(_d, 0.0) + cost

    chart_labels_list = sorted({str(r["usage_start"])[:10] for r in records})
    average_daily_cost = grand_total_cost / period_days if period_days > 0 else 0.0

    sorted_meters   = sorted(total_quantity_by_meter, key=lambda m: total_cost_by_meter.get(m, 0.0), reverse=True)
    sorted_quantity = {m: total_quantity_by_meter[m] for m in sorted_meters}
    sorted_cost     = {m: total_cost_by_meter.get(m, 0.0) for m in sorted_meters}
    chart_meter_order = [m for m in sorted_meters if total_cost_by_meter.get(m, 0.0) > 0]
    chart_series_data = {
        m: [chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in chart_meter_order
    }

    svc_sorted_meters     = sorted(svc_cost, key=lambda m: svc_cost.get(m, 0.0), reverse=True)
    svc_sorted_cost_dict  = {m: svc_cost.get(m, 0.0) for m in svc_sorted_meters}
    svc_sorted_qty_dict   = {m: svc_quantity.get(m, 0.0) for m in svc_sorted_meters}
    svc_meter_order       = [m for m in svc_sorted_meters if svc_cost.get(m, 0.0) > 0]
    svc_chart_series      = {
        m: [svc_chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in svc_meter_order
    }

    ai_sorted_meters = [m for m in sorted_meters if m in brsdt_detail_keys]
    ai_quantity = {m: total_quantity_by_meter[m] for m in ai_sorted_meters}
    ai_cost_map = {m: total_cost_by_meter.get(m, 0.0) for m in ai_sorted_meters}
    ai_unit     = {m: unit_by_meter.get(m, "") for m in ai_sorted_meters}
    ai_meter_order = [m for m in ai_sorted_meters if total_cost_by_meter.get(m, 0.0) > 0]
    ai_chart_series = {
        m: [chart_daily.get(m, {}).get(d, 0.0) for d in chart_labels_list]
        for m in ai_meter_order
    }
    ai_total_cost = sum(ai_cost_map.values())

    # Cache efficiency
    cache_efficiency: dict[str, float] = {}
    cache_hits: dict[str, float]   = {}
    regular_inputs: dict[str, float] = {}
    for key, qty in total_quantity_by_meter.items():
        if key.endswith("· cached input"):
            model = key.replace("· cached input", "").strip()
            cache_hits[model] = cache_hits.get(model, 0.0) + qty
        elif key.endswith("· input"):
            model = key.replace("· input", "").strip()
            regular_inputs[model] = regular_inputs.get(model, 0.0) + qty
    for model in cache_hits:
        total_in = cache_hits[model] + regular_inputs.get(model, 0.0)
        if total_in > 0:
            cache_efficiency[model] = cache_hits[model] / total_in

    # ── Proyección para días sin datos (entre último día real y hoy) ─────────
    today_str      = _today().strftime("%Y-%m-%d")
    projected_days: list[str]  = []
    projected_cost_per_day = 0.0
    projected_total = 0.0

    if chart_labels_list:
        from datetime import date
        last_real_day  = chart_labels_list[-1]
        last_date      = date.fromisoformat(last_real_day)
        today_date     = date.fromisoformat(today_str)
        gap_days       = (today_date - last_date).days

        # Solo proyectar si el gap es razonable (≤4 días = retraso normal de la API)
        # Si el gap es mayor, los datos están incompletos y no proyectamos
        if 0 < gap_days <= 3:
            # Media de los últimos 7 días con datos para suavizar picos
            recent_labels = chart_labels_list[-7:]
            daily_sums = []
            for d in recent_labels:
                day_total = sum(
                    (svc_chart_daily.get(svc, {}).get(d, 0.0))
                    for svc in svc_chart_daily
                )
                if day_total > 0:
                    daily_sums.append(day_total)

            if daily_sums:
                projected_cost_per_day = sum(daily_sums) / len(daily_sums)

            for i in range(1, gap_days + 1):
                proj_day = (last_date + timedelta(days=i)).isoformat()
                projected_days.append(proj_day)
                projected_total += projected_cost_per_day

        elif gap_days > 3:
            # Más de 3 días sin datos = sin gasto real, proyección $0
            projected_cost_per_day = 0.0
            for i in range(1, gap_days + 1):
                proj_day = (last_date + timedelta(days=i)).isoformat()
                projected_days.append(proj_day)
            projected_total = 0.0

    projected_grand_total = grand_total_cost + projected_total

    return dict(
        records=records,
        total_quantity_by_meter=sorted_quantity,
        total_cost_by_meter=sorted_cost,
        unit_by_meter=unit_by_meter,
        grand_total_cost=grand_total_cost,
        average_daily_cost=average_daily_cost,
        chart_labels=chart_labels_list,
        chart_series_data=chart_series_data,
        chart_meter_order=chart_meter_order,
        cache_efficiency=cache_efficiency,
        has_brsdt=has_brsdt,
        svc_quantity=svc_sorted_qty_dict,
        svc_cost=svc_sorted_cost_dict,
        svc_unit=svc_unit,
        svc_chart_series=svc_chart_series,
        svc_meter_order=svc_meter_order,
        ai_quantity=ai_quantity,
        ai_cost=ai_cost_map,
        ai_unit=ai_unit,
        ai_chart_series=ai_chart_series,
        ai_meter_order=ai_meter_order,
        ai_total_cost=ai_total_cost,
        brsdt_rate_labels=_BRSDT_RATE_LABELS,
        brsdt_other_rates=_BRSDT_OTHER_RATES,
        brsdt_unmatched_rates=sorted(brsdt_unmatched_rates),
        # Proyección
        projected_days=projected_days,
        projected_cost_per_day=projected_cost_per_day,
        projected_total=projected_total,
        projected_grand_total=projected_grand_total,
    )


# ---------------------------------------------------------------------------
# Fetching en paralelo para el overview
# ---------------------------------------------------------------------------

def _fetch_subscription_summary(
    sub: dict,
    start_dt: datetime,
    end_dt: datetime,
    period_days: int,
    rate_map: Optional[dict] = None,
) -> dict:
    """
    Tarea ejecutada en un hilo por cada suscripción.
    Recibe el rate_map ya cargado (compartido) para no llamar al RateCard por cada sub.
    """
    sub_id   = sub["id"]
    sub_name = sub.get("name", sub_id)
    budget   = sub.get("budget")
    tags     = sub.get("tags", [])
    sub_currency = sub.get("currency", "")

    try:
        records   = fetch_usage(sub_id, start_dt, end_dt)
        # Usar el rate_map compartido; si no viene, intentar obtenerlo (fallback)
        if rate_map is None:
            effective_currency = sub_currency or os.environ.get("AZURE_CURRENCY", "EUR")
            rate_map = fetch_rate_card(sub_id, currency=effective_currency)
        processed = process_records(records, rate_map, period_days)

        # Metadatos enriquecidos de la suscripción
        details = fetch_subscription_details(sub_id)

        grand_total = processed["grand_total_cost"]
        avg_daily   = processed["average_daily_cost"]
        top_services = sorted(
            processed["svc_cost"].items(),
            key=lambda x: x[1],
            reverse=True
        )[:5]

        # Serie diaria agregada (todos los servicios)
        all_dates = processed["chart_labels"]
        daily_totals = {}
        for svc, series in processed["svc_chart_series"].items():
            for i, d in enumerate(all_dates):
                daily_totals[d] = daily_totals.get(d, 0.0) + series[i]

        # Proyección
        proj_days    = processed["projected_days"]
        proj_per_day = processed["projected_cost_per_day"]
        proj_total   = processed["projected_total"]
        proj_grand   = processed["projected_grand_total"]

        budget_pct = (proj_grand / budget * 100) if budget else None

        return {
            "id":            sub_id,
            "name":          sub_name,
            "tags":          tags,
            "budget":        budget,
            "budget_pct":    budget_pct,
            "total":         grand_total,
            "avg_daily":     avg_daily,
            "top_services":  top_services,
            "daily_totals":  daily_totals,
            "all_dates":     all_dates,
            "rate_card_ok":  bool(rate_map),
            "error":         None,
            # Proyección
            "projected_days":         proj_days,
            "projected_cost_per_day": proj_per_day,
            "projected_total":        proj_total,
            "projected_grand_total":  proj_grand,
            # ── Metadatos enriquecidos ──
            "status":         details.get("status") or "Unknown",
            "display_name":   details.get("display_name") or sub_name,
            "quota_id":       details.get("quota_id") or "",
            "offer_name":     details.get("offer_name") or "",
            "spending_limit": details.get("spending_limit") or "",
            "tenant_id":      details.get("tenant_id") or "",
            "currency":       sub_currency or details.get("currency") or os.environ.get("AZURE_CURRENCY", "USD"),
            "billing_period": details.get("billing_period") or "",
            "purchase_date":  details.get("purchase_date") or "",
            "account_admin":  details.get("account_admin") or "",
            "billing_account_id": details.get("billing_account_id") or "",
            "azure_tags":     details.get("azure_tags") or {},
        }

    except KeyError:
        return {"id": sub_id, "name": sub_name, "tags": tags,
                "error": "Credenciales no configuradas"}
    except Exception as exc:
        log.warning("Error fetching %s: %s", sub_id, exc)
        return {"id": sub_id, "name": sub_name, "tags": tags,
                "error": str(exc)}


# ---------------------------------------------------------------------------
# Rutas
# ---------------------------------------------------------------------------

def _run_scheduled_overview():
    """
    Ejecutado por APScheduler cada 3h (y al arrancar la app).
    Re-descubre suscripciones y hace fetch de todas en paralelo.
    """
    global _OVERVIEW_CACHE, SUBSCRIPTIONS, SUBSCRIPTION_MAP

    if _OVERVIEW_CACHE["loading"]:
        log.info("Scheduled fetch ya en curso, omitiendo.")
        return

    _OVERVIEW_CACHE["loading"] = True
    log.info("Scheduled fetch iniciado.")

    # Re-descubrir suscripciones por si han cambiado
    fresh_subs = discover_subscriptions()
    if fresh_subs:
        SUBSCRIPTIONS      = fresh_subs
        SUBSCRIPTION_MAP   = {s["id"]: s for s in fresh_subs}
        log.info("Suscripciones actualizadas: %d", len(fresh_subs))

    start_dt = FIXED_START
    end_dt   = _yesterday()
    period_days = (end_dt - start_dt).days

    try:
        if not SUBSCRIPTIONS:
            log.warning("Sin suscripciones disponibles — fetch cancelado.")
            _OVERVIEW_CACHE["loading"] = False
            _OVERVIEW_CACHE["error"]   = "No se encontraron suscripciones con el offer ID configurado. Comprueba que el SP tiene rol Reader."
            return

        # Obtener el RateCard UNA SOLA VEZ antes del loop paralelo
        # Todas las subs comparten el mismo offer/currency/region
        log.info("Cargando RateCard (una vez para todas las suscripciones)...")
        shared_rate_map = fetch_rate_card(SUBSCRIPTIONS[0]["id"])
        if not shared_rate_map:
            log.warning("RateCard no disponible — los costes se mostrarán como 0.")

        max_workers = max(1, int(os.environ.get("MAX_WORKERS", "8") or "8"))
        with ThreadPoolExecutor(max_workers=min(max_workers, len(SUBSCRIPTIONS))) as executor:
            futures = {
                executor.submit(_fetch_subscription_summary, sub, start_dt, end_dt, period_days, shared_rate_map): sub
                for sub in SUBSCRIPTIONS
            }
            results_map = {}
            for future in as_completed(futures):
                sub = futures[future]
                results_map[sub["id"]] = future.result()

        sub_summaries = [results_map[s["id"]] for s in SUBSCRIPTIONS if s["id"] in results_map]
        grand_total   = sum(s.get("total", 0.0) for s in sub_summaries if not s.get("error"))
        avg_daily     = grand_total / period_days if period_days > 0 else 0.0

        _OVERVIEW_CACHE.update({
            "sub_summaries":   sub_summaries,
            "grand_total_all": grand_total,
            "avg_daily_all":   avg_daily,
            "period_days":     period_days,
            "start":           start_dt.strftime("%Y-%m-%d"),
            "end":             end_dt.strftime("%Y-%m-%d"),
            "last_updated":    datetime.now(timezone.utc),
            "loading":         False,
            "error":           None,
        })
        log.info("Scheduled fetch completado. Total: %.2f EUR", grand_total)

    except Exception as exc:
        log.error("Scheduled fetch falló: %s", exc)
        _OVERVIEW_CACHE["loading"] = False
        _OVERVIEW_CACHE["error"]   = str(exc)


# Arranca el scheduler en background (cada 3h + ejecución inmediata al inicio)
_scheduler = BackgroundScheduler(daemon=True)
_scheduler.add_job(
    _run_scheduled_overview,
    trigger="interval",
    hours=int(os.environ.get("REFRESH_HOURS", 3)),
    id="overview_refresh",
    next_run_time=datetime.now(),   # ejecutar inmediatamente al arrancar
    misfire_grace_time=300,
)
_scheduler.start()
log.info("Scheduler arrancado — refresh cada %sh.", os.environ.get("REFRESH_HOURS", 3))


def _date_context() -> dict:
    """Valores por defecto de fecha compartidos entre rutas."""
    today = _today()
    return {
        "today":         today,
        "default_start": FIXED_START,
        "default_end":   _yesterday(),
    }


@app.route("/overview")
@login_required
def overview():
    """
    Panel consolidado. Sirve datos de _OVERVIEW_CACHE (actualizado cada 3h).
    Si el usuario pulsa 'Actualizar ahora' lanza un fetch manual inmediato.
    """
    tag_filter  = request.args.get("tag", "")
    force_refresh = request.args.get("refresh") == "true"

    if force_refresh and not _OVERVIEW_CACHE["loading"]:
        import threading
        threading.Thread(target=_run_scheduled_overview, daemon=True).start()

    cache = _OVERVIEW_CACHE
    sub_summaries = cache["sub_summaries"]

    # Filtro por tag (se aplica sobre la caché en memoria)
    if tag_filter:
        sub_summaries = [s for s in sub_summaries if tag_filter in s.get("tags", [])]

    # Ordenar por consumo descendente
    sub_summaries = sorted(sub_summaries, key=lambda s: s.get("total", 0.0), reverse=True)

    grand_total_all = sum(s.get("total", 0.0) for s in sub_summaries if not s.get("error"))
    avg_daily_all   = grand_total_all / cache["period_days"] if cache["period_days"] > 0 else 0.0

    all_tags = sorted({tag for s in SUBSCRIPTIONS for tag in s.get("tags", [])})
    currency = os.environ.get("AZURE_CURRENCY", "EUR")

    return render_template(
        "overview.html",
        sub_summaries=sub_summaries,
        grand_total_all=grand_total_all,
        avg_daily_all=avg_daily_all,
        currency=currency,
        start=cache["start"],
        end=cache["end"] or _yesterday().strftime("%Y-%m-%d"),
        fetched=bool(cache["last_updated"]),
        loading=cache["loading"],
        last_updated=cache["last_updated"],
        error=cache["error"],
        all_tags=all_tags,
        tag_filter=tag_filter,
        subscriptions=SUBSCRIPTIONS,
        period_days=cache["period_days"],
    )


@app.route("/")
def index():
    """Redirige al overview consolidado (página de inicio)."""
    return redirect(url_for("overview"))


@app.route("/detail")
@login_required
def detail():
    """
    Dashboard individual: igual que el original pero con selector de suscripción.
    Si no se pasa ?sub=, usa la primera suscripción de la lista.
    """
    dc = _date_context()
    start_str   = request.args.get("start", FIXED_START.strftime("%Y-%m-%d"))
    end_str     = request.args.get("end",   _yesterday().strftime("%Y-%m-%d"))
    granularity = request.args.get("granularity", "Daily")
    show_details = request.args.get("show_details", "false").lower() == "true"
    fetched     = request.args.get("refresh") == "true"

    # Suscripción activa
    sub_id = request.args.get("sub")
    if not sub_id and SUBSCRIPTIONS:
        sub_id = SUBSCRIPTIONS[0]["id"]
    active_sub = SUBSCRIPTION_MAP.get(sub_id, {"id": sub_id, "name": sub_id or "Desconocida"})

    error            = None
    rate_card_warning = None
    currency         = os.environ.get("AZURE_CURRENCY", "USD")

    # Valores por defecto para el template
    template_ctx = dict(
        records=[], total_quantity_by_meter={}, total_cost_by_meter={},
        unit_by_meter={}, grand_total_cost=0.0, average_daily_cost=0.0,
        chart_labels=[], chart_series_data={}, chart_meter_order=[],
        cache_efficiency={}, has_brsdt=False,
        svc_quantity={}, svc_cost={}, svc_unit={},
        svc_chart_series={}, svc_meter_order=[],
        ai_quantity={}, ai_cost={}, ai_unit={},
        ai_chart_series={}, ai_meter_order=[], ai_total_cost=0.0,
        brsdt_rate_labels=_BRSDT_RATE_LABELS,
        brsdt_other_rates=_BRSDT_OTHER_RATES,
        brsdt_unmatched_rates=[],
        projected_days=[], projected_cost_per_day=0.0,
        projected_total=0.0, projected_grand_total=0.0,
    )

    try:
        start_dt = datetime.strptime(start_str, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        end_dt   = datetime.strptime(end_str,   "%Y-%m-%d").replace(tzinfo=timezone.utc)

        if start_dt >= end_dt:
            raise ValueError("La fecha de inicio debe ser anterior a la de fin.")
        if (end_dt - start_dt).days > 365:
            raise ValueError("El rango no puede superar 365 días.")
        if end_dt >= _today():
            raise ValueError("La fecha de fin debe ser anterior a hoy.")

        period_days = (end_dt - start_dt).days

        if fetched:
            if not sub_id:
                raise KeyError("sub_id")
            records  = fetch_usage(sub_id, start_dt, end_dt, granularity, show_details)
            rate_map = fetch_rate_card(sub_id)
            if not rate_map and records:
                rate_card_warning = (
                    "RateCard no disponible — los costes estimados no se muestran. "
                    "Verifica AZURE_OFFER_ID y credenciales."
                )
            template_ctx = process_records(records, rate_map, period_days)

        # Siempre cargar metadatos de la sub activa (con caché)
        sub_details = fetch_subscription_details(sub_id) if sub_id else {}

    except ValueError as exc:
        error = str(exc)
        sub_details = {}
    except KeyError:
        error = (
            "No hay suscripciones configuradas. "
            "Crea subscriptions.json a partir de subscriptions.json.example."
        )
        sub_details = {}
    except Exception as exc:
        error = f"Error Azure API: {exc}"
        sub_details = {}

    return render_template(
        "index.html",
        **template_ctx,
        currency=currency,
        rate_card_warning=rate_card_warning,
        fetched=fetched,
        start=start_str,
        end=end_str,
        granularity=granularity,
        show_details=show_details,
        error=error,
        # Multi-sub additions
        subscriptions=SUBSCRIPTIONS,
        active_sub=active_sub,
        sub_details=sub_details,
    )


@app.route("/api/cache-status")
def cache_status():
    """
    Endpoint de polling para que el frontend detecte cuándo termina el fetch.
    Devuelve el estado actual de la caché sin datos pesados.
    """
    last = _OVERVIEW_CACHE["last_updated"]
    return {
        "loading":      _OVERVIEW_CACHE["loading"],
        "has_data":     bool(last),
        "last_updated": last.isoformat() if last else None,
        "sub_count":    len(_OVERVIEW_CACHE.get("sub_summaries", [])),
        "error":        _OVERVIEW_CACHE.get("error"),
    }, 200


@app.route("/health")
def health():
    """Liveness probe con estado de la caché."""
    last = _OVERVIEW_CACHE["last_updated"]
    return {
        "status": "ok",
        "subscriptions_loaded": len(SUBSCRIPTIONS),
        "cache_last_updated": last.isoformat() if last else None,
        "cache_loading": _OVERVIEW_CACHE["loading"],
    }, 200


@app.route("/debug/subscriptions")
@login_required
def debug_subscriptions():
    """Lista suscripciones visibles al SP — solo para usuarios autenticados."""
    try:
        credential = DefaultAzureCredential()
        sub_client = SubscriptionClient(credential)

        all_subs = []
        for sub in sub_client.subscriptions.list():
            policies  = sub.subscription_policies or {}
            quota_raw = getattr(policies, "quota_id", "") or ""
            quota_id  = quota_raw.split("_")[0] if "_" in quota_raw else quota_raw
            all_subs.append({
                "id":           sub.subscription_id,
                "name":         sub.display_name,
                "state":        str(sub.state),
                "quota_id_raw": quota_raw,
                "quota_id":     quota_id,
                "match":        quota_id in MONITORED_OFFER_IDS,
            })

        return {
            "monitored_offer_ids": MONITORED_OFFER_IDS,
            "total_visible":       len(all_subs),
            "total_matched":       sum(1 for s in all_subs if s["match"]),
            "subscriptions":       all_subs,
        }, 200

    except Exception as exc:
        return {"error": str(exc), "type": type(exc).__name__}, 500


if __name__ == "__main__":
    app.run(debug=False)