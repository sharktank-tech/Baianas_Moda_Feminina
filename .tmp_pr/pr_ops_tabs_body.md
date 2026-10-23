## Abordagem

Pedidos, Revisão manual, Prontidão e Recuperação vivem no dashboard como
abas (`#orders`, `#manual-review`, `#sales-readiness`, `#checkout-recovery`),
no grupo Operação (desktop) e na navegação mobile, sem duplicar entradas.
Conteúdo extraído para `admin/partials/_*.html` (páginas antigas removidas —
o git detectou como renomeações); parcial carrega sob demanda via
`GET /admin/painel/*` (somente leitura, `admin_required`, GET-only).
URLs antigas de listagem redirecionam para a aba (estado/página preservados;
busca livre nunca viaja no fragmento). POSTs mantêm contrato/CSRF e voltam
para a aba (`/admin#tab`). JS com `pushState`, `popstate`/`hashchange`,
dedupe de fetch, erro com retry (refaz a última URL, preservando contexto)
e `AbortController` anti-corrida; `data-confirm` delegado já existente
continua valendo no conteúdo injetado.
**Refinamento visual**: parciais no vocabulário `adm-*` (cabeçalho com
título/descrição, cards de indicadores, filtros em card próprio, `adm-table`
com hover, numerais tabulares à direita, badges com texto, motivos em lista,
nomes com ellipsis+`title`, metodologia em `<details>` expansível, labels
com `for`, foco visível, empilhamento mobile sem ocultar ações). Somente
apresentação: rotas, permissões, consultas, ações e regras inalteradas.

## Matriz URLs antigas → novos destinos

| Antiga | Nova |
|---|---|
| `GET /admin/pedidos` (`?status=&page=` preservados; `q` e demais descartados) | `/admin#orders` (+query no fragmento) |
| `GET /admin/pedidos/revisao` | `/admin#manual-review` |
| `GET /admin/prontidao-vendas` | `/admin#sales-readiness` |
| `GET /admin/recuperacao-checkout` | `/admin#checkout-recovery` |
| `POST .../revisao/<id>/aprovar\|rejeitar` | mesma rota → `/admin#manual-review` |
| `POST .../recuperacao-checkout[/<id>/enviar\|/<id>/ignorar]`, `POST .../reservas-expiradas/...`, `POST .../prontidao-vendas/cotar/<id>` | mesmas rotas → `/admin#checkout-recovery` / `/admin#sales-readiness` |
| `/admin/pedidos/<uuid>` (detalhe) | permanece própria; retorno aponta `/admin#orders` |

## Testes e CI

- Novo `tests/test_admin_ops_tabs.py` (23 testes): abas registradas
  desktop/mobile, parciais (conteúdo/métodos/permissão/CSRF/sem `<script>`/IDs
  únicos), redirects com parâmetros, volta-à-aba dos POSTs, ausência de
  efeitos colaterais ao abrir abas (gates ligados), fiação do fragmento
  (pushState/popstate/hashchange/retry, AbortController anti-corrida).
- 7 arquivos atualizados para o novo contrato (redirects + parciais) +
  limpeza de singletons/pedidos onde redirects agora passam pelo dashboard.
- Suíte completa: **2358 passed, 109 skipped**; espelhos PG correlatos
  verdes; `git diff --check` limpo.
- CI no HEAD final: 3/3 jobs success. Sem merge/deploy em produção.

## Limitações (declaradas)

- **Sem testes de navegador neste ambiente** (sem Chromium funcional, sem
  PyPI/npm): testes estáticos **não equivalem** a testes de navegador.
  Validação real pendente no Preview com sessão admin (roteiro abaixo).
- **Sem evidência visual anexada** (sem ferramenta de screenshot): mesma
  causa; passos manuais abaixo geram a evidência em staging.
- Retry de parcial refaz a última URL (filtros/busca/página preservados);
  resposta abortada por clique rápido é silenciosa (sem erro espúrio);
  detalhe de pedido segue página própria.

## Roteiro de validação no Preview (sessão admin)

Preview: https://pollo-modas-9qh2tgypf-danilos-projects-dc14d397.vercel.app
(com dados sintéticos/staging — não usar dados reais de clientes)

1. **Abertura direta**: `/admin#orders`, `#manual-review`,
   `#sales-readiness`, `#checkout-recovery` — cada URL deve abrir o
   dashboard já na aba certa, com conteúdo carregado (não só o esqueleto).
2. **Recarregar** em cada aba: seleção e conteúdo preservados.
3. **Voltar/avançar**: navegar entre abas e usar os botões do navegador;
   a aba exibida deve acompanhar o fragmento em ambos os sentidos.
4. **Desktop e mobile** (devtools responsivo): botões apontam para os
   fragmentos; layout sem quebra.
5. **Pedidos**: filtrar por estado, buscar referência, paginar; abrir
   "Ver pedido" e voltar (link ← Pedidos) — deve retornar à aba.
6. **URLs antigas**: `/admin/pedidos?status=paid&page=2`,
   `/admin/pedidos/revisao`, `/admin/prontidao-vendas`,
   `/admin/recuperacao-checkout` — devem cair nas abas equivalentes.
7. **Falha**: bloquear a URL `/admin/painel/*` (devtools offline) numa aba
   filtrada → "Falha ao carregar" + **Tentar novamente preserva o filtro**.
8. **Cliques rápidos** entre abas: nenhum conteúdo trocado/duplicado.
