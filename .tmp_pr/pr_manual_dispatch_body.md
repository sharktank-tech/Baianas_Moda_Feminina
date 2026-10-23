## Interface de compra manual + despacho (sem automação)

Registra compras **já realizadas** pelo operador e seus rastreios, dentro
do detalhe do pedido (`/admin/pedidos/<uuid>`): seções Compra no fornecedor,
Despacho e rastreio, Histórico operacional. Salvar **nunca** compra no
AliExpress, consulta o fornecedor ou habilita automação.

## Decisão sobre reutilização dos modelos

- **Reutilizados**: `Order`/`OrderItem` (quantidades vendidas, vínculos por
  UUID), `payment_is_confirmed` (prova de pagamento), `Shipment` (+
  `tracking_code` único, status inicial `aguardando_postagem`) e
  `TrackingEvent`, regras de normalização do `TrackingService` — mas **não**
  seu `commit` interno (aqui só `flush`, transação única atômica).
- **Novos (migration 0039)**: `AliExpressOrder` **não** foi reutilizado de
  propósito — seus estados pertencem à máquina do fulfillment automático e
  não serão preenchidos só para caber o registro manual. Novas tabelas:
  `manual_supplier_purchases` (+`_items`), `shipment_items` (cobertura),
  `shipments.manual_purchase_id`, `manual_operational_audit` (correções com
  antes/depois, motivo e autor; sem exclusão física).
- Detalhe de implementação: `shipments.id`/`tracking_events.id` são
  BigInteger sem default no SQLite — o serviço gera 63 bits aleatórios
  (prod tem sequence; sem colisão entre faixas; `max()+1` teria corrida).

## Estados elegíveis e regras

- Registro exige **pagamento comprovado + `status == paid`**; bloqueia
  cancelled/refunded/charged_back/manual_review/pending/payment_failed/expired.
- Teto por item (soma registrada ≤ vendida) e por cobertura (soma das
  remessas ≤ vinculada à compra); compras divididas e múltiplas remessas OK.
- Idempotência por **conteúdo idêntico** (itens, data, fuso, evidência,
  transportadora, previsão): repetição retorna o existente; divergência
  exige correção explícita (`purchase_content_differs` /
  `shipment_content_differs`). Mesmo código em outra compra (ainda que do
  mesmo pedido) = conflito, sem escrita parcial. `UNIQUE` no banco + locks
  `Order → filhos` (`of=` no PG pelos joins `lazy`).
- Correções (número, transportadora, rastreio): justificativa obrigatória,
  auditoria, sem efeitos operacionais; **versão obrigatória e exata**
  (microssegundos, sob lock); `IntegrityError` vira aviso claro sem 500,
  com registro e auditoria intactos.
- Evidência e autoria ficam em campos privados/auditoria; telas públicas
  mostram só descrições genéricas + código/transportadora/estados.
- Conclusão do pedido exige **cobertura entregue de todas as unidades**
  ("vinculado" ≠ "entregue"); parciais deixam pendência explícita.
- Formulários ignoram quantidades zero (parciais possíveis), recusam
  negativas/inválidas e exigem ao menos uma positiva.
- Cliente (`/encomendas`): só transportadora/rastreio/estados públicos,
  isolamento por dono, pendências explícitas; nada operacional vaza.

## Testes e checks

- `tests/test_manual_dispatch.py`: **31 passed** (registro, elegibilidade,
  itens/tetos/campos, divididas/parciais, idempotência por conteúdo,
  duplicados, correções + versão obrigatória/exata/stale, IntegrityError
  nas duas rotas sem 500, zeros no form, evidência fora das telas públicas,
  bucket total/parcial/multi-item, rotas/permissões/CSRF/métodos,
  isolamento do cliente, sem chamadas externas nem mutação financeira).
- `tests/test_manual_dispatch_pg.py`: **6 passed, sem skip** — migração
  desde schema legado (aplica 2×, tipos/nulidade/PKs/FKs/unicidade/checks/
  índices, registros preservados) + validador completo com controles
  negativos; 4 corridas com sobreposição garantida (pausa sob lock):
  identificador repetido e saldo disputado, com quantidades finais exatas.
- Incluído no job `pytest-postgres` do CI. Suíte completa: **2392 passed**;
  `git diff --check` limpo.
- Sem merge; migração **não aplicada em produção**; nenhum registro real.

## Migração e aplicação (NÃO executar nesta rodada)

- Aplicar `migrations/0039_manual_dispatch.sql` no schema `pollomodas`
  (aditiva, idempotente); conferir com `migrations/validate_0039.py`
  (colunas/tipos/nulidade, PKs, FKs, unicidade, checks, índices);
  reversão em `0039_manual_dispatch_down.sql` (apaga só o registro manual).

## Validação de interface no Preview (pendente, com sessão admin)

Sem navegador/screenshots neste ambiente — roteiro com dados sintéticos
no Preview, com admin logado

Sem navegador/screenshots neste ambiente — roteiro com dados sintéticos
no Preview, com admin logado: pedido `paid` → detalhe → três seções;
registrar compra parcial (só alguns itens, zeros ignorados); registrar
despacho parcial; reenviar idêntico (retorna existente); divergir
transportadora (conflito claro); corrigir número/rastreio com/sem
justificativa e com token defasado; ver como cliente (código visível,
evidência ausente, pendências explícitas). Testes de navegador do
`tests/browser/` (adicionados no master) cobrem as abas de operações.
