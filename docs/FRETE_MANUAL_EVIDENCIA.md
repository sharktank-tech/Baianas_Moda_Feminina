# Frete manual com evidência — decisão e operação (P7)

## PASSO 1 — Diagnóstico do bloqueio da API (2026-09-25)

- **Provider/exato:** AliExpress Open Platform, método
  `aliexpress.logistics.buyer.freight.calculate`, gateway
  `https://api-sg.aliexpress.com/sync`, `sign_method=sha256`, SKU `skuId`.
- **Erro real observado (histórico live, `FREIGHT_PROBE.md`):** request aceito
  até a lógica interna; `queryShippingOptionForItem(forBuyer...)` retorna
  `null`. Tentativas anteriores: `buyer.freight.get` → `InvalidParameter` /
  `MissingParameter`; TOP legado → `code 29` / `isv.appkey-not-exists`.
- **Classificação (causa raiz):** provider não oferece esse tipo de cotação
  para esta App Key/SKU — limitação estrutural, não credencial, homologação
  ou rate limit. Nenhum novo experimento live deve ser feito.
- **Decisão:** rota manual (PASSO 2). O R$ 10 (`SHIPPING_TEMPORARY_FLAT_RATE`)
  continua existindo apenas como tarifa comercial com a flag
  `SHIPPING_GROUPED_QUOTE_ENABLED=False`; **não** é prova de custo.

## PASSO 2 — Fluxo de evidência (implementado)

- **Onde:** tabela `pollomodas.supplier_reference_freight_quotes`
  (migration `0025`), via `store_reference_freight_quote` /
  `find_valid_reference_quotes` (`loja/services/supplier_freight_quotes.py`).
- **Registro:** rota admin `POST /admin/integrations/aliexpress/rascunho/<id>/frete`
  (`rascunho_frete`) salva o `SupplierProduct.shipping_cost` como antes **e**
  persiste a cotação vigente por cenário (destino, opção, quantidade 1,
  moeda BRL, momento, validade). Campos opcionais do formulário:
  `freight_dest_cep`, `freight_option`, `freight_min_days`,
  `freight_max_days`, `freight_ttl_days` (padrão 30, 1–365),
  `freight_evidence_ref` (número/link/print da cotação — só o hash persiste).
  Sem CEP de referência, nada é persistido e o fluxo legado é preservado.
- **Bloqueio (2.2):** com `SHIPPING_GROUPED_QUOTE_ENABLED=True`, o checkout
  usa `quote_for_items` → `AliExpressSupplierProvider` alimentado pelo
  snapshot do banco (`default_providers` ← `load_fresh_quotes_snapshot`).
  Sem cotação vigente para o vínculo/destino/quantidade/momento, o checkout
  levanta `QuoteUnavailable` → `ValueError("Frete indisponível…")` e a venda
  **não** é processada. Cotação vencida exige recotação (código `expired`).
- **QUEM (2.3):** o admin do catálogo AliExpress, na tela de rascunho do
  produto, informa o frete cotado junto ao fornecedor (tela do seller,
  e-mail/proposta ou painel logístico) e a referência da evidência.
- **QUANDO (2.3):** (a) toda vez que um produto novo entra no catálogo;
  (b) quando a cotação vigente expira (checkout/painel sinalizam);
  (c) quando o fornecedor altera tabela/peso/dimensões ou o destino de
  referência muda. Produtos sem cotação vigente não vendem no modo agrupado.

## PASSO 3 — Custo do cliente e margem

- **3.1:** R$ 10 fixo em `config.py` (`SHIPPING_TEMPORARY_FLAT_RATE`,
  default `"10.00"`) e `shipping_quote.py`
  (`SHIPPING_TEMPORARY_FLAT_RATE_DEFAULT`).
- **3.2:** no modo agrupado o valor cobrado é `quote_for_items` (custo real
  por vínculo); o flat só vale com a flag desligada (regra comercial
  assumida, documentada como não-prova).
- **3.3 (risco oculto, exemplo com custo 60,00 + margem-alvo 30%):**
  landed assumido (10,00) = 70,00 → preço 100,00; landed real (18,50) =
  78,50 → preço 112,15. Diferença de **R$ 8,50 de custo** e **≥ R$ 12,14 de
  receita** por pedido para manter a mesma margem. Teste:
  `test_margin_real_vs_assumida`.
- **3.4:** sem cotação válida, `quote_checkout_shipping` levanta `ValueError`
  — nenhuma venda finaliza com frete assumido ou zerado no modo agrupado
  (teste `test_grouped_without_quote_blocks_sale`).

## PASSO 4 — Validação ponta a ponta (nível checkout, sem rede)

- **Produto de teste:** vínculo AliExpress novo (não o pedido legado de 5
  tentativas), custo 60,00, cotação manual 18,50, destino `77000000`.
- **Resultado:** `quote_for_items` retorna 18,50 (não 10,00); sem cotação →
  bloqueio; cotação vencida → bloqueio; helper admin persiste com validade
  (`tests/test_freight_manual_quote_evidence.py`, 8 testes).
- **Não executado aqui:** venda com pagamento aprovado/MP + compra no
  fornecedor — exige ambiente com credenciais MP/fornecedor e autorização
  humana explícita (`FIRST_REAL_SALE_RUNBOOK.md`). Este relatório cobre até
  o bloqueio/cobrança no checkout; a compra real segue o runbook existente.
- **Limite conhecido:** cenário único de referência — grupos de checkout não
  têm opção de envio; manter `SALES_READINESS_SHIPPING_OPTION` ausente até
  o checkout oferecer escolha, senão o modo agrupado bloqueia (fail-closed).
