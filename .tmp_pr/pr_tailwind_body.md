## Contexto

Fase 2 do plano aprovado (ordem: branches → **Tailwind** → AdSense → CSP → docs). O Play CDN era: terceiro-party em produção, única fonte de `'unsafe-eval'` da CSP e custo de DCL.

## Implementação (opção aprovada: package.json + CSS commitado)

| Item | Detalhe |
|---|---|
| `tailwind.config.js` | `content: ["./loja/templates/**/*.html"]` (68 templates; sem config inline, sem `@apply`, JS só usa `active`) |
| `package.json` | devDep `tailwindcss ^3.4.17` + scripts `build:css`/`watch:css` |
| `src/tailwind.input.css` | as 3 diretivas `@tailwind` |
| **`loja/static/styles/tailwind.css`** | gerado e **versionado** — Vercel continua sem Node |
| `base.html` | `<script cdn>` → `<link>` local (mesma posição) |
| Build local | binário **standalone Tailwind v3.4.17** (ambiente sem node/npm; `package.json` documenta o caminho npx p/ quem tiver Node) |

## Números do build

- **1.4 s** (meta ≤10 s) · **22.5 KB** raw / **4.7 KB gzip** (meta ≤50 KB gz)
- Auditoria de cobertura: **979 tokens** de classe limpos nos templates; **0 utilitárias Tailwind ausentes** (5 "ausentes" = classes próprias já mortas antes, ex.: `table-striped` — nem o CDN geraria)

## Testes (por item)

| Item | Teste |
|---|---|
| Home sem CDN + link local | `test_tailwind_precompiled.py::test_home_no_cdn_and_links_local_css` |
| 68 templates sem referência ao CDN | `::test_no_template_references_cdn` |
| CSS gerado existe e tem conteúdo (8-200KB, probes flex/hidden/bg-pink-600/md\\:) | `::test_generated_css_exists_and_is_substantial` |
| Config cobre todos os templates | `::test_config_content_covers_every_template` |
| Inputs de build documentados | `::test_build_inputs_documented` |
| Scripts locais seguem defer | `::test_cdn_removal_keeps_local_scripts_deferred` |

**Atualizado** (codificava o estado do #101): `test_performance_assets.py::test_home_has_local_fonts_and_local_tailwind` (antes exigia o `<script>` do CDN).

**Suíte completa:** `2041 passed, 90 skipped` (2034 + 7 novos)

## Efeitos esperados

- 1 terceiro-party a menos (6 → 5 domínios no Weblyzer)
- CSP: base para remover `'unsafe-eval'` no PR do nonce (fase 4)
- HTML/CSS idênticos visualmente (mesmas classes geradas pelo JIT)

## Risco / validação

Mudança visível → **validação sua no preview da Vercel antes do merge** (Home/admin devem parecer idênticos). CSP ainda allowlista `cdn.tailwindcss.com` (limpo no PR de nonce — sem efeito com o CDN fora do HTML).

## Rollback

Reverter o commit `b893c2c` (volta ao CDN + remove artefatos).
