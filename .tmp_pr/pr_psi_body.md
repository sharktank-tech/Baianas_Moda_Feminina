## Contexto

Resultado do PageSpeed (Lighthouse 13.5.0, 05/10 09:42 BRT) — métrica de aceite independente definida na pendência 1.

## Resultado

| | Mobile (Moto G Power, 4G lento) | Desktop |
|---|---|---|
| **Performance** | **99** | 84 |
| FCP / LCP | 0,9s / 1,0s | 0,3s / 1,6s |
| TBT / CLS | 0ms / 0,001 | 40ms / 0,006 |
| Speed Index | 3,3s | 5,6s |
| A11y / BP / SEO | 90 / 92 / 100 | 90 / 92 / 100 |

CWV verdes nos dois dispositivos; confirma que o FCP 20,5s do Weblyzer era artefato.

## Mudanças (arquivo único: `PENDENCIAS.md`)

- Pendência **1 → ✅** com os números acima.
- Pendências 5/6: referência à auditoria "Melhorar a entrega de imagens" (**2.998 KiB** mobile / 2.587 KiB desktop) — maior economia possível.
- Nova pendência **8** (baixa): SI desktop, JS não usado 148 KiB, imgs sem `width`/`height`, render-blocking 300–480 ms — triar junto de #5/#6.
- Linha de produção: + PSI mobile 99 / desktop 84.

## Testes

Docs-only — CI do PR (pytest) roda por padrão.

## Rollback

Reverter `9657829` (1 arquivo).
