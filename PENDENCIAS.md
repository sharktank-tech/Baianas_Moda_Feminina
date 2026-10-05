# Pendências — plano Weblyzer

Criado em **2026-10-04** (Fase 5 da rodada aprovada: branches → Tailwind →
AdSense → CSP nonce → docs). Próxima revisão: **2026-11-04**.

## Rodada concluída (referência)

| PR/issue | Conteúdo | Estado |
|---|---|---|
| #108 | Review estrutural `review/loja-estrutural` | merged |
| #106 / #107 | Branchs `feature/canary-private-evidence-register` e `fix/test-dotenv-canary-evidence` | fechados — 100% absorvidos pelo histórico |
| #109 | Fifth diagnostic canary 8.0.11z | fechado sem merge (decisão do mantenedor; 4th já revertido no #21) |
| #110 | Tailwind pré-compilado (saiu o Play CDN) | merged — produção validada |
| #111 | AdSense pós-`window load` | merged — produção validada |
| #112 | CSP com nonce: sem `unsafe-inline`/`unsafe-eval`, `script-src-attr 'none'`, 25 `on*` → data-attributes | merged — produção validada (14/14 checks) |
| #113 | Issue: cache de cupons com TTL | aberta |
| #114 | Docs `PENDENCIAS.md` + issue #113 | merged |
| #115 | P4b `style-src` nonce + `style-src-attr 'none'` (zero inline na CSP) | merged — **Security A+ 100/100** |
| #116 | P6 cache `/static` (`max-age=60, s-maxage=3600`) | merged |
| #117 | P6b middleware WSGI remove `Vary: Cookie` em `/static` (habilita borda: `X-Vercel-Cache: HIT`) | merged — produção validada |
| #118 | P7 `loja/static` → `public/static` (Vercel serve do filesystem, sem Lambda no caminho crítico) | merged — produção validada (CSP ausente no asset = filesystem ativo) |

Produção em `2a1e08f`: suíte `2061 passed, 90 skipped`; CI master 3/3;
Security **A+ 100/100** nos scans de 04/10 22:28 e 05/10 09:19;
**PSI 05/10 09:42: mobile 99 / desktop 84** (CWV verdes).

## Pendências

| # | Item | Por quê | Próximo passo | Revisão |
|---|---|---|---|---|
| 1 | ~~**Métrica independente de paint (PSI)**~~ | Weblyzer variou FCP 3.1 / 8.4 / 9.3 / 20.5s com código idêntico; o FCP/LCP 20.5s contradizia o DCL 3.1s → artefato do scanner | **Concluído em 2026-10-05** (Lighthouse 13.5.0, 09:42 BRT): **mobile 99** (FCP 0,9s · LCP 1,0s · TBT 0ms · CLS 0,001 · SI 3,3s) e **desktop 84** (FCP 0,3s · LCP 1,6s · TBT 40ms · CLS 0,006 · SI 5,6s) — CWV verdes nos dois; A11y 90, BP 92, SEO 100 | ✅ |
| 2 | ~~**`style-src` sem `unsafe-inline`**~~ | ~~Última permissão inline da CSP~~ | **Concluído em 2026-10-04** (PR P4b): nonce nos 12 blocos `<style>`, 9 atributos `style=""` → classes Tailwind, `style-src-attr 'none'` | ✅ |
| 3 | **Cache de cupons com TTL** — [#113](https://github.com/sharktank-tech/PolloModas/issues/113) | `_restore_checkout_coupon` reconsulta o Supabase a cada render do checkout (`routes.py:1031`) | Implementar via `loja/cache.py` (TTL 60s + invalidação nos mutations de admin) conforme a issue | 2026-11-04 |
| 4 | **Monitorar cold start pós-deploy** | Em 2026-10-04, logo após o merge do #112, produção retornou `FUNCTION_INVOCATION_FAILED` (500) em todas as rotas por ~3 min; preview do mesmo commit esteve sempre 200 e a produção se recuperou sozinha; **logs da Vercel verificados pelo mantenedor: sem ocorrência registrada** (em merges #117/#118 o 500 transitório **não** se repetiu) | Se não recorrer até a revisão, fechar | 2026-11-04 |
| 5 | **Hero de 774KB no Supabase** | Imagem do hero (preload) pesa **774KB** (medido 04/10) — candidato a LCP; Lighthouse 05/10: auditoria "Melhorar a entrega de imagens" = **2.998 KiB** (mobile) / **2.587 KiB** (desktop) de economia | Redimensionar/comprimir no upload (ex.: ≤200KB WebP) ou servir via transform do Supabase | 2026-11-04 |
| 6 | **Espelhar imagens `ae01.alicdn.com` → Supabase** | Homepage tem **32 imgs** servidas direto do CDN da AliExpress (third-party de imagem; dependência deles p/ render) — contam na economia de entrega de imagens acima | Job de import copia os arquivos para o Storage e grava URL primeira-parte; rollback = manter origem em fallback | 2026-11-04 |
| 7 | ~~Cache de `/static`~~ | ~~Cada asset invocava a Lambda (`no-cache` + `X-Vercel-Cache: MISS`); FCP/DCL 9.3s no scan de 21:41~~ | **Concluído em 2026-10-05**: #116 (política) + #117 (`Vary` sem Cookie → `HIT` na borda) + #118 (`public/static` → filesystem, sem Lambda). Produção: borda `HIT`, `Age` crescente, assets sem CSP (= filesystem) | ✅ |
| 8 | **Oportunidades Lighthouse restantes** (baixa) | Desktop 84: SI 5,6s; "JS não usado" 148–149 KiB; "cache eficiente" 130 KiB; imgs sem `width`/`height` explícitos; "render-blocking" 300–480 ms | Triagem junto de #5/#6 (entrega de imagens já é a maior economia); itens de DOM/JS só se PSI regress | 2026-11-04 |

## Decisões já tomadas (não reabrir sem motivo)

- `style-src` mantém `'unsafe-inline'` por ora (linha 2 acima é o caminho).
- Nonce usa `secrets.token_hex(32)` por requisição, com fallback preguiçoso
  no context processor para requests que encurtam `preprocess_request`.
- AdSense: loader com nonce + origens na allowlist (sem `strict-dynamic`).
- Fontes e Tailwind são 100% first-party — não reintroduzir allowlists
  `fonts.googleapis.com` / `cdn.tailwindcss.com` (testes bloqueiam).
- `/static` vive em `public/static` (fonte única; Flask serve o mesmo
  diretório como fallback local) — não mover de volta para `loja/static`.
- FCP do Weblyzer não é métrica de aceite (variância + contradição com DCL);
  aceitar/priorizar PSI (Lighthouse/CrUX) até a revisão.
