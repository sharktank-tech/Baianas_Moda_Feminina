# Isolamento Baianas x PolloModas

Nunca compartilhar banco, roles, schema, credenciais, buckets, SMTP ou Mercado Pago. Usar banco/projeto Supabase exclusivo para a Baianas e schema `baianas_moda_feminina`.

O código foi herdado. Migrations SQL ainda exigem validação integral antes da execução. Não execute scripts legados no banco PolloModas. Não execute scripts `down` sem backup e ensaio de recuperação.

Desativar integrações de fornecedores desnecessárias ao MVP. Verificar domínio, política de cookies, e-mails, webhook e permissões antes de disponibilizar produção.

Gates: testes PostgreSQL em banco separado; schema anterior intocado; checkout, webhook, estoque, status e admin testados; identidade comercial revisada; dados reais confirmados. Nenhum gate está implicitamente aprovado por este documento.
