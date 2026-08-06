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

## 1. Copiar os arquivos

Copie:

```text
loja/modules/payment_models.py
loja/modules/payment_service.py
loja/modules/payment_routes.py
loja/templates/payment/result.html
```

para os mesmos caminhos do projeto.

## 2. Instalar a dependência

O SDK 3.4.0 requer Python 3.10 ou superior.

```bash
pip install -r requirements-mercadopago.txt
```

Também adicione `mercadopago==3.4.0` ao `../../../requirements.txt` principal.

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

O checkout encontrado usa valores de frete diferentes em trechos distintos
(R$ 20,00 no carrinho e R$ 18,90 no cálculo por CEP). Este pacote centraliza
o valor em `PAYMENT_FLAT_SHIPPING=18.90`. Atualize a tela do carrinho para
usar a mesma regra ou substitua `calculate_shipping()` por uma integração real.

O carrinho é esvaziado depois que a preferência é criada e o pedido é salvo.
Mesmo que o cliente abandone o Mercado Pago, o pedido pendente continua no
banco. Uma evolução recomendada é criar a página "Meus pedidos" com o botão
"Continuar pagamento".
