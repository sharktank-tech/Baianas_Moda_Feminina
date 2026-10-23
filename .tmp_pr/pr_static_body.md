## Contexto

Scan do Weblyzer em produção (04/10 21:41): **Security A+ 100/100** ✅, mas Performance C com FCP/DCL 9.3s. Diagnóstico: todo `/static` invocava a Lambda (`Cache-Control: no-cache` + `X-Vercel-Cache: MISS` medidos) — em rajada de 32 requests, CSS (FCP) e deferred scripts (DCL) ficam na fila da função.

## Implementação

`loja/static_cache.py` (novo) — `install_static_cache(app)` via `after_request`, apenas para `request.path.startswith("/static/")`:

```
Cache-Control: public, max-age=60, s-maxage=3600
```

- **borda Vercel** serve o asset sem tocar a função (`s-maxage`);
- **navegador** revalida via ETag (que o `send_file` já emite) após 60s → **staleness máxima de 1 minuto** pós-deploy, valor combinado no plano;
- HTML/API **intactos** (teste garante).

Registrado em `loja/__init__.py` logo após `install_security_headers` (coexistência dos dois `after_request` testada).

## Testes (por item)

| Item | Teste |
|---|---|
| CSS com política pública + ETag preservado | `test_static_cache.py::test_static_asset_gets_public_cache_control` |
| JS e fontes também | `::test_static_js_and_font_also_cached` |
| HTML não ganha `s-maxage`/`max-age=60` | `::test_html_is_not_cached_by_this_installer` |
| Headers de segurança continuam no asset | `::test_static_keeps_security_headers` |

**Suíte completa:** `2059 passed, 90 skipped` (2055 + 4)

## Também neste PR (aprovado)

- **PENDENCIAS.md**: Security marcado como atingido (A+ 100/100), logs do 500 verificados (sem ocorrência), itens novos de imagem (**hero 774KB**, **32 imgs alicdn → espelhar no Supabase**) e item de cache.

## Validação pós-merge

1. `curl -I https://www.pollomodas.com.br/static/styles/tailwind.css` → `Cache-Control: public, max-age=60, s-maxage=3600`
2. Re-scanar Weblyzer (método fixo) para comparar FCP/requests

## Rollback

Reverter `f11c906` (4 arquivos).
