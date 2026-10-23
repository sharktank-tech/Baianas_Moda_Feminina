## Contexto

Fase 4 do plano aprovado (branches → Tailwind → AdSense → **CSP nonce** → docs). Era o último `!!` do Weblyzer (Security A 88): `script-src` com `'unsafe-inline'`/`'unsafe-eval'` e sem `script-src-attr 'none'` — este PR entrega o alvo **Security ≥90**.

## Implementação

### `loja/security_headers.py` (reescrito)
- `csp_nonce()`: `secrets.token_hex(32)` por requisição — sorteado em `before_request`, reconciliado de forma preguiçosa no context processor (requests que encurtam o `preprocess_request` não perdem o nonce) e emitido no header.
- `csp_directives(nonce)` — **novo header por requisição**:

| Antes | Depois |
|---|---|
| `script-src 'self' 'unsafe-inline' 'unsafe-eval' cdn.tailwindcss.com + ads` | `script-src 'self' 'nonce-{n}' + ads` |
| *(sem script-src-attr)* | **`script-src-attr 'none'`** |
| `style-src 'self' 'unsafe-inline' cdn.tailwindcss.com fonts.googleapis.com` | `style-src 'self' 'unsafe-inline'` |
| `font-src 'self' fonts.gstatic.com fonts.googleapis.com` | `font-src 'self'` |

- Allowlists mortas removidas: Google Fonts (#104) e Tailwind CDN (#110).
- `style-src 'unsafe-inline'` **mantido de propósito**: 12 blocos `<style>` + 9 `style=""` não usam nonce (escopo separado → registrado no PENDENCIAS da Fase 5).

### Templates (21 arquivos)
- **15 `<script>` inline** (incl. 2 JSON-LD) → `<script nonce="{{ csp_nonce }}">`.
- **25 `on*` eliminados** → delegação no script do `base.html` (todos os templates estendem `base.html`):
  - 20× `onsubmit="return confirm(...)"` → `data-confirm="..."` (listener `submit` + `preventDefault`)
  - `closeFlash(this)` → `data-dismiss-flash`
  - `location=this.value` → `data-nav-select`
  - `updateProductPreview()` → `data-change-fn`
  - `event.preventDefault()` no favorito → `type="button"`
- Varredura final: **0 `on*`, 0 `javascript:`** (testado).

## Testes (por item)

| Item | Teste |
|---|---|
| Header = `csp_directives(nonce)` exato | `test_security_headers.py::test_csp_header_is_present_and_matches_module` |
| Nonce único por requisição | `::test_csp_nonce_is_unique_per_request` |
| Sem `unsafe-inline`/`unsafe-eval`; `script-src-attr 'none'`; `style-src` mantém | `::test_csp_drops_unsafe_inline_and_eval` |
| Allowlist Google Fonts morta | `::test_csp_dropped_dead_google_fonts_allowlist` |
| Allowlist Tailwind CDN morta | `::test_csp_dropped_tailwind_cdn_allowlist` |
| AdSense origens preservadas (loader é pós-#111) | `::test_csp_allows_adsense_script_and_frames` |
| Todo `<script>` srcless da home com o nonce do header | `::test_home_inline_scripts_carry_header_nonce` |
| 404 também nonced | `::test_error_page_inline_scripts_carry_nonce` |
| 0 handlers `on*` nos templates | `::test_templates_have_no_inline_event_handlers` |
| 0 `javascript:` | `::test_templates_have_no_javascript_urls` |
| Loader do AdSense com nonce | `test_adsense_postload.py::test_adsense_loader_is_nonced` |

Invariantes preservadas (9 do módulo + hardening + viaCEP + no-referrer 2.9) seguem todas verdes.

**Suíte completa:** `2052 passed, 90 skipped` (2046 + 6 novos)

## Risco / validação (mudanças visíveis/funcionais — sua aprovação antes do merge)

1. **Anúncios AdSense sob CSP estrita** — validar no preview que auto ads renderizam (loader tem nonce; script dinâmico coberto por origem na allowlist).
2. **Confirm de exclusão no admin** (20 formulários, ex.: dashboard) — testar 1 exclusão: diálogo deve aparecer e cancelar deve impedir o submit.
3. **Flash com ×**, **ordenação do catálogo** (`data-nav-select`), **preview de promoção** (`data-change-fn`) e **favorito** (`type="button"`).
4. Console do navegador sem violações de CSP.

## Rollback

Reverter `354008e` (módulo + 21 templates + 2 testes de uma vez).
