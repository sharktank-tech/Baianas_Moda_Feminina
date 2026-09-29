# Loja de Roupas

Loja de roupas com estilização moderna e limpa com todas as funcionalidades de administração.

## Funcionalidades

- Gerenciamento de produtos, promoções, cupons e fornecedores
- Gerenciamento de usuários e favoritos
- Carrinho e checkout (buy now ou carrinho completo)
- Pagamento via Mercado Pago (Checkout Pro, webhook com assinatura)
- Rastreio de pedidos
- Newsletter, depoimentos e páginas institucionais
- Painel administrativo completo

## Tecnologias Utilizadas

- Flask (Python 3.10+)
- SQLAlchemy + PostgreSQL (UUID, JSONB)
- HTML / CSS / JavaScript (Tailwind CSS via CDN)
- Mercado Pago SDK

## Pré-requisitos

- Python 3.10+
- PostgreSQL (a aplicação **não** roda com SQLite: os modelos usam tipos `UUID`/`JSONB` do PostgreSQL)
- Chaves de ambiente (veja abaixo)

## Como Executar o Projeto

1. Clone o repositório:

    ```sh
    git clone https://github.com/sharktank-tech/PolloModas
    cd PolloModas
    ```

2. Crie um ambiente virtual:

    ```sh
    python -m venv venv
    source venv/bin/activate  # No Windows, use `venv\Scripts\activate`
    ```

3. Instale as dependências:

    ```sh
    pip install -r requirements.txt
    ```

4. Configure as variáveis de ambiente (crie um `.env` na raiz):

    ```sh
    DATABASE_URL=postgresql://usuario:senha@localhost:5432/pollomodas
    SECRET_KEY=uma-chave-secreta-forte
    EMAIL_SERVER=smtp.resend.com
    EMAIL_PORT=587
    EMAIL_USERNAME=resend
    EMAIL_SENDER=atendimento@pollomodas.com.br
    EMAIL_SENDER_NAME=PolloModas
    EMAIL_PASSWORD=<API_KEY_RESEND>
    EMAIL_DESTINE=contato@pollomodas.com.br
    MERCADO_PAGO_ACCESS_TOKEN=APP_USR-xxxxxxxx
    MERCADO_PAGO_WEBHOOK_SECRET=xxxxxxxx
    APP_PUBLIC_URL=http://127.0.0.1:5001
    MERCADO_PAGO_USE_SANDBOX=true
    ```

   Consulte [a configuração de e-mail Resend](docs/RESEND_EMAIL.md) para
   produção, prioridade de credenciais e validação de entrega.

5. Crie as tabelas do banco (necessário uma única vez):

    ```sh
    flask shell
    ```

    Dentro do shell:

    ```python
    from loja import db, create_app
    app = create_app()
    with app.app_context():
        db.create_all()
    ```

6. Crie o primeiro usuário administrador:

    ```sh
    flask create-admin --email admin@pollomodas.com.br --password 'senha-forte'
    ```

7. Execute a aplicação:

    ```sh
    python run.py
    ```

8. Acesse a aplicação no seu navegador:

    ```
    http://127.0.0.1:5001
    ```

## Notas de segurança e estoque

- `SECRET_KEY` e `DATABASE_URL` são obrigatórias e lidas do ambiente.
- Todos os formulários são protegidos contra CSRF (Flask-WTF); o webhook do
  Mercado Pago é a única rota isenta.
- O estoque é **reservado** na criação do pedido e devolvido quando o pagamento
  é recusado, cancelado, reembolsado ou permanece pendente por mais de 24h
  (liberação automática no webhook/retorno).

## Mudanças de schema (PostgreSQL)

O projeto não usa Flask-Migrate. Quando o modelo ganha uma coluna nova,
`db.create_all()` **não** a adiciona a tabelas existentes. Para aplicar:

```sh
# Opção 1 — executar o SQL de migração (veja migrations/):
psql "$DATABASE_URL" -f migrations/0002_stock_reserved.sql

# Opção 2 — deixar o app criar no primeiro boot (loja/schema.py)
```

Sempre rode os scripts em `migrations/` no banco de produção após o deploy.

## Testes

```sh
pytest -q tests/
```

## Deploy

- Heroku: `Procfile` usa `gunicorn run:app`.
- O webhook do Mercado Pago (`/pagamentos/webhook`) precisa ser público via HTTPS em produção.
