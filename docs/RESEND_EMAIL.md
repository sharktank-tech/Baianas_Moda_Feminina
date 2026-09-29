# E-mail comercial via Resend

O serviço compartilhado `loja/modules/enviar_email.py` atende recuperação de
senha, recuperação de checkout, formulário de contato e boas-vindas da
newsletter. O remetente padrão é `PolloModas <atendimento@pollomodas.com.br>`.
Não há migração de banco nem nova dependência.

## Configuração na Vercel (Production)

| Variável | Valor/configuração |
| --- | --- |
| `EMAIL_SERVER` | `smtp.resend.com` |
| `EMAIL_PORT` | `587` (STARTTLS); `465` ou `2465` usam TLS implícito |
| `EMAIL_USERNAME` | `resend` |
| `EMAIL_SENDER` | `atendimento@pollomodas.com.br` (somente endereço) |
| `EMAIL_SENDER_NAME` | `PolloModas` |
| `EMAIL_PASSWORD` | Segredo: API key do Resend com permissão de envio para o domínio |
| `RESEND_API_KEY` | Alternativa a `EMAIL_PASSWORD`, somente para `smtp.resend.com` |
| `APP_PUBLIC_URL` | `https://www.pollomodas.com.br` |

Valores explícitos do ambiente prevalecem sobre os padrões. Ao migrar,
atualize/remova as configurações SMTP antigas, especialmente `EMAIL_PASSWORD`:
ela tem prioridade sobre `RESEND_API_KEY`. Nunca grave a chave no código, PR,
logs ou documentação. Faça novo deploy após alterar as variáveis.

`EMAIL_DESTINE` é o destinatário do formulário de contato, não o remetente;
configure-o para uma caixa realmente monitorada. Verificar o domínio de envio
não cria uma caixa postal para receber respostas.

O login SMTP é separado do remetente. Se `EMAIL_USERNAME` estiver ausente,
Resend usa `resend`; outros servidores preservam o fallback legado para
`EMAIL_SENDER`. Usuário explicitamente vazio é configuração inválida. TLS
verifica certificados; falha no STARTTLS impede autenticação/envio. Operações
SMTP têm timeout de 10 segundos e logs não incluem mensagens ou credenciais.

## Recuperação de senha

O envio é síncrono por padrão: threads daemon podem ser interrompidas quando
uma função serverless retorna. Não habilite `PASSWORD_RESET_SEND_ASYNC` na
configuração Flask em produção Vercel. Uma evolução para processamento em
segundo plano deve usar fila durável. As respostas públicas genéricas e as
regras de tokens, CSRF e limitação de requisições são preservadas.

## Newsletter e ofertas

O fluxo existente de inscrição envia o cupom de boas-vindas pelo mesmo
remetente. Esta alteração não implementa disparo em massa, agendamento,
segmentação ou descadastro. Antes de campanhas, implementar esses controles,
enviar somente para assinantes que consentiram e respeitar descadastros.
Não usar a base inteira de clientes nem misturar publicidade nos resets.

## Validação

1. Executar `pytest tests/test_resend_email.py tests/test_password_reset.py
   tests/test_checkout_recovery.py -q` (SMTP simulado, rede externa bloqueada).
2. Após configurar e implantar, solicitar recuperação para uma conta local
   própria, verificando From, conteúdo HTML/texto, link HTTPS e expiração.
3. Confirmar recebimento na caixa e evento no painel Resend; aceitação SMTP
   sozinha não comprova entrega na caixa de entrada.
4. Usar o link, trocar a senha e confirmar invalidação de senha/sessão antigas.
5. Testar uma inscrição própria na newsletter, sem disparar campanha.

Referência: https://resend.com/docs/send-with-smtp
