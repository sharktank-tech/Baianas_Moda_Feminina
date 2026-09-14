import re
import uuid


def _extract_csrf_token(html):
    match = re.search(
        r'name="csrf_token"\s+value="([^"]+)"',
        html,
    )
    assert match, "Token CSRF não encontrado na página renderizada."
    return match.group(1)


def test_post_without_csrf_token_is_rejected(app, client):
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = client.post(
            "/login",
            data={"email": "x@x.com", "password": "x"},
        )
        assert resp.status_code == 400
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_login_flow_with_csrf_token(app, client, make_user):
    user = make_user()

    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = client.get("/login")
        token = _extract_csrf_token(resp.get_data(as_text=True))

        resp = client.post(
            "/login",
            data={
                "email": user.email,
                "password": "senha123",
                "csrf_token": token,
            },
            follow_redirects=False,
        )
        assert resp.status_code == 302
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_webhook_is_exempt_from_csrf(app, client):
    # Sem a isenção, o webhook seria rejeitado com 400. Como está isento,
    # um payload sem data.id no query é malformado/desconhecido e é ignorado
    # com 200 (IPN legado/malformado não exige assinatura).
    app.config["WTF_CSRF_ENABLED"] = True
    try:
        resp = client.post(
            "/pagamentos/webhook",
            json={"type": "payment", "data": {"id": "123"}},
        )
        assert resp.status_code == 200
        assert resp.get_json() == {"ok": True}
    finally:
        app.config["WTF_CSRF_ENABLED"] = False


def test_newsletter_form_contains_csrf_token(client):
    resp = client.get("/")
    html = resp.get_data(as_text=True)
    assert 'name="csrf_token"' in html
    _extract_csrf_token(html)
