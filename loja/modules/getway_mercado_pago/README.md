# Mercado Pago Checkout Pro — PolloModas

Pacote preparado para o projeto Flask/SQLAlchemy atual da PolloModas.

## O que foi implementado

- Checkout Pro por redirecionamento.
- Pedido e itens com snapshot de nome, tamanho, quantidade e preço.
- Recalculo de preço, frete e cupom exclusivamente no servidor.
- `external_reference` apontando para o UUID do pedido.
- Preferência criada com chave de idempotência.
- Webhook com validação da assinatura `x-signature`.
- Consulta do pagamento diretamente na API antes de atualizar o banco.
- Proteção contra notificações duplicadas.
- Conferência de valor e moeda.
- Baixa de estoque e contagem do cupom somente na primeira aprovação.
- Estado `manual_review` quando o pagamento foi recebido, mas há divergência,
  produto removido, estoque insuficiente ou limite de cupom atingido.
- Página de retorno que lê o estado interno do pedido; a URL de retorno não é
  usada como prova de pagamento.

## 1. Estrutura atual

Neste projeto as rotas de pagamento foram consolidadas em:

```text
loja/views/payments_routes.py   (checkout, webhook e retorno)
loja/modules/getway_mercado_pago/payment_models.py
loja/modules/getway_mercado_pago/payment_service.py
loja/templates/payment/result.html
```

O blueprint `payments` é registrado automaticamente em `loja/__init__.py`.
Este arquivo era o guia original da integração; as rotas de `payment_routes.py`
foram movidas para `loja/views/payments_routes.py`.

## 2. Instalar a dependência

O SDK 3.4.0 requer Python 3.10 ou superior.

```bash
pip install -r requirements.txt
```

`mercadopago==3.4.0` já está no `requirements.txt` principal.

## 3. Importar os models

Aplique o conteúdo de:

```text
patches/models_import_patch.py
```

no final de `../models.py`.

Isso permite que Flask-Migrate/Alembic carregue `Order`, `OrderItem` e
`Payment` no metadata.

## 4. Registrar o blueprint

Aplique:

```text
patches/app_factory_patch.py
```

no local em que os blueprints `main` e `admin` são registrados.

Quando `CSRFProtect` estiver ativo, isente somente
`mercado_pago_webhook`. A rota que inicia o pagamento deve permanecer
protegida por CSRF.

## 5. Configurar o ambiente

Use `../../../pollomodas_mercadopago_integration/pollomodas_mercadopago/.env.example` como referência e aplique as chaves de
`../../../patches/config_patch.py` na classe `Config`.

Nunca coloque o Access Token ou a chave secreta no HTML, JavaScript,
repositório Git ou template.

`APP_PUBLIC_URL` deve ser a origem pública da loja, por exemplo:

```text
https://sua-loja.com.br
```

Em produção, o webhook precisa ser acessível por HTTPS.

## 6. Criar as tabelas

Com Flask-Migrate:

```bash
flask db migrate -m "add orders order_items payments"
flask db upgrade
```

Revise a migration gerada antes do `upgrade`, principalmente:

- `orders.user_id` com FK para `users.id`;
- `orders.coupon_id` com `ON DELETE SET NULL`;
- `order_items.order_id` e `payments.order_id` com `ON DELETE CASCADE`;
- índice único de `payments.provider_payment_id`;
- tipos UUID compatíveis com PostgreSQL.

## 7. Atualizar o checkout

Aplique `../../../patches/checkout_template_patch.html` ao template
`../../templates/cart/checkout.html`.

Os radios locais de PIX/cartão/boleto deixam de ser necessários, porque a
escolha acontece no Checkout Pro.

O campo oculto `coupon_code` deve continuar sendo atualizado pelo JavaScript
de cupons que já existe.

## 8. Configurar o webhook no painel

Na aplicação do Mercado Pago:

1. Abra Webhooks.
2. Cadastre a URL de produção:
   `https://SEU_DOMINIO/pagamentos/webhook`
3. Ative o evento de pagamentos.
4. Copie a chave secreta gerada para
   `MERCADO_PAGO_WEBHOOK_SECRET`.
5. Mantenha `MERCADO_PAGO_REQUIRE_WEBHOOK_SIGNATURE=true`.

A URL enviada na preferência também aponta para esse endpoint.

### 8.1 Notificações IPN legadas (importante)

O Mercado Pago também pode disparar IPN no formato antigo:

```text
https://SEU_DOMINIO/pagamentos/webhook?id=<resource>&topic=<payment|merchant_order>
```

Esse formato não contém a assinatura secreta usada pelos Webhooks atuais e,
por isso, é tratado como sinal sem confiança: o `POST /pagamentos/webhook`
classifica cada chamada ANTES de validar assinatura:

- `modern_webhook` (`data.id` no query string): única fonte com autoridade
  para sincronizar pagamentos — exige assinatura válida (401 se inválida,
  503 se `MERCADO_PAGO_WEBHOOK_SECRET` ausente);
- `legacy_ipn` (`id` + `topic` no allowlist, sem `data.id`/`type`): reconhecida
  e ignorada com HTTP 200 para interromper retries — sem consultar provedor,
  sem sincronizar e sem alterar banco/estoque;
- `malformed_or_unknown`: ignorada com HTTP 200, sem efeitos colaterais.

Classifications em `getway_mercado_pago/webhook_classification.py`. Regra
inegociável: notificação com `data.id` no query NUNCA cai no caminho legado.
Eventos no log (`mp_observability`): `modern_webhook_validated`,
`modern_webhook_rejected`, `legacy_ipn_ignored`, `webhook_malformed_ignored` —
sempre sanitizados, sem IDs em texto puro.

Quando o Webhook autenticado for a fonte primária, desative a IPN no painel do
Mercado Pago (ação operacional, fora do código).

## 9. Testar

```bash
pytest -q tests/test_payment_service.py
```

Fluxo manual:

1. Use credenciais de teste.
2. Crie usuário e produto com estoque.
3. Adicione produto ao carrinho.
4. Finalize o checkout.
5. Confirme que o navegador foi redirecionado ao Mercado Pago.
6. Faça um pagamento de teste.
7. Confirme no banco:
   - `orders.status = paid`;
   - uma linha em `payments`;
   - estoque reduzido uma única vez;
   - cupom incrementado uma única vez.
8. Reenvie a mesma notificação no simulador e confirme que não há nova baixa.

## Pontos que precisam coincidir com a loja

O frete não usa mais tarifa fixa: `calculate_shipping()` foi removido. A
cobrança sai de cotação por grupo de remessa/fornecedor
(`loja/services/shipping_quote.py`), e a vitrine exibe apenas a estimativa
"a partir de R$ 10,00 (estimativa)" antes do CEP. Sem cotação válida não há
preferência nem pedido (mensagem + retry).

O carrinho é esvaziado depois que a preferência é criada e o pedido é salvo.
Mesmo que o cliente abandone o Mercado Pago, o pedido pendente continua no
banco. Uma evolução recomendada é criar a página "Meus pedidos" com o botão
"Continuar pagamento".
