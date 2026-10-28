"""Resend transport and real service integration; SMTP is always simulated."""
import importlib
import runpy
import ssl
from pathlib import Path

import pytest

from config import Config

mail = importlib.import_module('loja.modules.enviar_email')


@pytest.fixture
def smtp(monkeypatch):
    for key, value in {
        'EMAIL_SERVER': 'smtp.resend.com', 'EMAIL_PORT': 587,
        'EMAIL_USERNAME': None, 'EMAIL_PASSWORD': 're_test_not_a_real_key',
        'EMAIL_SENDER': 'atendimento@baianasmodafeminina.com.br',
        'EMAIL_SENDER_NAME': 'As Baianas Moda Feminina',
    }.items():
        monkeypatch.setattr(Config, key, value)
    events = []

    class SMTP:
        def __init__(self, host, port, **kwargs):
            events.append(('connect', host, port, kwargs))
        def __enter__(self):
            return self
        def __exit__(self, *args):
            return False
        def starttls(self, **kwargs):
            events.append(('tls', kwargs))
        def login(self, username, password):
            events.append(('login', username, password))
        def send_message(self, message, **kwargs):
            events.append(('send', message, kwargs))

    monkeypatch.setattr(mail.smtplib, 'SMTP', SMTP)
    monkeypatch.setattr(mail.smtplib, 'SMTP_SSL', SMTP)
    return events


def test_resend_identity_tls_and_alternative(smtp):
    mail.enviar_email('customer@example.test', 'Recuperação', 'Texto', '<p>HTML</p>')
    assert [e[0] for e in smtp] == ['connect', 'tls', 'login', 'send']
    assert smtp[0][1:3] == ('smtp.resend.com', 587)
    assert smtp[0][3]['timeout'] == 10
    context = smtp[1][1]['context']
    assert context.verify_mode == ssl.CERT_REQUIRED and context.check_hostname
    assert smtp[2][1:] == ('resend', 're_test_not_a_real_key')
    message, envelope = smtp[-1][1:]
    assert message['From'] == 'As Baianas Moda Feminina <atendimento@baianasmodafeminina.com.br>'
    assert envelope == {'from_addr': 'atendimento@baianasmodafeminina.com.br',
                        'to_addrs': ['customer@example.test']}
    assert message.get_content_type() == 'multipart/alternative'
    assert [p.get_content_type() for p in message.get_payload()] == ['text/plain', 'text/html']


@pytest.mark.parametrize('port', [465, 2465])
def test_implicit_tls(smtp, monkeypatch, port):
    monkeypatch.setattr(Config, 'EMAIL_PORT', port)
    mail.enviar_email('customer@example.test', 'Assunto', 'Texto')
    assert [e[0] for e in smtp] == ['connect', 'login', 'send']
    assert smtp[0][3]['context'].verify_mode == ssl.CERT_REQUIRED


@pytest.mark.parametrize('configured,expected', [(None, 'legacy@example.test'), ('custom-login', 'custom-login')])
def test_legacy_and_separate_username(smtp, monkeypatch, configured, expected):
    monkeypatch.setattr(Config, 'EMAIL_SERVER', 'smtp.example.test')
    monkeypatch.setattr(Config, 'EMAIL_SENDER', 'legacy@example.test')
    monkeypatch.setattr(Config, 'EMAIL_USERNAME', configured)
    mail.enviar_email('customer@example.test', 'Assunto', '<script>texto</script>')
    assert smtp[2][1] == expected
    payload = smtp[-1][1].get_payload()[0].get_payload(decode=True).decode()
    assert '&lt;script&gt;' in payload and '<script>' not in payload


@pytest.mark.parametrize('key,value', [('EMAIL_PASSWORD', ''), ('EMAIL_USERNAME', ''), ('EMAIL_SENDER', ''), ('EMAIL_PORT', 0), ('EMAIL_PORT', 'invalid')])
def test_missing_config_fails_before_network(smtp, monkeypatch, key, value):
    monkeypatch.setattr(Config, key, value)
    assert not mail.email_delivery_ready()
    with pytest.raises(RuntimeError, match='email_delivery_unavailable'):
        mail.enviar_email('customer@example.test', 'Assunto', 'Texto')
    assert smtp == []


