# Frete manual com evidência — decisão e operação (P7)

## PASSO 1 — Diagnóstico do bloqueio da API (2026-09-25)

- **Provider/exato:** AliExpress Open Platform, método
  `aliexpress.logistics.buyer.freight.calculate`, gateway
  `https://api-sg.aliexpress.com/sync`, `sign_method=sha256`, SKU `skuId`.
- **Resposta observada (histórico live, `FREIGHT_PROBE.md`):** request aceito
  até a lógica interna; `queryShippingOptionForItem(forBuyer...)` retorna
  `null`. Tentativas anteriores: `buyer.freight.get` → `InvalidParameter` /
  `MissingParameter`; TOP legado → `code 29` / `isv.appkey-not-exists`.
- **Conclusão honesta:** o `null` prova apenas indisponibilidade **nas
  condições testadas** — NÃO se afirma limitação estrutural do provider nem
  se exclui causa de credencial/permissão sem reteste. A rota manual foi
  mantida por **decisão operacional** (evidência auditável por venda, sem
  depender de reteste/homologação externa), não por causa raiz comprovada.
- O R$ 10 (`SHIPPING_TEMPORARY_FLAT_RATE`) continua existindo apenas como
  tarifa comercial com a flag `SHIPPING_GROUPED_QUOTE_ENABLED=False`;
  **não** é prova de custo — e, pelo ITEM 5.5, a venda exige evidência
  vigente mesmo nesse modo.

## PASSO 2 — Fluxo de evidência (implementado)

- **Onde:** tabela `pollomodas.supplier_reference_freight_quotes`
  (migrations `0025` + `0026` com escopo), via `store_reference_freight_quote` /
  `find_valid_reference_quotes` (`loja/services/supplier_freight_quotes.py`).
- **Registro (ITEM 1):** rota admin `POST .../rascunho/<id>/frete`
  (`rascunho_frete`) com formulário completo (`rascunho.html`): custo, moeda,
  CEP exato (+ fim de faixa opcional), escopo de variante (específica ou
  todas-explícito), opção, prazos, teto de quantidade, **validade e
  referência obrigatórias**. Entrada incompleta é REJEITADA (nada é gravado);
  sem defaults silenciosos.
- **Renovação de publicado (ITEM 2):** `GET/POST .../publicado/<id>/frete`
  (`renovar_frete.html`) — renova sem despublicar, com as mesmas validações.
  Publicado não pode ficar STALE (CHECK `ck_product_aliexpress_publication_guard`
  exige `priced`): mudança material recalcula com a MESMA margem-alvo na
  transação; falha reverte tudo.
- **Transação única (ITEM 4):** custo + cotação com um commit; qualquer falha
  reverte ambas (`store(..., commit=False)`).
- **Escopo real (ITEM 3):** a evidência vale só para o declarado — CEP exato
  ou faixa explícita (prefixo nunca casa), variante vinculada ou cobertura
  multi explícita, quantidade em `1..qty_max`. Linhas legadas sem escopo
  valem para referência/painel, nunca para o checkout.
- **Bloqueio (2.2/5.5):** com ou sem modo agrupado, o checkout exige cobertura
  vigente (`require_aliexpress_evidence`); o modo define só o valor (cotação
  real vs. flat R$ 10). Sem cobertura a venda **não** é processada. Cotação
  vencida exige recotação (código `expired`).
- **QUEM/QUANDO:** o admin do catálogo AliExpress, na tela de rascunho (novo)
  ou de renovação (publicado), com a cotação do fornecedor em mãos: (a) todo
  produto novo; (b) expiração; (c) mudança de tabela/peso/dimensões/destino.
  Sem cotação vigente, o produto não vende em nenhum modo.

## PASSO 3 — Custo do cliente e margem

- **3.1:** R$ 10 fixo em `config.py` (`SHIPPING_TEMPORARY_FLAT_RATE`,
  default `"10.00"`) e `shipping_quote.py`
  (`SHIPPING_TEMPORARY_FLAT_RATE_DEFAULT`).
- **3.2:** no modo agrupado o valor cobrado é `quote_for_items` (custo real
  por vínculo); o flat só vale com a flag desligada (regra comercial
  assumida, documentada como não-prova).
- **3.3 — SIMULAÇÃO DE PRECIFICAÇÃO (não é margem real validada):**
  exemplo com custo 60,00 + margem-alvo 30%: landed assumido (10,00) = 70,00
  → preço 100,00; landed com frete evidenciado (18,50) = 78,50 → preço
  112,15. Diferença de **R$ 8,50 de custo** e **≥ R$ 12,14 de receita** por
  pedido para manter a mesma margem. Não inclui o restante da economia do
  pedido nem a receita adicional do frete cobrado no checkout — serve para
  dimensionar o risco oculto do flat, não como margem auditada. Teste:
  `test_margin_real_vs_assumida`.
- **3.4:** sem cotação válida, `quote_checkout_shipping` levanta `ValueError`
  — nenhuma venda finaliza com frete assumido ou zerado no modo agrupado
  (teste `test_grouped_without_quote_blocks_sale`).

## PASSO 4 — Validação ponta a ponta (nível checkout + HTTP, sem rede)

- **Produto de teste:** vínculo AliExpress novo (não o pedido legado de 5
  tentativas), custo 60,00, cotação manual 18,50, destino `77000000`.
- **Resultado:** `quote_for_items` retorna 18,50 (não 10,00); sem cotação →
  bloqueio; cotação vencida → bloqueio; matriz de escopo (CEP fora/faixa,
  outra variante, qtd acima do teto) bloqueia e cobertura explícita passa
  (`tests/test_freight_manual_quote_evidence.py`).
- **Fluxo real via HTTP:** POST admin sem referência/validade → rejeitado sem
  gravar; renovação de publicado recalcula mesma margem sem despublicar;
  falha forçada na cotação reverte o custo; checkout flat sem evidência →
  página "frete indisponível" sem pedido; com evidência → 302 e frete 10,00
  (`test_aliexpress_admin_catalog.py` §11, `test_shipping_quote.py` HTTP flat).
- **Não executado aqui:** venda com pagamento aprovado/MP + compra no
  fornecedor — exige ambiente com credenciais MP/fornecedor e autorização
  humana explícita (`FIRST_REAL_SALE_RUNBOOK.md`). Este relatório cobre até
  o bloqueio/cobrança no checkout; a compra real segue o runbook existente.
- **Limite conhecido:** cenário único de referência — grupos de checkout não
  têm opção de envio; manter `SALES_READINESS_SHIPPING_OPTION` ausente até
  o checkout oferecer escolha, senão o checkout bloqueia (fail-closed).
  O painel de prontidão usa semântica própria de cenário de referência
  (prefixo); a cobertura que autoriza a venda é só a do checkout.
