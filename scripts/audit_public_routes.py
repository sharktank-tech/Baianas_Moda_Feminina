"""Auditoria HTTP conservadora da loja Baianas.

Executar em ambiente de homologação com BAIANAS_BOOTSTRAP_ON_STARTUP=0:
  python scripts/audit_public_routes.py

Só dispara GETs de uma lista explícita de páginas públicas, sem alterar dados.
Não substitui testes de login, checkout, webhook, administração ou integrações.
"""
import sys
from run import app

PUBLIC = (
    "/", "/produtos", "/login", "/register", "/sobre",
    "/contato", "/politica-privacidade", "/trocas-e-devolucoes",
    "/condicoes-de-uso", "/robots.txt", "/sitemap.xml",
    "/static/img/favicon-baianas.svg",
)
failures = []
with app.test_client() as client:
    for path in PUBLIC:
        try:
            response = client.get(path, follow_redirects=False)
            status = response.status_code
            ok = 200 <= status < 400
            print(f"{'OK' if ok else 'FALHA'} {status} GET {path}")
            if not ok:
                failures.append(path)
        except Exception as exc:
            print(f"ERRO GET {path}: {type(exc).__name__}: {exc}")
            failures.append(path)

print(f"Rotas registradas no Flask: {len(list(app.url_map.iter_rules()))}")
print("Nota: rotas autenticadas, dinâmicas e métodos de escrita não foram executados.")
sys.exit(1 if failures else 0)