def test_tls_failure_never_logs_secrets_or_authenticates(smtp, monkeypatch, caplog):
    def fail(*args, **kwargs):
        raise RuntimeError('re_test_not_a_real_key token=private-token')
    monkeypatch.setattr(mail.smtplib.SMTP, 'starttls', fail)
    with pytest.raises(RuntimeError, match='email_delivery_failed') as error:
        mail.enviar_email('customer@example.test', 'Assunto', 'token=private-token')
    assert [e[0] for e in smtp] == ['connect']
    assert 're_test' not in caplog.text and 'private-token' not in caplog.text
    assert 'private-token' not in str(error.value)


def test_reset_dispatch_waits_for_smtp_by_default(app, smtp, monkeypatch):
    from loja.services.password_reset import _dispatch_email
    monkeypatch.delitem(app.config, 'PASSWORD_RESET_SEND_ASYNC', raising=False)
    with app.app_context():
        _dispatch_email('customer@example.test', 'https://www.baianasmodafeminina.com.br/redefinir-senha?token=fake')
    assert smtp[-1][0] == 'send'
    assert 'atendimento@baianasmodafeminina.com.br' in smtp[-1][1]['From']
    assert len(smtp[-1][1].get_payload()) == 2


def test_newsletter_uses_shared_sender(app, smtp):
    import uuid

    from loja import db
    from loja.modules.models import NewsletterSubscriber
    from loja.modules.newsletter import _send_welcome_email

    subscriber = NewsletterSubscriber(
        id=uuid.uuid4(),
        email="customer@example.test",
    )
    with app.app_context():
        db.session.add(subscriber)
        db.session.commit()
        assert _send_welcome_email(subscriber)
    assert smtp[-1][1]['From'] == 'As Baianas Moda Feminina <atendimento@baianasmodafeminina.com.br>'


def test_reset_request_reaches_resend_transport(client, make_user, app, smtp, monkeypatch):
    monkeypatch.setitem(app.config, 'APP_PUBLIC_URL', 'https://www.baianasmodafeminina.com.br')
    monkeypatch.delitem(app.config, 'PASSWORD_RESET_SEND_ASYNC', raising=False)
    user = make_user()
    # The suite shares committed rate-limit rows. Give this transport test
    # its own documentation-only IP instead of inheriting localhost's quota.
    response = client.post('/esqueci-senha', data={'email': user.email},
                           headers={'X-Forwarded-For': '192.0.2.79'})
    assert response.status_code == 302
    assert smtp[-1][0] == 'send'
    message = smtp[-1][1]
    assert message['To'] == user.email
    body = message.get_payload()[0].get_payload(decode=True).decode()
    assert 'https://www.baianasmodafeminina.com.br/redefinir-senha?token=' in body


@pytest.mark.parametrize('server,password,expected', [
    ('smtp.resend.com', None, 're_fake'),
    ('smtp.resend.com', 'explicit-secret', 'explicit-secret'),
    ('smtp.other.test', None, None),
])
def test_api_key_fallback_only_for_resend(monkeypatch, server, password, expected):
    monkeypatch.setenv('EMAIL_SERVER', server)
    monkeypatch.setenv('RESEND_API_KEY', 're_fake')
    monkeypatch.delenv('EMAIL_PASSWORD', raising=False)
    monkeypatch.delenv('EMAIL_SENDER', raising=False)
    if password:
        monkeypatch.setenv('EMAIL_PASSWORD', password)
    config = runpy.run_path(str(Path(__file__).resolve().parents[1] / 'config.py'))['Config']
    assert config.EMAIL_PASSWORD == expected
    assert config.EMAIL_SENDER == 'atendimento@baianasmodafeminina.com.br'
