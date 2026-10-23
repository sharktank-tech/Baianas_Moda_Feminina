## O que muda

Somente documentação: novo `docs/ROADMAP_OPERACAO_ALIEXPRESS.md` (255 linhas,
1 arquivo, +255/−0). Nenhum código, migração, configuração, dado, publicação,
compra ou notificação foi criado ou alterado.

- Checkpoint datado do piloto `1005010478234256` separando **relatos do
  usuário** (publicado/PRONTO, R$ 58,00, frete R$ 10,00, checkout R$ 68,00,
  margens 14,16%, prazo 7–10 dias) de **verificações do agente**,
  apresentados como **sequência de etapas** (estado posterior como relato
  do usuário, sem releitura independente) — sem tratar a cotação como
  permanentemente válida.
- 8 implementações futuras em ordem (assistente de cadastro → preço por
  margem → cotações → revisão/publicação → fila de despacho → compra
  assistida → rastreamento → compra automática controlada), cada uma com
  problema, escopo, dependências, serviços existentes a reaproveitar
  (verificados no código: `importer`, `pricing`, `unit_economics`,
  `supplier_freight_quotes`, `publication`, `variant_availability`,
  `stock_reservations`, fulfillment, `customer_tracking`, `enviar_email`),
  aceite, testes e limites da automação.
- Regras preservadas (evidência com escopo, frete-receita × frete-custo,
  sem ampliação automática, sem zero implícito, sem reativação automática,
  publicação ≠ validação, idempotência, reconciliação em incerteza, PII).
- Decisões abertas: frete ao cliente, base da margem, geografia, renovação
  de cotações, limites da compra automática. Simulação financeira existente
  intocada; primeira venda segue pelo `FIRST_REAL_SALE_RUNBOOK.md`.

## Verificação

- `git diff --check` limpo; sem testes de aplicação (fora do escopo:
  só documentação).
- Sem merge/deploy; sem alteração em produção.

Observações: não há `AGENTS.md` nem índice de docs no repo (registrado no
documento); sem planos duplicados (relaciona `FIRST_REAL_SALE_RUNBOOK.md`,
`MONETIZATION_ROADMAP.md` e docs AliExpress existentes sem substituí-los).
