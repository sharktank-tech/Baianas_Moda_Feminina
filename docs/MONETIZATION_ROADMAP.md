# PolloModas — Roadmap de monetização

Checkpoint criado em 2026-09-12 para consulta futura.

## Estado atual

A loja já provou capacidade de monetização: existe pagamento Mercado Pago aprovado em modo real, catálogo AliExpress publicado e deploy de produção ativo. O objetivo das próximas fases não é apenas "conseguir cobrar", mas tornar o ciclo de venda repetível, lucrativo e operacionalmente seguro.

O fluxo-alvo é:

`produto publicado -> checkout -> pagamento aprovado -> fulfillment fornecedor -> rastreio -> entrega -> margem líquida positiva`

## FASE 8.0.12C — CPF nativo no checkout

**Status:** concluída e implantada.

Objetivo: capturar, validar e persistir o CPF do destinatário antes da criação da preferência do Mercado Pago, para que novos pedidos AliExpress já nasçam com identidade fiscal suficiente para materializar o submission snapshot.

Entregue:

- CPF informado no checkout, sem valor hardcoded;
- normalização para 11 dígitos e validação dos dígitos verificadores;
- nenhum CPF em logs, exceptions ou mensagens sanitizadas;
- `orders.customer_cpf` persistido para pedidos novos;
- pedidos legados continuam válidos com `customer_cpf = NULL`;
- snapshot novo recebe `cpf` diretamente da fonte persistida;
- falha de CPF acontece antes da chamada ao Mercado Pago;
- migration 0014 aplicada e validada no Supabase de produção;
- nenhuma chamada real AliExpress foi habilitada por esta fase.

## FASE 8.1.0 — Fulfillment AliExpress de produção

**Status:** em andamento.

Objetivo: substituir o caminho operacional HARD-OFF/canário por um pipeline de produção explicitamente controlado para pedidos pagos.

### Checkpoint 8.1.0-A — política de dupla trava

Em implementação/validação:

- `ALIEXPRESS_PRODUCTION_FULFILLMENT_ENABLED=1` autoriza a camada de produção;
- `ALIEXPRESS_PLACEORDER_ENABLED=1` continua sendo exigido pelo boundary HTTP;
- provider real só pode ser selecionado quando os dois gates são exatamente `1`;
- qualquer estado parcial/ausente seleciona `HardOffOrderSubmitter`;
- este checkpoint NÃO conecta automaticamente o provider ao webhook/pedido pago;
- este checkpoint NÃO faz HTTP real.

Critérios de saída da fase completa:

- somente pedidos `paid` e íntegros entram no fulfillment;
- provider real habilitado por configuração de produção, sem depender de canários one-shot;
- idempotência por tentativa e circuit breaker preservados;
- resultado `unknown` nunca gera reenvio automático;
- reconciliação obrigatória para ambiguidades;
- compra confirmada gera identidade remota persistida;
- falhas confirmadas ficam operáveis sem duplicar compra;
- observabilidade sem PII/segredos.

## FASE 8.2.0 — Margem líquida e proteção contra venda com prejuízo

**Status:** futura.

Objetivo: sair da margem baseada apenas em landed cost e calcular unit economics reais.

Incluir, quando conhecidos e configurados:

- custo do fornecedor;
- frete do fornecedor;
- taxa do gateway;
- custo de parcelamento quando aplicável;
- descontos/cupons;
- impostos configurados pelo negócio;
- custo de aquisição por pedido/canal para análise gerencial;
- reserva operacional para reembolso/chargeback, se adotada.

Critérios de saída:

- produto não é publicado abaixo da margem líquida mínima definida;
- nenhum custo desconhecido é silenciosamente tratado como zero;
- dashboard mostra receita bruta, custo, margem de contribuição e margem líquida operacional definida pelo projeto.

## FASE 8.3.0 — Pós-venda, rastreio e exceções

**Status:** futura.

Objetivo: fechar o ciclo após a compra no fornecedor.

Critérios de saída:

- sincronização de status do fornecedor;
- tracking code/carrier persistidos quando disponíveis;
- cliente consegue acompanhar pedido;
- fluxo explícito para fornecedor rejeitar/cancelar pedido;
- fluxo de cancelamento/reembolso integrado ao estado interno;
- pedidos presos possuem fila/admin de exceção;
- alertas para fulfillment sem atualização por período definido.

## FASE 9.0.0 — Aquisição e crescimento

**Status:** futura, somente após o ciclo operacional estar estável.

Objetivo: transformar infraestrutura de venda em faturamento recorrente.

Frentes planejadas:

- Meta Ads / Instagram;
- TikTok orgânico e pago;
- Google Shopping quando catálogo/feed estiver pronto;
- SEO de páginas de produto/categoria;
- recuperação de checkout/pedido pendente;
- remarketing;
- métricas por canal: sessões, conversão, CAC, ticket médio, margem por pedido e recompra.

## Regra de progressão

Não avançar tráfego pago relevante enquanto uma venda aprovada ainda exigir correção manual para chegar ao fornecedor. Crescimento só deve ser acelerado depois de checkout, fulfillment, margem e pós-venda estarem observáveis e previsíveis.

## Regras permanentes de segurança AliExpress

- nenhuma autorização de merge implica autorização de chamada ao provider;
- nenhuma autorização de provider implica retry;
- resultado remoto ambíguo deve falhar fechado;
- nenhuma fase deve expor token, CPF, payload integral, attempt key ou remote ID desnecessário em logs;
- qualquer mudança que habilite compra real precisa de gates e testes próprios antes de produção.
