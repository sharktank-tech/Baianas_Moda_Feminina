# As Baianas Moda Feminina

MVP de loja feminina, baseado no backend Flask/PostgreSQL existente, em fase de estabilização e separação do projeto original.

## Desenvolvimento local

1. `git clone https://github.com/sharktank-tech/Baianas_Moda_Feminina.git`.
2. `python -m venv .venv` e `pip install -r requirements-dev.txt`.
3. Copie `.env.example` para `.env`, gere uma chave aleatória exclusiva e configure um PostgreSQL exclusivo.
4. Configure `BAIANAS_DB_SCHEMA=baianas_moda_feminina`. Nunca compartilhe `DATABASE_URL`, credenciais ou schemas com outro projeto.
5. Crie o schema em banco vazio e controlado; execute o bootstrap somente com `BAIANAS_BOOTSTRAP_ON_STARTUP=1`.
6. Rode `python -m pytest -q tests/` e testes PostgreSQL antes do deploy.

**Aviso crítico:** o legado contém migrations e scripts não homologados para a Baianas. Não execute lote de migrations nem scripts DOWN em qualquer ambiente real antes da auditoria SQL integral. A simples criação do schema não autoriza pagamentos reais.

## MVP pretendido

Vitrine responsiva, catálogo real, sacola, checkout WhatsApp, admin básico, estoque e, posteriormente, Pix em sandbox. Integrações AliExpress herdadas não compõem o fluxo básico.

Leia [o plano](PLAN.md), [dados pendentes](TODO_DADOS.md), [backlog](BACKLOG.md) e [isolamento](docs/BAIANAS_ISOLAMENTO.md). Produtos, preços, logo, endereço e contatos só devem ser publicados após confirmação da loja.
