import html
import logging

from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
from config import Config


logger = logging.getLogger(__name__)


def enviar_email(destinatario, assunto, corpo):
    servidor_smtp = Config.EMAIL_SERVER
    porta_smtp = Config.EMAIL_PORT
    remetente = Config.EMAIL_SENDER
    senha = Config.EMAIL_PASSWORD

    msg = MIMEMultipart()
    msg['From'] = remetente
    msg['To'] = destinatario
    msg['Subject'] = assunto

    # O corpo chega como texto simples; o conteúdo é escapado antes de virar
    # HTML para impedir injeção de marcação vinda de campos de usuário.
    corpo_html = (
        html.escape(corpo)
        .replace("\r\n", "\n")
        .replace("\n", "<br>\n")
    )
    msg.attach(MIMEText(corpo_html, 'html', 'utf-8'))

    try:
        with smtplib.SMTP(servidor_smtp, porta_smtp) as server:
            server.starttls()  # Inicializa a comunicação segura
            server.login(remetente, senha)
            server.send_message(msg)
        logger.info("E-mail enviado para %s com sucesso!", destinatario)
    except Exception as e:
        raise RuntimeError(f"Erro ao enviar e-mail: {e}")