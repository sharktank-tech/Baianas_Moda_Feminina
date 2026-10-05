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
| 1 | **Medição Weblyzer pós-#112** | Fechar os alvos do plano: Security **≥90** (era 88 com `unsafe-inline`) e PageLoad **≤3.5s / requests ≤25** (critério do AdSense pós-load) | Rodar o Weblyzer em produção e registrar o print aqui | 2026-11-04 |
| 2 | ~~**`style-src` sem `unsafe-inline`**~~ | ~~Última permissão inline da CSP~~ | **Concluído em 2026-10-04** (PR P4b): nonce nos 12 blocos `<style>`, 9 atributos `style=""` → classes Tailwind, `style-src-attr 'none'` | ✅ |
| 3 | **Cache de cupons com TTL** — [#113](https://github.com/sharktank-tech/PolloModas/issues/113) | `_restore_checkout_coupon` reconsulta o Supabase a cada render do checkout (`routes.py:1031`) | Implementar via `loja/cache.py` (TTL 60s + invalidação nos mutations de admin) conforme a issue | 2026-11-04 |
| 4 | **Monitorar cold start pós-deploy** | Em 2026-10-04, logo após o merge do #112, produção retornou `FUNCTION_INVOCATION_FAILED` (500) em todas as rotas por ~3 min; preview do mesmo commit esteve sempre 200 e a produção se recuperou sozinho | Se recorrer: coletar logs da Vercel e checar limite de import/cold-start do `api/app.py` | 2026-11-04 |

## Decisões já tomadas (não reabrir sem motivo)

- `style-src` mantém `'unsafe-inline'` por ora (linha 2 acima é o caminho).
- Nonce usa `secrets.token_hex(32)` por requisição, com fallback preguiçoso
  no context processor para requests que encurtam o `preprocess_request`.
- AdSense: loader com nonce + origens na allowlist (sem `strict-dynamic`).
- Fontes e Tailwind são 100% first-party — não reintroduzir allowlists
  `fonts.googleapis.com` / `cdn.tailwindcss.com` (testes bloqueiam).
