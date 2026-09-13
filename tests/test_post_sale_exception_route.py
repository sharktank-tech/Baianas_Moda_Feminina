def test_exception_queue_admin_only(client, admin_user, login):
    login(admin_user.email, "senha123")
    response = client.get("/admin/integrations/aliexpress/pedidos/exceptions")
    assert response.status_code == 200
    body = response.get_data(as_text=True)
    assert "Exceções e fulfillment estagnado" in body
    assert "Somente dados operacionais sanitizados" in body


def test_exception_queue_anonymous_redirect(client):
    response = client.get("/admin/integrations/aliexpress/pedidos/exceptions")
    assert response.status_code == 302
    assert response.headers["Location"].endswith("/login")


def test_exception_queue_non_admin_denied(client, regular_user, login):
    login(regular_user.email, "senha123")
    response = client.get(
        "/admin/integrations/aliexpress/pedidos/exceptions",
        follow_redirects=True,
    )
    assert response.status_code == 200
    assert "Exceções e fulfillment estagnado" not in response.get_data(
        as_text=True
    )


def test_invalid_stale_config_blocks_queue(
    client, admin_user, login, monkeypatch
):
    monkeypatch.setenv("FULFILLMENT_STALE_AFTER_HOURS", "invalid")
    login(admin_user.email, "senha123")
    response = client.get("/admin/integrations/aliexpress/pedidos/exceptions")
    assert response.status_code == 200
    assert "Configuração inválida" in response.get_data(as_text=True)


def test_exception_queue_is_get_only(client, admin_user, login):
    login(admin_user.email, "senha123")
    response = client.post(
        "/admin/integrations/aliexpress/pedidos/exceptions"
    )
    assert response.status_code == 405
