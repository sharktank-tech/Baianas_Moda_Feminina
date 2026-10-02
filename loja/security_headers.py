"""Headers de segurança globais (Prioridade 1 do plano Weblyzer).

Aplicados via ``after_request`` em todas as respostas. Os valores são gravados
com ``setdefault`` para não sobrescrever headers específicos de rota (ex.:
``Referrer-Policy: no-referrer`` da página de redefinição de senha, 2.9).

Fora do escopo proposital:
- HSTS: a Vercel já envia ``max-age=63072000`` na borda.
- COEP: ``require-corp`` bloquearia AdSense/fontes/imagens externas sem CORP.
- CSP: entra em PR separado (allowlist auditada contra o site real).
"""

DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)

SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": DEFAULT_REFERRER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
}


def install_security_headers(app) -> None:
    @app.after_request
    def _set_security_headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        return response


__all__ = ["SECURITY_HEADERS", "install_security_headers"]
