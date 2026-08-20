import uuid

import click
from flask.cli import with_appcontext

from loja import db
from loja.modules.models import User


@click.command("create-admin")
@click.option("--email", required=True, prompt=True, help="E-mail do admin.")
@click.option(
    "--password",
    required=True,
    prompt=True,
    hide_input=True,
    confirmation_prompt=True,
    help="Senha do admin.",
)
@click.option("--name", default="Administrador", help="Nome do admin.")
@with_appcontext
def create_admin(email, password, name):
    """Cria o primeiro usuário administrador da loja."""
    existing = User.query.filter_by(email=email.strip().lower()).first()
    if existing:
        click.echo(f"Usuário {email} já existe.")
        return

    user = User(
        id=uuid.uuid4(),
        name=name,
        email=email.strip().lower(),
        phone="",
        is_admin=True,
    )
    user.set_password(password)
    db.session.add(user)
    db.session.commit()
    click.echo(f"Administrador {email} criado com sucesso.")