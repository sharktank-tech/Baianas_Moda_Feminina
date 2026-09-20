# Registro privado de evidências do canário

O serviço `loja.services.canary_evidence` registra um caso por pedido e eventos de
checagem técnica, conferência financeira, confirmação do cliente e decisão.
Cada caso tem finalidade imutável: `simulation` (padrão) ou
`organic_candidate` (seleção explícita pelo técnico autorizado). O serviço
recusa decisão `confirmed` em casos `simulation`. Marcar `organic_candidate`
é uma classificação humana inicial, não uma prova de origem: a decisão ainda
exige conferências independentes.
O estado `confirmed` **não autoriza compra**, não muda o verificador de somente
leitura, não habilita gates e não chama AliExpress. A autorização financeira de
uma compra específica permanece um procedimento separado.

## Acesso e dados

- API interna `/admin/canary-evidence/cases`, `/cases/events` e
  `/cases/<uuid>`, atendida pelo backend autenticado com CSRF nos POSTs.
  `CANARY_EVIDENCE_REGISTER_ENABLED` permanece diferente de `1` até
  instalação controlada das tabelas, trigger e atribuições de função;
  enquanto desligado, as rotas devolvem 404 e não consultam tabelas.
- Exige `User.is_admin` e associação explícita do UUID do usuário, configurada
  **somente no servidor**, em `CANARY_EVIDENCE_TECHNICAL_USER_IDS`,
  `CANARY_EVIDENCE_FINANCE_USER_IDS`, `CANARY_EVIDENCE_SUPPORT_USER_IDS` e
  `CANARY_EVIDENCE_REVIEWER_USER_IDS`. Lista vazia nega acesso. Conceder ou
  revogar função exige alterar configuração controlada, não existe endpoint
  para autoatribuição. Auditar alterações dessa configuração no sistema de
  implantação.
- Eventos de pagamento e atendimento aceitam apenas uma referência UUID
  opaca gerada no sistema autorizado de origem; não armazenar URLs, texto
  livre, comprovantes, capturas de tela, CPF, contato ou payload de provedor.
  A origem da referência deve ser conferida pelo humano autorizado: um UUID
  isolado não prova pagamento ou contato com o cliente.
- GET retorna apenas tipo, resultado, horário e estado; não expõe referência,
  actor ID, pedido ou dados do cliente. O caso e eventos ficam no schema
  exclusivo configurado em `POLLOMODAS_SCHEMA` e não em `public`.
- `canary_evidence_events` tem trigger PostgreSQL que rejeita UPDATE e DELETE.
  O dono do banco ainda pode alterar triggers; preservar backups, auditoria
  de acesso e privilégios mínimos na operação. Não é prova criptográfica de
  inviolabilidade.

## Instalação e verificação

Os modelos são importados antes de `db.create_all()` no bootstrap controlado.
No servidor, o cold start não executa DDL. Preparar uma migração revisada
para o schema dedicado antes de implantar endpoints; inspecionar tabelas,
índices, checks, FKs e trigger. Nenhum passo deste trabalho aplica DDL em
produção. Usuários do navegador não recebem credenciais SQL nem papéis de
escrita no schema privado. A configuração de `search_path` deve apontar ao
schema do projeto e nunca a `public`.

O job PostgreSQL do CI executa os testes no banco `pollotest`, em schema
descartável `pollo_test_*`, com roles, rejeição de entradas sensíveis, estorno,
concorrência de decisões e trigger de imutabilidade. Não substitui revisão
das concessões SQL do ambiente de implantação nem a checagem humana de
origem orgânica. Não registrar identificadores completos na PR.

## Compra simulada por conta distinta

Uma compra feita deliberadamente para teste, ainda que a conta compradora do
Mercado Livre seja diferente da conta da loja, deve ser registrada como
`simulation`. A identidade distinta não demonstra origem espontânea, nem
substitui a confirmação de pagamento vigente no Mercado Pago e a checagem
independente pelo atendimento. O serviço rejeita decisão `confirmed` para
esse caso; ele não é candidato ao canário orgânico. Registrar resultados do
teste sem dados pessoais; manter o verificador com confirmação orgânica
pendente, gates desligados e nenhuma submissão ao fornecedor.
