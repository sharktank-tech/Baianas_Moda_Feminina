"""Cache de borda para /static (plano Weblyzer — Performance).

O ``rewrites`` do ``vercel.json`` manda tudo para ``api/app.py``: sem
Cache-Control explícito, o ``send_file`` do Flask responde
``no-cache`` e **cada CSS/JS/fonte vira 1 invocação da Lambda**
(``X-Vercel-Cache: MISS``). Em rajada (32 requests) a função segura o
CSS (FCP) e os deferred scripts (DCL) — o FCP do scan de 21:41.

Com política pública:

- **borda Vercel** (``s-maxage=3600``): serve o asset sem tocar a função;
- **navegador** (``max-age=60`` + ETag que o ``send_file`` já emite):
  revalida em ≤60s — no máximo 1 minuto de staleness após um deploy.

Dois detalhes de implementação (ambos medidos em produção):

1. O Flask-Login lê a sessão em toda request e o ``save_session``
   (que roda DEPOIS dos ``after_request``) marca ``Vary: Cookie`` —
   a borda da Vercel **não cacheia** resposta com Vary de cookie.
   Por isso a correção vive num **middleware WSGI** (pós-``save_session``)
   e não num ``after_request``.
2. HTML e API ficam intactos: o middleware só age em ``/static/*``.
"""

STATIC_CACHE_CONTROL = "public, max-age=60, s-maxage=3600"


class StaticCacheMiddleware:
    """Aplica a política de cache e limpa ``Cookie`` do ``Vary`` em /static."""

    def __init__(self, wsgi_app, prefix: str):
        self.wsgi_app = wsgi_app
        self.prefix = prefix

    def __call__(self, environ, start_response):
        if not environ.get("PATH_INFO", "").startswith(self.prefix):
            return self.wsgi_app(environ, start_response)

        def _start_response(status, headers, exc_info=None):
            out = []
            has_cache_control = False
            for name, value in headers:
                lower = name.lower()
                if lower == "cache-control":
                    out.append(("Cache-Control", STATIC_CACHE_CONTROL))
                    has_cache_control = True
                    continue
                if lower == "vary" and "cookie" in value.lower():
                    keep = [
                        p.strip()
                        for p in value.split(",")
                        if p.strip() and p.strip().lower() != "cookie"
                    ]
                    if keep:
                        out.append(("Vary", ", ".join(keep)))
                    continue
                out.append((name, value))
            if not has_cache_control:
                out.append(("Cache-Control", STATIC_CACHE_CONTROL))
            return start_response(status, out, exc_info)

        return self.wsgi_app(environ, _start_response)


def install_static_cache(app) -> None:
    prefix = app.static_url_path.rstrip("/") + "/"
    app.wsgi_app = StaticCacheMiddleware(app.wsgi_app, prefix)


__all__ = ["STATIC_CACHE_CONTROL", "StaticCacheMiddleware", "install_static_cache"]
