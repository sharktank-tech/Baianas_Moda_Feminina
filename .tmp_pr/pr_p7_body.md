## Contexto

Scan Weblyzer 04/10 22:28: FCP=DCL=load=**8.4s** agrupados, TTFB 157ms. Medições posteriores: borda **0.18s** (HIT), função quente **0.3s** → o 8.4s foi **primeiro toque no PoP do scanner: MISS → cold start da Lambda pesada** segurando o `tailwind.css` render-blocking. O cache de borda (#116/#117) resolve os toques seguintes, mas o caminho crítico ainda depende da função no 1º acesso.

## Implementação (P7)

- `git mv loja/static public/static` (37 renames **100%, 0 diff**): a Vercel serve `public/` direto do filesystem/CDN **antes** dos rewrites — `/static/*` não invoca mais `api/app.py`.
- `loja/__init__.py`: `Flask(..., static_folder=<repo>/public/static)` — fallback local aponta para o mesmo diretório (fonte única, zero duplicação).
- `vercel.json` +`headers`: `/static/(.*)` → `Cache-Control: public, max-age=60, s-maxage=3600` e `X-Content-Type-Options: nosniff` (o middleware WSGI do #117 continua valendo no fallback/local).
- `package.json`: `build:css`/`watch:css` → saída em `public/static/styles/tailwind.css` (regerado com o binário standalone: **bytes idênticos**, renome 100%).

## Testes (por item)

| Item | Teste |
|---|---|
| **`static_folder` = public/static + arquivos presentes (novo)** | `test_static_cache.py::test_static_folder_is_public_static` |
| Política/ETag no fallback | `test_static_cache.py` (5 do #116/#117) |
| Tailwind pré-compilado no caminho novo | `test_tailwind_precompiled.py` (OUTPUT atualizado) |
| Fontes auto-hospedadas | `test_font_self_hosted.py` (FONTS_DIR/CSS movidos) |
| Referências de CSS nos testes | `test_performance_assets.py` / `test_security_headers.py` / `test_product_content.py` |

**Suíte completa:** `2061 passed, 90 skipped` (+1)

## Validação pós-merge (a executar)

`GET /static/styles/tailwind.css` sem query: se vier **sem header CSP** e com `Cache-Control` do `vercel.json` → filesystem venceu (esperado). Segunda fetch: `Age`/`X-Vercel-Cache` da Vercel. Se vier CSP = rewrite venceu → **fallback seguro** (Flask serve o mesmo diretório), apenas sem o ganho.

## Rollback

Reverter `6e51cd7` (46 arquivos; 37 são renames 100%).
