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
    EMAIL_SERVER=smtp.seuprovedor.com
    EMAIL_PORT=587
    EMAIL_SENDER=contato@pollomodas.com.br
    EMAIL_PASSWORD=sua-senha
    EMAIL_DESTINE=contato@pollomodas.com.br
    MERCADO_PAGO_ACCESS_TOKEN=APP_USR-xxxxxxxx
    MERCADO_PAGO_WEBHOOK_SECRET=xxxxxxxx
    APP_PUBLIC_URL=http://127.0.0.1:5001
    MERCADO_PAGO_USE_SANDBOX=true
    ```

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

6. Execute a aplicação:

    ```sh
    python run.py
    ```

7. Acesse a aplicação no seu navegador:

    ```
    http://127.0.0.1:5001
    ```

## Testes

```sh
pytest -q tests/
```

## Deploy

- Heroku: `Procfile` usa `gunicorn run:app`.
- O webhook do Mercado Pago (`/pagamentos/webhook`) precisa ser público via HTTPS em produção.