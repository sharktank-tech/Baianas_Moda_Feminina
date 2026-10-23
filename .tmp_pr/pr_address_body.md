## Contexto

Análise das 3 pendências reportadas (relatório entregue antes desta implementação; a #3 — loop do Mercado Pago — **não se reproduz mais** e ficou fora do escopo).

## Pendência #1 — "None" no campo Complementos ✅

**Causa**: `checkout.html:807` imprimia `saved_address.complement` nullable → Jinja vira a string literal `"None"` (reproduzido: `value="None"` no HTML).

**Fix**: `(saved_address.complement or '')` — e o mesmo padrão latente no telefone (`saved_address.phone` nullable → mesmo leak).

## Pendência #2 — endereço não salvo após checkout ✅

**Causa**: `create_mercado_pago_payment` só gravava snapshot nas colunas do pedido (`orders.street/...`); o único `Address()` do sistema era a rota de edição de perfil → "Meu Perfil → Endereços" sempre vazio e checkout reaberto sem prefill (reproduzido: `ADDRESSES APOS CHECKOUT: 0`).

**Fix**: `_upsert_checkout_address(current_user, customer_data)` no mesmo transaction do pedido (antes do `commit` — falha de preferência/rollback não deixa endereço órfão):

- atualiza o endereço **default** (mesmo id, sem duplicar) ou cria o primeiro (`is_default=True`);
- endereços secundários não são tocados;
- `complemento` vazio → `NULL` (nunca string vazia no DB).

**+#2b**: `Order.shipping_address` (property → dict) — o bloco "Entrega" do detalhe do pedido dependia de `pedido.shipping_address is mapping`, mas o atributo era `Undefined` (seção **morta**). Propriedade lê as colunas do snapshot; sem novo schema.

## Testes (por item)

| Item | Teste |
|---|---|
| #1 sem literal `None` (complement/phone NULL, user sem telefone) | `test_checkout_address.py::test_checkout_renders_no_none_in_address_fields` |
| #2 primeiro endereço criado + prefill na reabertura | `::test_checkout_creates_first_address_and_prefills` |
| #2 default atualizado sem duplicar (vazio → NULL) | `::test_checkout_updates_existing_default_address` |
| #2b bloco "Entrega" renderiza | `::test_order_detail_renders_shipping_address` |

**Suíte completa:** `2065 passed, 90 skipped` (+4)

## Validação pós-merge

1. Checkout com complemento vazio → perfil → "Endereços" mostra o endereço;
2. Reabrir checkout → campos preenchidos, sem "None";
3. Detalhe do pedido → seção "Entrega" com logradouro.

## Rollback

Reverter `5ee7df5` (4 arquivos; sem migração de schema).
