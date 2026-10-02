"""Correção do primeiro endereço na edição de conta (flash falso de e-mail).

O POST /edit_account de um usuário SEM endereço montava o ``Address`` na
sessão antes de preencher as colunas NOT NULL; o lazy load de
``current_user.addresses`` (e o ``Address.query.update`` seguinte) disparava
autoflush → ``IntegrityError`` de NOT NULL → o ``except IntegrityError``
genérico exibia o flash falso "Não foi possível salvar. Verifique se o
e-mail já está cadastrado."

Cobertura:
- test_primeiro_endereco_salva_sem_flash_de_email: regressão exata do bug;
- test_primeiro_endereco_vira_principal_sem_checkbox: invariante "primeiro
  endereço sempre é principal";
- test_endereco_existente_edita_com_sucesso: caminho de edição já funcionava;
- test_email_duplicado_retorna_409_mensagem_de_outra_conta: pre-check;
- test_integrity_error_sem_conflito_de_email_usa_flash_generico: novo texto
  aprovado para violação que NÃO é de e-mail;
- test_cep_longo_retorna_400_sem_criar_endereco: CEP > 10 chars (coluna
  String(10)) estourava DataError no commit;
- test_campos_obrigatorios_do_endereco_retornam_400;
- test_sem_endereco_e_sem_dados_atualiza_somente_a_conta.
"""

import uuid

from sqlalchemy.exc import IntegrityError

from loja.modules.models import Address


def _flashes(client):
    with client.session_transaction() as sess:
        return list(sess.get("_flashes", []))


def _messages(client):
    return [message for _, message in _flashes(client)]


def _account_form(user, **overrides):
    form = {
        "username": user.name,
        "email": user.email,
        "phone": user.phone or "",
        "recipient_name": "Maria Silva",
        "address_phone": "11988887777",
        "zip_code": "01310100",
        "street": "Avenida Paulista",
        "number": "1000",
        "complement": "",
        "neighborhood": "Bela Vista",
        "city": "São Paulo",
        "state": "SP",
        "is_default": "1",
    }
    form.update(overrides)
    return form


def test_primeiro_endereco_salva_sem_flash_de_email(
    client, login, make_user, db
):
    """Regressão do bug: 1º endereço salvava 0% das vezes."""
    user = make_user()
    login(user.email)

    resp = client.post("/edit_account", data=_account_form(user))

    assert resp.status_code == 302
    messages = _messages(client)
    assert "Conta e endereço atualizados com sucesso!" in messages
    assert not any("e-mail já está cadastrado" in m for m in messages)

    address = Address.query.filter_by(user_id=user.id).one_or_none()
    assert address is not None
    assert address.recipient_name == "Maria Silva"
    assert address.street == "Avenida Paulista"
    assert address.zip_code == "01310100"
    assert address.city == "São Paulo"
    assert address.state == "SP"
    assert address.is_default is True


def test_primeiro_endereco_vira_principal_sem_checkbox(
    client, login, make_user, db
):
    user = make_user()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(user, is_default=""),
    )

    assert resp.status_code == 302
    address = Address.query.filter_by(user_id=user.id).one()
    assert address.is_default is True


def test_endereco_existente_edita_com_sucesso(client, login, make_user, db):
    user = make_user()
    existing = Address(
        id=uuid.uuid4(),
        user_id=user.id,
        recipient_name="João Souza",
        zip_code="01310100",
        street="Rua Antiga",
        number="10",
        neighborhood="Centro",
        city="São Paulo",
        state="SP",
        is_default=True,
    )
    db.session.add(existing)
    db.session.commit()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(user, street="Rua Nova"),
    )

    assert resp.status_code == 302
    assert "Conta e endereço atualizados com sucesso!" in _messages(client)

    db.session.expire_all()
    refreshed = db.session.get(Address, existing.id)
    assert refreshed.street == "Rua Nova"
    assert refreshed.is_default is True
    assert Address.query.filter_by(user_id=user.id).count() == 1


def test_email_duplicado_retorna_409_mensagem_de_outra_conta(
    client, login, make_user, db
):
    holder = make_user()
    user = make_user()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(user, email=holder.email),
    )

    assert resp.status_code == 409
    # Nos 400/409 o template consome os flashes (get_flashed_messages) — o
    # texto visível ao usuário está no HTML da resposta.
    assert "Este e-mail já está sendo usado por outra conta." in resp.get_data(
        as_text=True
    )


def test_integrity_error_sem_conflito_de_email_usa_flash_generico(
    client, login, make_user, db, monkeypatch
):
    user = make_user()
    login(user.email)

    def _boom():
        raise IntegrityError(
            "INSERT INTO addresses ...",
            {},
            Exception("NOT NULL constraint failed: addresses.recipient_name"),
        )

    monkeypatch.setattr(db.session, "commit", _boom)
    resp = client.post("/edit_account", data=_account_form(user))

    assert resp.status_code == 409
    page = resp.get_data(as_text=True)
    assert (
        "Não foi possível salvar os dados. "
        "Verifique os campos e tente novamente."
    ) in page
    assert "e-mail já está cadastrado" not in page


def test_cep_longo_retorna_400_sem_criar_endereco(
    client, login, make_user, db
):
    user = make_user()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(user, zip_code="01310-100123"),
    )

    assert resp.status_code == 400
    assert "CEP muito longo." in resp.get_data(as_text=True)
    assert Address.query.filter_by(user_id=user.id).count() == 0


def test_campos_obrigatorios_do_endereco_retornam_400(
    client, login, make_user, db
):
    user = make_user()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(
            user,
            recipient_name="Maria",
            zip_code="",
            street="",
            number="",
            neighborhood="",
            city="",
            state="",
        ),
    )

    assert resp.status_code == 400
    assert "Preencha os campos obrigatórios do endereço:" in resp.get_data(
        as_text=True
    )
    assert Address.query.filter_by(user_id=user.id).count() == 0


def test_sem_endereco_e_sem_dados_atualiza_somente_a_conta(
    client, login, make_user, db
):
    user = make_user()
    login(user.email)

    resp = client.post(
        "/edit_account",
        data=_account_form(
            user,
            recipient_name="",
            address_phone="",
            zip_code="",
            street="",
            number="",
            complement="",
            neighborhood="",
            city="",
            state="",
            is_default="",
        ),
    )

    assert resp.status_code == 302
    assert "Conta e endereço atualizados com sucesso!" in _messages(client)
    assert Address.query.filter_by(user_id=user.id).count() == 0
