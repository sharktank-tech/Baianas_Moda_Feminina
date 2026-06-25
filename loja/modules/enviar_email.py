from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText
import smtplib
from config import Config

def enviar_email(destinatario, assunto, corpo):
    servidor_smtp = Config.EMAIL_SERVER
    porta_smtp = Config.EMAIL_PORT
    remetente = Config.EMAIL_SENDER
    senha = Config.EMAIL_PASSWORD

    msg = MIMEMultipart()
    msg['From'] = remetente
    msg['To'] = destinatario
    msg['Subject'] = assunto

    # Alterar o tipo para 'html' para permitir conteúdo formatado
    msg.attach(MIMEText(corpo, 'html'))

    try:
        with smtplib.SMTP(servidor_smtp, porta_smtp) as server:
            server.starttls()  # Inicializa a comunicação segura
            server.login(remetente, senha)
            server.send_message(msg)
            print(f"E-mail enviado para {destinatario} com sucesso!")
    except Exception as e:
        raise RuntimeError(f"Erro ao enviar e-mail: {e}")
