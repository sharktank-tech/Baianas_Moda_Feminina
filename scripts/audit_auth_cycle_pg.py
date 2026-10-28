"""Teste E2E isolado de cadastro e sessão em schema PostgreSQL descartável.

Execute em container TEMPORÁRIO com as credenciais do banco da Baianas;
este script força BAIANAS_DB_SCHEMA para um nome aleatório permitido,
executa bootstrap só nesse schema e remove APENAS esse schema ao sair.
NÃO use com banco contendo outro usuário com permissões globais.
"""
import os
import re
import sys
import uuid
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
schema = "baianas_smoke_auth_" + uuid.uuid4().hex[:12]
os.environ["BAIANAS_DB_SCHEMA"] = schema
os.environ["BAIANAS_ALLOW_TEST_SCHEMA"] = "1"
os.environ["BAIANAS_BOOTSTRAP_ON_STARTUP"] = "1"
os.environ["VERCEL"] = ""
os.environ["FLASK_DEBUG"] = "0"

from sqlalchemy import text
from run import app
from loja import db
from loja.modules.models import User

failures = []
def check(label, condition):
    print(f"{'OK' if condition else 'FALHA'} {label}")
    if not condition:
        failures.append(label)

def csrf(client, path):
    page = client.get(path)
    check("GET " + path, page.status_code == 200)
    m = re.search(r'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', page.get_data(as_text=True))
    if not m:
        raise RuntimeError("Token CSRF ausente de " + path)
    return m.group(1)

test_email = "smoke-auth-" + uuid.uuid4().hex[:8] + "@example.invalid"
password = "Teste-Apenas-Local-82917!"
try:
    with app.test_client() as client:
        token = csrf(client, "/register")
        fields = dict(username="Auditoria Baianas", email=test_email,
                      telefone="63999999999", password=password,
                      confirm_password="OUTRA SENHA", csrf_token=token)
        mismatch = client.post("/register", data=fields)
        check("Cadastro recusa senhas divergentes", mismatch.status_code in (302, 303))
        with app.app_context():
            check("Nenhum usuario criado com senha divergente",
                  User.query.filter_by(email=test_email).count() == 0)

        fields["confirm_password"] = password
        fields["csrf_token"] = csrf(client, "/register")
        signup = client.post("/register", data=fields, follow_redirects=False)
        check("Cadastro redireciona ao login", signup.status_code in (302, 303)
              and "/login" in signup.headers.get("Location", ""))
        with app.app_context():
            user = User.query.filter_by(email=test_email).one_or_none()
            check("Usuario criado no schema descartavel", user is not None)
            if user:
                check("Senha armazenada como hash", user.password != password
                      and user.check_password(password))
                check("Usuario sem privilegios admin", user.is_admin is False)
        token = csrf(client, "/register")
        dupe = client.post("/register", data={**fields, "csrf_token": token})
        with app.app_context():
            check("Cadastro duplicado nao cria novo usuario",
                  User.query.filter_by(email=test_email).count() == 1)

        bad = client.post("/login", data={"csrf_token": csrf(client, "/login"),
                         "email": test_email, "password": "senha-incorreta"})
        check("Login com senha incorreta rejeitado", bad.status_code in (302, 303))
        check("Conta ainda protegida", client.get("/conta").status_code in (302, 303, 401, 403))

        good = client.post("/login", data={"csrf_token": csrf(client, "/login"),
                          "email": test_email, "password": password}, follow_redirects=False)
        check("Login valido redireciona para conta", good.status_code in (302, 303)
              and "/conta" in good.headers.get("Location", ""))
        check("Conta acessivel autenticado", client.get("/conta").status_code == 200)
        logout = client.get("/logout", follow_redirects=False)
        check("Logout redireciona", logout.status_code in (302, 303))
        check("Conta bloqueada apos logout", client.get("/conta").status_code in (302, 303, 401, 403))
finally:
    # Evita deletar qualquer schema fora do namespace descartavel.
    if re.fullmatch(r"baianas_smoke_auth_[0-9a-f]{12}", schema):
        with app.app_context():
            db.session.remove()
            with db.engine.connect() as conn:
                conn.execute(text(f'DROP SCHEMA IF EXISTS "{schema}" CASCADE'))
                conn.commit()
        print("Schema descartavel removido:", schema)

print("Total de verificacoes com falha:", len(failures))
sys.exit(1 if failures else 0)
