# PostgreSQL Baianas no Umbrel — implantação segura

## Limites de escopo
Este Compose instala somente o PostgreSQL da **As Baianas Moda Feminina**. Não utiliza PocketBase, PolloModas nem qualquer banco existente. Nenhum passo nesta documentação acessa serviços reais automaticamente.

## Pré-requisitos
- Umbrel com Docker e Docker Compose disponíveis via SSH.
- Backup do Umbrel e armazenamento suficiente para o novo volume.
- Uma senha aleatória longa, diferente de qualquer outra aplicação.

## Instalar (no terminal SSH do Umbrel)
Copie `postgres.compose.yml` para uma pasta própria, por exemplo `~/baianas-db/`.
Crie `~/baianas-db/.env` **somente no Umbrel**, com `BAIANAS_POSTGRES_PASSWORD=<senha_aleatoria_gerada_localmente>`, e proteja com `chmod 600 .env`.
Então execute:

```bash
docker compose -f postgres.compose.yml up -d
docker compose -f postgres.compose.yml ps
docker compose -f postgres.compose.yml exec -T baianas-postgres \
  psql -U baianas_app -d baianas_db -v ON_ERROR_STOP=1 \
  -c 'CREATE SCHEMA IF NOT EXISTS baianas_moda_feminina AUTHORIZATION baianas_app;'
```

Verifique o healthcheck e configure backup automatizado + teste de restauração. O volume `baianas_pgdata` contém os dados persistentes. Nunca rode `docker compose down -v` com dados reais.

## Conectividade Vercel: limite importante
A porta 5433 fica restrita a `127.0.0.1` no Umbrel. Essa configuração protege a instância e permite testar localmente, porém **não permite à Vercel conectar por DATABASE_URL diretamente**.

Para conectar com segurança, é necessário um gateway TCP privado/publicado com TLS, controle de acesso e ponte de rede (por exemplo uma VM/gateway com WireGuard para o Umbrel, proxy PostgreSQL TLS e limitação de acesso), ou hospedar o backend Flask dentro da rede Umbrel e expor somente uma API HTTPS autenticada. Não abrir 5432/5433 no roteador nem usar a IP doméstica em `DATABASE_URL`. A escolha do gateway deve ser aprovada e testada antes de implantar.

## Vercel
Projeto: `baianas-loja`. Verificar se `DATABASE_URL` aponta ao **gateway da Baianas**, com credenciais exclusivas, TLS e `sslmode=verify-full` (quando houver certificado de CA/hostname válidos); configurar `BAIANAS_DB_SCHEMA=baianas_moda_feminina`, `SECRET_KEY`, ambientes Production/Preview e redeploy.

Não copie segredos para GitHub ou logs. Os placeholders de `.env.example` não são credenciais.
