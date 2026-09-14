import hashlib
import json
import os
import re
import uuid
from datetime import datetime, timezone

from flask import current_app, has_request_context, request


_SENSITIVE_KEYS = {
    "access_token",
    "authorization",
    "card_number",
    "cvv",
    "email",
    "first_name",
    "last_name",
    "name",
    "password",
    "phone",
    "surname",
    "token",
}
_CORRELATION_RE = re.compile(r"^[A-Za-z0-9._:-]{1,64}$")

# Eventos de webhook (issue #50): nomes fixos e sanitizados para poder
# distinguir moderno válido, moderno rejeitado, IPN legado ignorado e payload
# malformado/desconhecido ignorado — sem vazar identificadores em texto puro.
WH_MODERN_VALIDATED = "modern_webhook_validated"
WH_MODERN_REJECTED = "modern_webhook_rejected"
WH_LEGACY_IPN_IGNORED = "legacy_ipn_ignored"
WH_MALFORMED_IGNORED = "webhook_malformed_ignored"


def stable_hash(value):
    """Return a short deterministic hash suitable for cross-log correlation."""
    if value is None:
        return None
    normalized = str(value).strip()
    if not normalized:
        return None
    return hashlib.sha256(normalized.encode("utf-8")).hexdigest()[:16]


def new_correlation_id():
    return uuid.uuid4().hex


def request_correlation_id():
    if not has_request_context():
        return new_correlation_id()
    existing = getattr(request, "_pollomodas_correlation_id", None)
    if existing:
        return existing
    incoming = (request.headers.get("x-correlation-id") or "").strip()
    correlation_id = incoming if _CORRELATION_RE.fullmatch(incoming) else new_correlation_id()
    request._pollomodas_correlation_id = correlation_id
    return correlation_id


def runtime_context():
    return {
        "deployment_id": os.environ.get("VERCEL_DEPLOYMENT_ID"),
        "deployment_url": os.environ.get("VERCEL_URL"),
        "environment": os.environ.get("VERCEL_ENV"),
        "git_branch": os.environ.get("VERCEL_GIT_COMMIT_REF"),
        "git_sha": os.environ.get("VERCEL_GIT_COMMIT_SHA"),
        "region": os.environ.get("VERCEL_REGION"),
    }


def _safe_text(value, max_length=160):
    if value is None:
        return None
    normalized = " ".join(str(value).split())
    return normalized[:max_length]


def _sanitize_value(key, value):
    lower = str(key or "").lower()
    if lower in _SENSITIVE_KEYS or any(token in lower for token in ("secret", "credential")):
        return "[REDACTED]"
    if lower.endswith("_id") or lower in {
        "external_reference",
        "merchant_order",
        "merchant_order_id",
        "order_id",
        "payment_id",
        "preference_id",
        "provider_payment_id",
        "user_id",
        "x_request_id",
    }:
        return stable_hash(value)
    if lower == "provider_message":
        return _safe_text(value)
    if isinstance(value, dict):
        return sanitize_fields(value)
    if isinstance(value, (list, tuple)):
        return [
            sanitize_fields(item) if isinstance(item, dict) else item
            for item in value
        ]
    return value


def sanitize_fields(fields):
    return {key: _sanitize_value(key, value) for key, value in (fields or {}).items()}


def log_event(event, *, correlation_id=None, level="info", **fields):
    supplied_correlation = str(correlation_id or "").strip()
    if supplied_correlation and _CORRELATION_RE.fullmatch(supplied_correlation):
        safe_correlation = supplied_correlation
    elif supplied_correlation:
        safe_correlation = stable_hash(supplied_correlation)
    else:
        safe_correlation = request_correlation_id()

    payload = {
        "ts": datetime.now(timezone.utc).isoformat(),
        "event": str(event),
        "correlation_id": safe_correlation,
        **runtime_context(),
        **sanitize_fields(fields),
    }
    message = "mp_observability " + json.dumps(
        payload,
        ensure_ascii=False,
        separators=(",", ":"),
        sort_keys=True,
        default=str,
    )
    logger = current_app.logger
    log_method = getattr(logger, level, logger.info)
    log_method(message)
    return payload
