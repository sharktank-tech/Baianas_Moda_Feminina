## O que muda

Ação comercial por variante no admin AliExpress (rascunho e publicado):
habilitar/desabilitar **uma variante para venda** preservando registros
e vínculos — pedidos, reservas e estoque da variante ficam intactos;
só `is_active` muda (e o agregado decorrente `Product.stock`).

- **Novo serviço** `loja/services/variant_availability.py`:
  trava `Product → ProductVariant` na mesma ordem do checkout,
  recarrega com `populate_existing`, valida pertencimento ao produto
  (`ValueError` com código estável, sem escrita parcial) e recalcula o
  agregado pela regra canônica (soma dos estoques das variantes **ativas**).
- **Rotas** `POST .../rascunho/<id>/disponibilidade` e
  `POST .../publicado/<id>/disponibilidade` (admin + sessão Ali,
  CSRF, `data-once`); formulário estrito (`disponivel` só aceita
  `"1"`/`"0"`, resto vira aviso sem efeito).
- **UI**: parcial `_variant_availability.html` incluída em
  `rascunho.html` e `renovar_frete.html` (tabela SKU/cor/tamanho/
  estoque/status + botão alternar).
- **Importação não reativa mais**: reimportação segue a mesma regra
  unilateral do sync — só desativa a variante que zerou no fornecedor;
  reativação é explícita no admin.
- **Restituição × disponibilidade** (`stock_reservations.py`): liberação
  por expiração e restituição compartilhada devolvem o estoque **à
  variante** (teto `supplier_stock − outras reservas` preservado), mas só
  somam no agregado quando a variante está **ativa**. Encerramento
  idempotente e ordem de locks preservados; reativação recalcula o
  agregado incorporando o devolvido.

## Garantias (20 testes SQLite + 3 PG novos)

- Desabilitar impede **novas** compras (`_reserve_stock` levanta
  `ValueError`) mas a **reserva ativa anterior sobrevive** (pedido
  intacto, quantidade ainda debitada).
- Sync e reimportação **nunca reativam** variante desativada pelo admin.
- Variante de outro produto / `variant_id` inválido / flag inválida →
  rejeitado sem alterar nada.
- Agregado sempre igual à soma das ativas (8 ⇄ 3 no cenário 5+3).
- Ciclo **reservar → desabilitar → liberar → repetir → reabilitar** nas
  duas vias (expiração e compartilhada), incluindo teto do fornecedor e
  outras reservas; segunda liberação não muda nada.
- Guards das rotas: sem permissão (302/“não possui permissão”), sem login
  (302 `/login`), sem sessão Ali (volta ao índice), CSRF inválido (400),
  GET (405).
- PG: ciclo em PostgreSQL real + corrida **disponibilidade × liberação ×
  checkout com sobreposição garantida** (pausa sob lock + barreira +
  evidência de espera; controle negativo prova que execução serial
  falha) — sem deadlock, checkout com sucessos contados e toda recusa
  validada como falta de estoque.

## Verificação

- Nova suíte SQLite: 20/20 (`test_aliexpress_variant_availability.py`).
- Nova suíte PG: 3/3 sem skips (`test_variant_availability_release_pg.py`,
  também no job `pytest-postgres` do CI com etapa de prova dedicada).
- Afetadas (SQLite): 257 passed (catalog, sync, resiliência, reservas,
  storefront, checkout guards, origem, backfill).
- Espelhos PG17 descartável (5433): 23 passed (concorrência do sync,
  import, supplier_stock, origem, cleanups/expiry + 3 novos).
- Suíte completa: **2313 passed, 109 skipped**.
- `git diff --check` limpo.

Sem merge/deploy; sem escrita em produção; sem migração (só `is_active`,
que já existe).
