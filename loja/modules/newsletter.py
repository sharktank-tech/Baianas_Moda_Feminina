import hashlib
import hmac
import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from flask import current_app, url_for
from sqlalchemy.exc import IntegrityError

from loja import db
from loja.modules.enviar_email import enviar_email
from loja.modules.models import NewsletterSubscriber
from loja.utils import utcnow


logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)

# Prefixo do MAC do token de cancelamento (evita reuso do mesmo segredo em
# outro contexto).
_TOKEN_CONTEXT = "newsletter-unsubscribe"
_MAC_HEX_LEN = 32


@dataclass(frozen=True)
class NewsletterResult:
    success: bool
    message: str
    subscriber: Optional[NewsletterSubscriber] = None
    created: bool = False
    email_sent: bool = False


def normalize_email(email: Optional[str]) -> str:
    """Remove espaços e padroniza o e-mail em letras minúsculas."""
    return (email or "").strip().lower()


def is_valid_email(email: str) -> bool:
    """Faz uma validação básica sem adicionar dependências externas."""
    if not email or len(email) > 150:
        return False

    return bool(_EMAIL_PATTERN.fullmatch(email))


def is_subscription_suppressed(email: Optional[str]) -> bool:
    """True se o e-mail tem cancelamento ativo (não enviar promoção)."""
    normalized_email = normalize_email(email)
    if not normalized_email:
        return False
    subscriber = NewsletterSubscriber.query.filter_by(
        email=normalized_email
    ).first()
    return bool(subscriber and subscriber.unsubscribed_at is not None)


def is_marketable(email: Optional[str]) -> bool:
    """Liberação para envios promocionais (supressão de cancelados)."""
    return not is_subscription_suppressed(email)


def _unsubscribe_mac(subscriber_id_hex: str) -> str:
    secret = current_app.config.get("SECRET_KEY") or ""
    return hmac.new(
        secret.encode("utf-8"),
        f"{_TOKEN_CONTEXT}:{subscriber_id_hex}".encode("utf-8"),
        hashlib.sha256,
    ).hexdigest()[:_MAC_HEX_LEN]


def generate_unsubscribe_token(subscriber: NewsletterSubscriber) -> str:
    """Token opaco (id + MAC): não expõe e-mail nem depende de login."""
    return f"{subscriber.id.hex}.{_unsubscribe_mac(subscriber.id.hex)}"


def verify_unsubscribe_token(
    token: Optional[str],
) -> Optional[NewsletterSubscriber]:
    """Valida token e devolve o assinante; inválido/falho => None."""
    if not token or "." not in token:
        return None
    id_hex, _, mac = token.partition(".")
    if len(id_hex) != 32 or len(mac) != _MAC_HEX_LEN:
        return None
    try:
        subscriber_id = uuid.UUID(hex=id_hex)
    except ValueError:
        return None
    subscriber = db.session.get(NewsletterSubscriber, subscriber_id)
    if subscriber is None:
        return None
    expected = _unsubscribe_mac(id_hex)
    if not hmac.compare_digest(mac, expected):
        return None
    return subscriber


def unsubscribe_by_token(token: Optional[str]) -> NewsletterResult:
    """Cancela a inscrição validada pelo token (idempotente)."""
    subscriber = verify_unsubscribe_token(token)
    if subscriber is None:
        return NewsletterResult(
            success=False,
            message="Link de cancelamento inválido ou expirado.",
        )
    if subscriber.unsubscribed_at is None:
        subscriber.unsubscribed_at = utcnow()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Erro ao cancelar newsletter para %s.", subscriber.email,
            )
            return NewsletterResult(
                success=False,
                message="Não foi possível concluir o cancelamento. Tente novamente.",
            )
    return NewsletterResult(
        success=True,
        message="Sua inscrição foi cancelada. Você não receberá mais novidades e promoções.",
        subscriber=subscriber,
    )


def reactivate_by_token(token: Optional[str]) -> NewsletterResult:
    """Reativa inscrição cancelada (manifestação explícita via token)."""
    subscriber = verify_unsubscribe_token(token)
    if subscriber is None:
        return NewsletterResult(
            success=False,
            message="Link inválido ou expirado.",
        )
    if subscriber.unsubscribed_at is not None:
        subscriber.unsubscribed_at = None
        subscriber.resubscribed_at = utcnow()
        try:
            db.session.commit()
        except Exception:
            db.session.rollback()
            logger.exception(
                "Erro ao reativar newsletter para %s.", subscriber.email,
            )
            return NewsletterResult(
                success=False,
                message="Não foi possível reativar. Tente novamente.",
            )
    return NewsletterResult(
        success=True,
        message="Pronto! Sua inscrição está ativa novamente.",
        subscriber=subscriber,
    )


def _welcome_cancel_link(subscriber: NewsletterSubscriber) -> Optional[str]:
    try:
        return url_for(
            "main.newsletter_cancelar",
            token=generate_unsubscribe_token(subscriber),
            _external=True,
        )
    except Exception:
        logger.exception(
            "Não foi possível gerar o link de cancelamento para %s.",
            subscriber.email,
        )
        return None


