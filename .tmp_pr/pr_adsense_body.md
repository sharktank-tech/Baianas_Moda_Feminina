## Contexto

Fase 3 do plano aprovado (branches → Tailwind → **AdSense** → CSP → docs). O `adsbygoogle.js` era o único request de terceiro-party no critical path do `<head>` (auto ads, sem `<ins>` slots — só o loader).

## Implementação (opção aprovada: pós-`window load`)

```html
<script>
  window.addEventListener('load', function () {
    var adsbygoogle = document.createElement('script');
    adsbygoogle.async = true;
    adsbygoogle.crossOrigin = 'anonymous';
    adsbygoogle.src = 'https://pagead2.googlesyndication.com/pagead/js/adsbygoogle.js?client=ca-pub-…';
    document.head.appendChild(adsbygoogle);
  });
</script>
```

- Request do AdSense sai do page-load (critério: **PageLoad ≤3.5s / requests ≤25**)
- `client=` id preservado byte a byte (testado — evitaria quebra silenciosa de receita)
- Auto ads inicializam normalmente quando o script carrega pós-load (inserção dinâmica é prática suportada)

## Testes (por item)

| Item | Teste |
|---|---|
| Sem `<script src>` eager do adsbygoogle | `test_adsense_postload.py::test_home_has_no_eager_adsense_script_tag` |
| Loader cria script no `window load` | `::test_adsense_loader_deferred_to_window_load` |
| Client id preservado (1 ocorrência) | `::test_adsense_client_id_preserved` |
| Só `base.html` referencia (auto ads herda) | `::test_adsense_single_template_reference` |
| Loader inline (hoje CSP tem unsafe-inline) | `::test_loader_stays_inline_pending_nonce_pr` |

**Suíte completa:** `2046 passed, 90 skipped` (2041 + 5 novos)

## Efeitos esperados

- Requests no page-load: −1 (terceiro-party sai do caminho crítico)
- Weblyzer: PageLoad deve cair (medir preview)

## Risco / validação

- **Mudança visível** (anúncios): validar no preview que os auto ads ainda renderizam após o load — é sua aprovação antes do merge.
- O loader é inline → o próximo passo (CSP nonce) precisa colocar o nonce neste `<script>` (há teste marcando isso).

## Rollback

Reverter `3f0d668` (volta ao `<script async src>` original).
