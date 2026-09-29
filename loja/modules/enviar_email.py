import html
import logging
import ssl

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
from email.utils import formataddr
import smtplib
from config import Config


logger = logging.getLogger(__name__)


def _smtp_username():
    configured = getattr(Config, "EMAIL_USERNAME", None)
    if configured is not None:
        return str(configured).strip()
    if str(Config.EMAIL_SERVER or "").strip().lower() == "smtp.resend.com":
        return "resend"
    return str(Config.EMAIL_SENDER or "").strip()


def email_delivery_ready():
    """Confirma apenas presença/forma da configuração, sem expor valores."""
    try:
        port = int(Config.EMAIL_PORT)
    except (TypeError, ValueError):
        return False
    return bool(
        str(Config.EMAIL_SERVER or "").strip()
        and str(Config.EMAIL_SENDER or "").strip()
        and str(Config.EMAIL_PASSWORD or "").strip()
        and _smtp_username()
        and 1 <= port <= 65535
    )


def enviar_email(destinatario, assunto, corpo, corpo_html=None):
    """Envia e-mail transacional.

    corpo_html (opcional): quando informado, envia multipart/alternative com
    texto puro (corpo) + HTML próprio. Sem corpo_html, mantém o formato
    histórico: apenas o HTML gerado do texto escapado (comportamento
    inalterado para os chamadores existentes, ex.: checkout_recovery).
    """
    if not email_delivery_ready():
        raise RuntimeError("email_delivery_unavailable")

    servidor_smtp = str(Config.EMAIL_SERVER).strip()
    porta_smtp = int(Config.EMAIL_PORT)
    remetente = str(Config.EMAIL_SENDER).strip()
    senha = str(Config.EMAIL_PASSWORD)

    msg = MIMEMultipart("alternative")
    msg["From"] = formataddr((
        str(getattr(Config, "EMAIL_SENDER_NAME", "") or "").strip(),
        remetente,
    ))
    msg["To"] = destinatario
    msg["Subject"] = assunto

    if corpo_html is not None:
        # Texto puro + HTML: o cliente escolhe a melhor representação.
        msg.attach(MIMEText(corpo, "plain", "utf-8"))
        msg.attach(MIMEText(corpo_html, "html", "utf-8"))
    else:
        # O corpo chega como texto simples; o conteúdo é escapado antes de
        # virar HTML para impedir injeção de marcação vinda de campos de
        # usuário.
        corpo_html_gerado = (
            html.escape(corpo)
            .replace("\\r\\n", "\\n")
            .replace("\\n", "<br>\n")
        )
        msg.attach(MIMEText(corpo_html_gerado, "html", "utf-8"))

    try:
        context = ssl.create_default_context()
        if porta_smtp in (465, 2465):
            connection = smtplib.SMTP_SSL(
                servidor_smtp, porta_smtp, timeout=10, context=context
            )
        else:
            connection = smtplib.SMTP(servidor_smtp, porta_smtp, timeout=10)
        with connection as server:
            if porta_smtp not in (465, 2465):
                server.starttls(context=context)
            server.login(_smtp_username(), senha)
            server.send_message(msg, from_addr=remetente, to_addrs=[destinatario])
        logger.info("email_delivery sent=1")
    except Exception:
        logger.warning("email_delivery failed=1")
        raise RuntimeError("email_delivery_failed") from None
