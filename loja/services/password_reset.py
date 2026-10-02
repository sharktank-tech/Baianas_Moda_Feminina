"""Recuperação de senha ("esqueci minha senha").

Garantias:
- Resposta pública sempre idêntica (PUBLIC_MESSAGE) exista ou não a conta,
  seja OAuth-only (sem senha local), esteja no limite de tentativas ou o
  envio de e-mail falhe — nenhuma dessas variações muda texto/código.
- Token: secrets.token_urlsafe(32) (>=32 bytes de entropia); no banco fica
  apenas sha256(token); comparação com hmac.compare_digest.
- 1 token ativo por usuário (nova solicitação revoga os anteriores); troca
  bem-sucedida consome o token, incrementa User.auth_version (invalida
  sessões e "manter conectado" antigos) e revoga demais tokens — tudo em uma
  única transação com rollback em falha parcial.
- Link montado SOMENTE de APP_PUBLIC_URL (origem configurada), nunca do
  header Host. Nenhum token, link ou senha é logado.
"""

import hashlib
import hmac
import logging
import secrets
import threading
from datetime import timedelta
from urllib.parse import quote, urlsplit

from loja.modules.enviar_email import enviar_email
from loja.services.rate_limits import rate_allow

logger = logging.getLogger(__name__)

PUBLIC_MESSAGE = (
    "Se houver uma conta elegível com esse e-mail, você receberá as "
    "instruções para redefinir sua senha."
)

TOKEN_TTL_MINUTES = 30

# Limites por janela fixa (segundos). O app pode sobrescrever via config:
# PASSWORD_RESET_IP_LIMIT / PASSWORD_RESET_EMAIL_LIMIT / *_WINDOW_SECONDS.
IP_LIMIT_DEFAULT = 5
EMAIL_LIMIT_DEFAULT = 3
WINDOW_SECONDS_DEFAULT = 900

_SUBJECT = "Redefina sua senha — Pollo Modas"


def _hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def _config_int(name: str, default: int) -> int:
    from flask import current_app

    try:
        return int(current_app.config.get(name, default))
    except (TypeError, ValueError):
        return default


def _build_reset_link(token: str):
    """Link absoluto a partir de APP_PUBLIC_URL — nunca do header Host."""
    from flask import current_app

    raw = (current_app.config.get("APP_PUBLIC_URL") or "").strip()
    if not raw:
        return None
    raw = raw.rstrip("/")
    if not raw.startswith(("http://", "https://")):
        return None
    parts = urlsplit(raw)
    if not parts.netloc or parts.query or parts.fragment:
        # Somente a origem pública (mesma regra de APP_PUBLIC_URL usada
        # pelo serviço de pagamentos) — caminho/query/fragmento não são
        # aceitos para não forjar o link.
        return None
    return f"{raw}/redefinir-senha?token={quote(token, safe='')}"


def _text_body(link: str) -> str:
    return (
        "Olá,\n\n"
        "Recebemos uma solicitação para redefinir a senha da sua conta "
        "Pollo Modas.\n\n"
        f"Para escolher uma nova senha, acesse o link abaixo "
        f"(válido por {TOKEN_TTL_MINUTES} minutos):\n"
        f"{link}\n\n"
        "Se você não solicitou esta alteração, ignore este e-mail — sua "
        "senha continuará a mesma.\n\n"
        "Equipe Pollo Modas\n"
    )


def _html_body(link: str) -> str:
    escaped_link = (
        link.replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    )
    return (
        "<!DOCTYPE html><html><body style=\"margin:0;padding:24px;"
        "background:#f6f6f7;font-family:Arial,Helvetica,sans-serif;\">"
        "<div style=\"max-width:520px;margin:0 auto;background:#ffffff;"
        "border-radius:12px;padding:32px;\">"
        "<h1 style=\"margin:0 0 8px;font-size:20px;color:#111111;\">"
        "Pollo<span style=\"color:#c81e4a;\">.</span>Modas</h1>"
        "<p style=\"margin:0 0 16px;font-size:15px;color:#333333;\">"
        "Recebemos uma solicitação para redefinir a senha da sua conta.</p>"
        "<p style=\"margin:0 0 20px;font-size:15px;color:#333333;\">"
        f"O link abaixo é válido por <strong>{TOKEN_TTL_MINUTES} minutos"
        "</strong>:</p>"
        "<p style=\"margin:0 0 20px;\"><a href=\""
        f"{escaped_link}\" style=\"display:inline-block;background:#111111;"
        "color:#ffffff;text-decoration:none;padding:12px 24px;"
        "border-radius:8px;font-size:15px;\">Redefinir minha senha</a></p>"
        "<p style=\"margin:0 0 8px;font-size:13px;color:#777777;\">"
        "Se você não solicitou esta alteração, ignore este e-mail — sua "
        "senha continuará a mesma.</p>"
        "<p style=\"margin:0;font-size:13px;color:#777777;\">"
        "Equipe Pollo Modas</p>"
        "</div></body></html>"
    )


def _dispatch_email(destinatario: str, link: str, *, sender=None) -> None:
    """Envio síncrono ou em thread; falha NUNCA muda a resposta pública."""
    from flask import current_app

    send = sender if sender is not None else enviar_email
    subject = _SUBJECT
    text_body = _text_body(link)
    html_body = _html_body(link)

    def _run():
        try:
            send(destinatario, subject, text_body, corpo_html=html_body)
        except Exception as exc:  # log sem token/link/senha
            logger.warning(
                "password_reset email_failed=1 err=%s", type(exc).__name__
            )

    # Serverless runtimes can freeze daemon threads after the response.
    # Wait for SMTP by default; background dispatch needs a durable worker.
    if current_app.config.get("PASSWORD_RESET_SEND_ASYNC", False):
        threading.Thread(target=_run, daemon=True).start()
    else:
        _run()


