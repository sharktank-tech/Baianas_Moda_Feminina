"""Auditoria de autenticação SEM criar usuários, enviar e-mail ou processar compras.

Execução: python scripts/audit_auth_routes.py
Testa páginas GET, proteção anônima, formulário CSRF e rejeição de login inválido.
Teste E2E de cadastro/login REAL requer banco de testes isolado.
"""
import re
import sys
from pathlib import Path
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from run import app

failures = []
def check(label, response, valid):
    code = response.status_code
    ok = code in valid
    print(f"{'OK' if ok else 'FALHA'} {code} {label} (esperado {sorted(valid)})")
    if not ok:
        failures.append(label)

with app.test_client() as client:
    for path in ("/login", "/register", "/esqueci-senha"):
        check(f"GET {path}", client.get(path), {200})
    for path in ("/conta", "/cart", "/encomendas", "/admin"):
        response = client.get(path, follow_redirects=False)
        # Um anônimo jamais deve receber uma página protegida (200).
        check(f"GET anonimo {path}", response, {302, 303, 401, 403})
        if response.status_code in (302, 303):
            location = response.headers.get("Location", "")
            if "/login" not in location and "/entrar" not in location:
                print(f"AVISO: redirecionamento de {path} para {location}")
    page = client.get("/login")
    html = page.get_data(as_text=True)
    token_match = re.search(r'<input[^>]*name="csrf_token"[^>]*value="([^"]+)"', html)
    if not token_match:
        print("FALHA formulario de login sem token CSRF detectavel")
        failures.append("csrf do login")
    else:
        token = token_match.group(1)
        response = client.post("/login", data={
            "csrf_token": token,
            "email": "usuario-inexistente@invalid.example",
            "password": "senha-invalida-auditoria",
        }, follow_redirects=False)
        check("POST login inexistente", response, {302, 303})
        # Credenciais invalidas nao devem criar sessao autenticada.
        protected = client.get("/conta", follow_redirects=False)
        check("GET conta apos login invalido", protected, {302, 303, 401, 403})

print("Teste seguro: nenhum usuario cadastrado e nenhum e-mail enviado.")
print("Cadastro/login reais e exclusao/edicao exigem ambiente descartavel.")
sys.exit(1 if failures else 0)
