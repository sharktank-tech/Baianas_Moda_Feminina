# Recuperação seletiva de checkout por e-mail

## Gates

- `CHECKOUT_RECOVERY_ENABLED=1`: permite somente ações individuais feitas por admin.
- `CHECKOUT_RECOVERY_BATCH_ENABLED=1`: permite adicionalmente o envio em lote. O padrão é desligado.

Os dois gates são comparados de forma exata com `1`. Nenhum deles inicia tráfego pago.

## Preflight SMTP

Antes de exibir uma ação individual utilizável, a aplicação verifica apenas a presença e a forma de:

- `EMAIL_SERVER`
- `EMAIL_PORT` (1 a 65535)
- `EMAIL_SENDER`
- `EMAIL_PASSWORD`

Valores nunca são devolvidos pela interface ou escritos em log. Falhas do provedor geram somente códigos sanitizados.

## Envio individual

A ação exige admin autenticado e CSRF. Sob lock do pedido, o serviço revalida:

- status `pending_payment`;
- reserva de estoque ativa;
- idade mínima configurada;
- reserva ainda dentro da janela de recuperação;
- e-mail presente;
- lembrete ainda não enviado.

Somente o pedido escolhido é processado. Sucesso grava `recovery_email_sent_at`; falha mantém o campo vazio para nova tentativa controlada.

## Ordem operacional

1. manter ambos os gates desligados;
2. configurar SMTP somente em Production e fazer redeploy;
3. confirmar no painel apenas o estado agregado `Entrega SMTP: PRONTA`;
4. ativar `CHECKOUT_RECOVERY_ENABLED=1` e fazer redeploy;
5. enviar um candidato individual por vez;
6. manter `CHECKOUT_RECOVERY_BATCH_ENABLED` desligado até decisão separada.
