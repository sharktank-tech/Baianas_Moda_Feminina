import logging
import re
import uuid
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.exc import IntegrityError

from loja import db
from loja.modules.enviar_email import enviar_email
from loja.modules.models import NewsletterSubscriber


logger = logging.getLogger(__name__)

_EMAIL_PATTERN = re.compile(
    r"^[A-Z0-9.!#$%&'*+/=?^_`{|}~-]+@"
    r"[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?"
    r"(?:\.[A-Z0-9](?:[A-Z0-9-]{0,61}[A-Z0-9])?)+$",
    re.IGNORECASE,
)


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


def _send_welcome_email(email: str) -> bool:
    """
    Envia a mensagem de boas-vindas.

    A falha no envio não desfaz o cadastro, pois o assinante já foi salvo
    com sucesso no banco de dados.
    """
    subject = "Seu cupom de boas-vindas — Pollo Modas"
    message = """
Olá!

Seu cadastro na newsletter da Pollo Modas foi realizado com sucesso.

Use o cupom PRIMEIRACOMPRA para ganhar 10% de desconto na sua primeira compra.

Você receberá novidades, lançamentos e promoções em primeira mão.

Pollo Modas
    """.strip()

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

    if send_welcome_email:
        email_sent = _send_welcome_email(normalized_email)

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
