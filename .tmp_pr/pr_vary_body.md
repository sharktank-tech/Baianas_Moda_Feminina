## Contexto

Follow-up do #116: em produção o header chega como `public, max-age=60` (borda descarta `s-maxage`) e **`X-Vercel-Cache: MISS` em toda fetch** — o cache da borda não estava funcionando.

**Causa medida**: toda resposta carrega `Vary: Cookie` — o Flask-Login lê a sessão em toda request e `flask/sessions.py:348` (`save_session`) marca o Vary. A Vercel **não cacheia resposta com `Vary: Cookie`**. E `save_session` roda **depois** de todos os `after_request` (`flask/app.py:1313→1322`) — o `pop` num `after_request` é inútil (o teste novo pegou exatamente isso).

## Implementação

`loja/static_cache.py` reescrito como **middleware WSGI** (último estágio, pós-`save_session`):

- em `/static/*`: grava `Cache-Control: public, max-age=60, s-maxage=3600` (garantido, substituindo o `no-cache` do `send_file`) e **remove só `Cookie` do `Vary`** (mantém outros valores);
- fora de `/static/*`: passa direto (HTML/API com `Vary: Cookie` intacto).

## Testes (por item)

| Item | Teste |
|---|---|
| Política pública + ETag preservado | `test_static_cache.py::test_static_asset_gets_public_cache_control` (do #116) |
| JS/fontes | `::test_static_js_and_font_also_cached` |
| HTML sem a política | `::test_html_is_not_cached_by_this_installer` |
| Segurança coexiste | `::test_static_keeps_security_headers` |
| **`Vary` sem `Cookie` (novo)** | `::test_static_has_no_vary_cookie` |

**Suíte completa:** `2060 passed, 90 skipped` (+1)

## Validação pós-merge

`curl -I` no `tailwind.css` duas vezes: esperado `Cache-Control` completo, **sem `Vary: Cookie`** e `X-Vercel-Cache: HIT` (ou `Age > 0`) na segunda.

## Rollback

Reverter `0a43b4d` (2 arquivos).
