# Protocolo de confirmação do cliente para o canário AliExpress

**Estado:** procedimento preparado; nenhuma confirmação de cliente ou compra realizada. Este arquivo é um formulário vazio e não recebe dados de pedidos reais. Gates AliExpress permanecem desligados.

## Finalidade e definições

- **Pedido real:** iniciado por um cliente e pago no ambiente de produção. CPF, e-mail, atribuição de tráfego e uma linha `Payment` no banco não comprovam isso por si.
- **Pedido orgânico para o canário:** pedido real não criado por teste, equipe ou automação para provocar o canário. Se "orgânico" também exigir tráfego não pago, validar esse requisito separadamente com evidência de campanha/analytics; `acquisition_source` sozinho não o comprova.
- **Checks preliminares:** a verificação `scripts/verify_future_canary_readonly.py` permanece somente leitura e termina com confirmação orgânica pendente (`exit 1`). Não existe desbloqueio automático por este protocolo.

## Responsáveis por função

1. **Técnico:** localiza um candidato novo, executa a verificação somente leitura e confere que o pedido legado com cinco tentativas está excluído. Registra somente o resultado sanitizado e a referência do caso restrito.
2. **Financeiro ou administrador autorizado no Mercado Pago:** consulta o pagamento na conta de **produção** pelo fluxo oficial do painel; compara com o pedido correspondente o estado aprovado vigente, valor, BRL e referência. Confere que não é transação de teste, estornada ou em revisão. Registra resultado, momento e referência opaca à consulta, sem captura de tela/payload no Git.
3. **Atendimento autorizado:** por canal já autorizado pela operação, confirma diretamente com o cliente que ele iniciou a compra correspondente. Não envia pedido, CPF, endereço, link de pagamento ou tokens na abordagem. Se não houver canal autorizado, se não houver resposta ou se houver dúvida, marca como pendente. Esta etapa não autoriza contato automático.
4. **Responsável pela decisão:** revisa os dois resultados independentes, a última leitura técnica e a ausência de tentativas/identidade remota. Registra "cliente real confirmado" somente quando todos concordam; divergência ou falta de registro mantém "pendente". A autorização financeira da compra no fornecedor é decisão posterior e separada.

Uma mesma pessoa pode acumular funções em equipe pequena, desde que registre qual fonte verificou em cada etapa e a decisão permaneça auditável. A consulta ao painel e a confirmação do cliente são fontes distintas do banco da loja.

## Registro restrito do caso

Usar um **sistema interno já aprovado, privado e com controle de acesso**. Não há local de registro escolhido ou criado por esta PR; enquanto não houver um, a etapa continua pendente. Guardar nele apenas: identificador interno opaco do caso (que permita localizar o pedido no sistema restrito), data/hora UTC, responsável por cada conferência, resultado do pagamento de produção, referência opaca à consulta no painel, resultado da confirmação do cliente, referência opaca ao atendimento, resultado técnico e decisão final. Os comprovantes brutos, se exigidos pela operação, ficam apenas nas ferramentas autorizadas de origem.

Nunca copiar para issue, PR, repositório ou log: CPF, nome, e-mail, telefone, endereço, token, payload do provedor, link privado, conversa, captura de tela ou IDs completos de pedido/pagamento. Fora do sistema restrito, divulgar somente contagens e estado geral (`pendente`/`confirmado`), sem identificadores correlacionáveis.

Modelo vazio para preencher **apenas** no sistema restrito:

| Campo | Resultado permitido |
| --- | --- |
| Caso interno | Referência opaca |
| Técnico / horário UTC | Identidade interna / horário |
| Checks técnicos | `aprovados`, `falharam` ou `pendentes` |
| Financeiro / horário UTC / referência | Identidade interna / horário / referência opaca |
| Pagamento em produção | `confirmado`, `divergente` ou `pendente` |
| Atendimento / horário UTC / referência | Identidade interna / horário / referência opaca |
| Cliente confirmou que iniciou a compra | `sim`, `não` ou `sem resposta` |
| Origem de tráfego não pago (se exigida) | `comprovada` ou `pendente` |
| Responsável pela decisão / horário UTC | Identidade interna / horário |
| Decisão | `cliente real confirmado` ou `pendente` |
| Compra no fornecedor | `não autorizada` até autorização financeira separada |

## Sequência quando surgir um pedido novo

1. Repetir a busca somente leitura. Exigir `ready`, `attempt_count=0`, pagamento canônico, snapshot/economia válidos e zero tentativas/remotas. Pedido legado nunca é candidato.
2. Abrir caso no sistema restrito; conferir pagamento no painel de produção e confirmação do cliente pelo canal autorizado.
3. Se qualquer evidência faltar, mantiver dúvida ou divergir, encerrar o caso como `pendente`; não habilitar gates nem enviar pedido ao AliExpress.
4. Com ambas as evidências, repetir a leitura técnica antes de decidir. Registrar decisão no sistema restrito; ela não altera o banco nem converte `exit 1` do script em liberação automática.
5. Somente depois, submeter a autorização financeira específica do canário (pedido, teto, janela e responsável). Sem autorização explícita, nenhuma compra ou habilitação de gates.
