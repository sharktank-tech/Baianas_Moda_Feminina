"""Cache de borda para /static (plano Weblyzer — Performance).

O ``rewrites`` do ``vercel.json`` manda tudo para ``api/app.py``: sem
Cache-Control explícito, o ``send_file`` do Flask responde
``no-cache`` e **cada CSS/JS/fonte vira 1 invocação da Lambda**
(``X-Vercel-Cache: MISS``). Em rajada (32 requests) a função segura o
CSS (FCP) e os deferred scripts (DCL) — o FCP do scan de 21:41.

Com política pública:

- **borda Vercel** (``s-maxage=3600``): serve o asset sem tocar a função;
- **navegador** (``max-age=60`` + ETag que o ``send_file`` já emite):
  revalida em ≤60s — no máximo 1 minuto de staleness após um deploy,
  dentro do combinado no plano.

Só respostas de ``/static/*`` são afetadas; HTML e API seguem sem cache.
"""

from flask import request

STATIC_CACHE_CONTROL = "public, max-age=60, s-maxage=3600"


def install_static_cache(app) -> None:
    prefix = app.static_url_path.rstrip("/") + "/"

    @app.after_request
    def _cache_static_assets(response):
        if request.path.startswith(prefix):
            response.headers["Cache-Control"] = STATIC_CACHE_CONTROL
        return response


__all__ = ["STATIC_CACHE_CONTROL", "install_static_cache"]
