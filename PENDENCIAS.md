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

Produção em `4bde1ec`: suíte `2052 passed, 90 skipped`; CI master 3/3.

## Pendências

| # | Item | Por quê | Próximo passo | Revisão |
|---|---|---|---|---|
| 1 | **Medição Weblyzer pós-#112** | ~~Security ≥90~~ → **A+ 100/100 em 04/10 21:41** ✅. Resta Performance: alvos PageLoad ≤3.5s / requests ≤25 variam com o scroll do scanner (as mesmas 32 imgs lazy variam 17↔32 requests entre scans) | Re-scanar com método fixo (sem rolagem) e comparar com o PR de cache de `/static` | 2026-11-04 |
| 2 | ~~**`style-src` sem `unsafe-inline`**~~ | ~~Última permissão inline da CSP~~ | **Concluído em 2026-10-04** (PR P4b): nonce nos 12 blocos `<style>`, 9 atributos `style=""` → classes Tailwind, `style-src-attr 'none'` | ✅ |
| 3 | **Cache de cupons com TTL** — [#113](https://github.com/sharktank-tech/PolloModas/issues/113) | `_restore_checkout_coupon` reconsulta o Supabase a cada render do checkout (`routes.py:1031`) | Implementar via `loja/cache.py` (TTL 60s + invalidação nos mutations de admin) conforme a issue | 2026-11-04 |
| 4 | **Monitorar cold start pós-deploy** | Em 2026-10-04, logo após o merge do #112, produção retornou `FUNCTION_INVOCATION_FAILED` (500) em todas as rotas por ~3 min; preview do mesmo commit esteve sempre 200 e a produção se recuperou sozinha; **logs da Vercel verificados pelo mantenedor: sem ocorrência registrada** | Se não recorrer até a revisão, fechar | 2026-11-04 |
| 5 | **Hero de 774KB no Supabase** | Imagem do hero (preload) pesa **774KB** (medido 04/10) — candidato a LCP | Redimensionar/comprimir no upload (ex.: ≤200KB WebP) ou servir via transform do Supabase | 2026-11-04 |
| 6 | **Espelhar imagens `ae01.alicdn.com` → Supabase** | Homepage tem **32 imgs** servidas direto do CDN da AliExpress (third-party de imagem; dependência deles p/ render) | Job de import copia os arquivos para o Storage e grava URL primeira-parte; rollback = manter origem em fallback | 2026-11-04 |
| 7 | ~~Cache de `/static`~~ | ~~Cada asset invocava a Lambda (`no-cache` + `X-Vercel-Cache: MISS`); FCP/DCL 9.3s no scan de 21:41~~ | **PR P6**: `public, max-age=60, s-maxage=3600` só em `/static/*` (staleness ≤60s no navegador) | ✅ |

## Decisões já tomadas (não reabrir sem motivo)

- `style-src` mantém `'unsafe-inline'` por ora (linha 2 acima é o caminho).
- Nonce usa `secrets.token_hex(32)` por requisição, com fallback preguiçoso
  no context processor para requests que encurtam o `preprocess_request`.
- AdSense: loader com nonce + origens na allowlist (sem `strict-dynamic`).
- Fontes e Tailwind são 100% first-party — não reintroduzir allowlists
  `fonts.googleapis.com` / `cdn.tailwindcss.com` (testes bloqueiam).
