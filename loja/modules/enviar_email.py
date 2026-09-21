import html
import logging

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
from config import Config


logger = logging.getLogger(__name__)


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
        and 1 <= port <= 65535
    )


def enviar_email(destinatario, assunto, corpo):
    if not email_delivery_ready():
        raise RuntimeError("email_delivery_unavailable")

    servidor_smtp = str(Config.EMAIL_SERVER).strip()
    porta_smtp = int(Config.EMAIL_PORT)
    remetente = str(Config.EMAIL_SENDER).strip()
    senha = str(Config.EMAIL_PASSWORD)

    msg = MIMEMultipart()
    msg["From"] = remetente
    msg["To"] = destinatario
    msg["Subject"] = assunto

    # O corpo chega como texto simples; o conteúdo é escapado antes de virar
    # HTML para impedir injeção de marcação vinda de campos de usuário.
    corpo_html = (
        html.escape(corpo)
        .replace("\\r\\n", "\\n")
        .replace("\\n", "<br>\\n")
    )
    msg.attach(MIMEText(corpo_html, "html", "utf-8"))

    try:
        with smtplib.SMTP(servidor_smtp, porta_smtp) as server:
            server.starttls()
            server.login(remetente, senha)
            server.send_message(msg)
        logger.info("email_delivery sent=1")
    except Exception:
        logger.warning("email_delivery failed=1")
        raise RuntimeError("email_delivery_failed") from None