def _persisted_subscription_state(
    subscriber_id: uuid.UUID,
) -> Optional[NewsletterSubscriber]:
    """Recarrega o estado persistido, imediatamente antes do envio.

    Não confia no objeto recebido (pode estar stale — cancelamento
    confirmado em outra sessão após a leitura). `populate_existing()`
    força o refresh do identity map da sessão, evitando que `.first()`
    reutilize o estado em memória. Objeto detached também é tratado:
    a consulta roda na sessão ativa atrelada ao contexto. Qualquer falha
    na verificação deve ser tratada como supressão (fail-closed).
    """
    try:
        return (
            NewsletterSubscriber.query
            .filter_by(id=subscriber_id)
            .populate_existing()
            .first()
        )
    except Exception:
        logger.exception(
            "Não foi possível recarregar o estado da newsletter (id=%s).",
            subscriber_id,
        )
        return None


def _send_welcome_email(subscriber: NewsletterSubscriber) -> bool:
    """
    Envia a mensagem de boas-vindas com o link de cancelamento.

    Consulta o estado persistido imediatamente antes da chamada ao
    provedor: o objeto recebido pode estar stale (cancelamento confirmado
    em outra sessão após a leitura). Falha na verificação impede o envio
    (fail-closed). A falha no envio não desfaz o cadastro.
    """
    email = subscriber.email
    persisted = _persisted_subscription_state(subscriber.id)
    if persisted is None or persisted.unsubscribed_at is not None:
        return False
    subject = "Seu cupom de boas-vindas — Pollo Modas"
    message = """
Olá!

Seu cadastro na newsletter da Pollo Modas foi realizado com sucesso.

Use o cupom PRIMEIRACOMPRA para ganhar 10% de desconto na sua primeira compra.

Você receberá novidades, lançamentos e promoções em primeira mão.

Pollo Modas
    """.strip()
    cancel_link = _welcome_cancel_link(subscriber)
    if cancel_link:
        message += (
            "\n\nPara cancelar sua inscrição a qualquer momento:\n"
            f"{cancel_link}"
        )

    try:
        enviar_email(email, subject, message)
        return True
    except Exception:
        logger.exception(
            "Não foi possível enviar o e-mail de boas-vindas para %s.",
            email,
        )
        return False


def subscribe_newsletter(
    email: Optional[str],
    *,
    send_welcome_email: bool = True,
) -> NewsletterResult:
    """
    Cadastra um e-mail na newsletter.

    Retorna um NewsletterResult para a rota decidir qual mensagem exibir.
    """
    normalized_email = normalize_email(email)

    if not is_valid_email(normalized_email):
        return NewsletterResult(
            success=False,
            message="Informe um e-mail válido.",
        )

    existing_subscriber = NewsletterSubscriber.query.filter_by(
        email=normalized_email
    ).first()

    if existing_subscriber:
        if existing_subscriber.unsubscribed_at is not None:
            # Cancelado: NÃO reativar por repetição de cadastro. Reinscrição
            # exige manifestação explícita via token (reactivate_by_token).
            return NewsletterResult(
                success=False,
                message=(
                    "Este e-mail está cancelado na nossa newsletter. "
                    "Use o link de reativação para voltar a receber novidades."
                ),
                subscriber=existing_subscriber,
                created=False,
                email_sent=False,
            )
        return NewsletterResult(
            success=True,
            message="Este e-mail já está cadastrado na nossa newsletter.",
            subscriber=existing_subscriber,
            created=False,
            email_sent=False,
        )

    subscriber = NewsletterSubscriber(
        id=uuid.uuid4(),
        email=normalized_email,
    )

    try:
        db.session.add(subscriber)
        db.session.commit()

    except IntegrityError:
        # Protege contra dois cadastros simultâneos do mesmo e-mail.
        db.session.rollback()

        existing_subscriber = NewsletterSubscriber.query.filter_by(
            email=normalized_email
        ).first()

        if existing_subscriber:
            if existing_subscriber.unsubscribed_at is not None:
                return NewsletterResult(
                    success=False,
                    message=(
                        "Este e-mail está cancelado na nossa newsletter. "
                        "Use o link de reativação para voltar a receber novidades."
                    ),
                    subscriber=existing_subscriber,
                    created=False,
                    email_sent=False,
                )
            return NewsletterResult(
                success=True,
                message="Este e-mail já está cadastrado na nossa newsletter.",
                subscriber=existing_subscriber,
                created=False,
                email_sent=False,
            )

        logger.exception(
            "Erro de integridade ao cadastrar %s na newsletter.",
            normalized_email,
        )

        return NewsletterResult(
            success=False,
            message="Não foi possível concluir o cadastro. Tente novamente.",
        )

    except Exception:
        db.session.rollback()

        logger.exception(
            "Erro inesperado ao cadastrar %s na newsletter.",
            normalized_email,
        )

        return NewsletterResult(
            success=False,
            message="Não foi possível concluir o cadastro. Tente novamente.",
        )

    email_sent = False

    if send_welcome_email and subscriber.unsubscribed_at is None:
        email_sent = _send_welcome_email(subscriber)

    if email_sent:
        message = (
            "Cadastro feito! Confira seu e-mail para receber o cupom de 10%."
        )
    else:
        message = (
            "Cadastro feito com sucesso! Seu cupom é PRIMEIRACOMPRA."
        )

    return NewsletterResult(
        success=True,
        message=message,
        subscriber=subscriber,
        created=True,
        email_sent=email_sent,
    )
