"""Headers de segurança globais (Prioridade 1 do plano Weblyzer).

Aplicados via ``after_request`` em todas as respostas. Os valores são gravados
com ``setdefault`` para não sobrescrever headers específicos de rota (ex.:
``Referrer-Policy: no-referrer`` da página de redefinição de senha, 2.9).

Fora do escopo proposital:
- HSTS: a Vercel já envia ``max-age=63072000`` na borda.
- COEP: ``require-corp`` bloquearia AdSense/fontes/imagens externas sem CORP.

CSP (adicionada no PR #100) é uma allowlist mínima auditada contra os
templates reais: Tailwind CDN (``<script>`` do Play CDN), AdSense
(script + iframes de anúncio + beacons), Google Fonts (CSS + arquivos),
ViaCEP (busca de CEP no front) e a origem da própria API.
``script-src-attr 'none'`` do plano foi omitido: o app usa 25 handlers
``on*`` inline.
"""

DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)

CSP_DIRECTIVES = [
    "default-src 'self'",
    (
        "script-src 'self' 'unsafe-inline' 'unsafe-eval' "
        "https://cdn.tailwindcss.com https://*.googlesyndication.com "
        "https://*.googleadservices.com https://*.doubleclick.net"
    ),
    (
        "style-src 'self' 'unsafe-inline' "
        "https://cdn.tailwindcss.com https://fonts.googleapis.com"
    ),
    "img-src 'self' data: blob: https:",
    "font-src 'self' https://fonts.gstatic.com https://fonts.googleapis.com",
    (
        "connect-src 'self' https://viacep.com.br "
        "https://api.pollomodas.com.br https://www.google.com "
        "https://*.googlesyndication.com https://*.googleadservices.com "
        "https://*.doubleclick.net"
    ),
    "frame-ancestors 'self'",
    "frame-src https://*.googlesyndication.com https://*.doubleclick.net",
    "object-src 'none'",
    "base-uri 'self'",
    "form-action 'self'",
    "upgrade-insecure-requests",
]

CONTENT_SECURITY_POLICY = "; ".join(CSP_DIRECTIVES)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": DEFAULT_REFERRER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
    "Content-Security-Policy": CONTENT_SECURITY_POLICY,
}


def install_security_headers(app) -> None:
    @app.after_request
    def _set_security_headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


__all__ = ["SECURITY_HEADERS", "install_security_headers"]