def request_password_reset(email, *, ip=None, email_sender=None) -> str:
    """Processa a solicitação e SEMPRE retorna PUBLIC_MESSAGE.

    Elegível apenas conta com senha local (coluna password não vazia):
    contas sem senha local (cenário futuro de OAuth-only) caem na mesma
    resposta genérica e NUNCA ganham senha criada por recuperação.
    """
    from loja import db
    from loja.modules.models import PasswordResetToken, User, utcnow

    normalized = (email or "").strip().lower()

    ip_limit = _config_int("PASSWORD_RESET_IP_LIMIT", IP_LIMIT_DEFAULT)
    email_limit = _config_int(
        "PASSWORD_RESET_EMAIL_LIMIT", EMAIL_LIMIT_DEFAULT
    )
    window = _config_int(
        "PASSWORD_RESET_WINDOW_SECONDS", WINDOW_SECONDS_DEFAULT
    )

    ip_blocked = bool(ip) and not rate_allow("pwreset_ip", ip, ip_limit, window)
    email_blocked = bool(normalized) and not rate_allow(
        "pwreset_email", normalized, email_limit, window
    )

    user = (
        User.query.filter_by(email=normalized).first()
        if normalized
        else None
    )
    eligible = bool(user is not None and (user.password or "").strip())

    if ip_blocked or email_blocked or not eligible:
        return PUBLIC_MESSAGE

    token = secrets.token_urlsafe(32)
    now = utcnow()
    try:
        # Nova solicitação invalida os tokens anteriores (1 ativo por vez).
        PasswordResetToken.query.filter(
            PasswordResetToken.user_id == user.id,
            PasswordResetToken.used_at.is_(None),
        ).delete(synchronize_session=False)
        db.session.add(
            PasswordResetToken(
                user_id=user.id,
                token_hash=_hash_token(token),
                created_at=now,
                expires_at=now + timedelta(minutes=TOKEN_TTL_MINUTES),
            )
        )
        db.session.commit()
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "password_reset store_failed=1 err=%s", type(exc).__name__
        )
        return PUBLIC_MESSAGE

    link = _build_reset_link(token)
    if link is None:
        logger.error("password_reset public_url_invalid=1")
        return PUBLIC_MESSAGE

    _dispatch_email(normalized, link, sender=email_sender)
    return PUBLIC_MESSAGE


def validate_reset_token(token) -> str:
    """Checagem SOMENTE leitura (GET não consome o token).

    Retorna "ok" | "invalid" | "expired" | "used".
    """
    from loja.modules.models import PasswordResetToken, utcnow

    if not token:
        return "invalid"
    token_hash = _hash_token(token)
    row = PasswordResetToken.query.filter_by(token_hash=token_hash).first()
    if row is None:
        return "invalid"
    if not hmac.compare_digest(str(row.token_hash), token_hash):
        return "invalid"
    if row.used_at is not None:
        return "used"
    if row.expires_at <= utcnow():
        return "expired"
    return "ok"


def reset_password_with_token(token, new_password) -> tuple:
    """Consome o token e troca a senha em UMA transação.

    Retorna (ok, code): code em {"ok", "invalid", "expired", "used",
    "empty", "same_password"}. Sob corrida (dois POSTs com o mesmo token)
    apenas um recebe "ok"; o outro recebe "used"/"invalid" — nunca erro 500.
    """
    from sqlalchemy.exc import OperationalError

    from loja import db
    from loja.modules.models import PasswordResetToken, User, utcnow

    if not token:
        return False, "invalid"
    token_hash = _hash_token(token)
    now = utcnow()

    try:
        row = (
            PasswordResetToken.query.filter(
                PasswordResetToken.token_hash == token_hash
            )
            .with_for_update()
            .first()
        )
        if row is None:
            return False, "invalid"
        if not hmac.compare_digest(str(row.token_hash), token_hash):
            return False, "invalid"
        if row.used_at is not None:
            db.session.rollback()
            return False, "used"
        if row.expires_at <= now:
            db.session.rollback()
            return False, "expired"

        if not (new_password or "").strip():
            db.session.rollback()
            return False, "empty"

        user = db.session.get(User, row.user_id)
        if user is None or not (user.password or "").strip():
            db.session.rollback()
            return False, "invalid"
        if user.check_password(new_password):
            db.session.rollback()
            return False, "same_password"

        # Consumo atômico: só a primeira transação afeta a linha.
        consumed = (
            PasswordResetToken.query.filter(
                PasswordResetToken.id == row.id,
                PasswordResetToken.used_at.is_(None),
            )
            .update(
                {PasswordResetToken.used_at: now},
                synchronize_session=False,
            )
        )
        if consumed != 1:
            db.session.rollback()
            return False, "used"

        user.set_password(new_password)
        user.auth_version += 1
        PasswordResetToken.query.filter(
            PasswordResetToken.user_id == row.user_id,
            PasswordResetToken.used_at.is_(None),
        ).delete(synchronize_session=False)
        db.session.commit()
        return True, "ok"
    except OperationalError:
        # Corrida por lock (ex.: SQLite) — resposta clara, sem 500.
        db.session.rollback()
        return False, "used"
    except Exception as exc:
        db.session.rollback()
        logger.warning(
            "password_reset consume_failed=1 err=%s", type(exc).__name__
        )
        return False, "invalid"


__all__ = [
    "PUBLIC_MESSAGE",
    "TOKEN_TTL_MINUTES",
    "request_password_reset",
    "reset_password_with_token",
    "validate_reset_token",
]
