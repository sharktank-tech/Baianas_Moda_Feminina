"""Headers de segurança globais (Prioridade 1 do plano Weblyzer).

Aplicados via ``after_request`` em todas as respostas. Os valores são gravados
com ``setdefault`` para não sobrescrever headers específicos de rota (ex.:
``Referrer-Policy: no-referrer`` da página de redefinição de senha, 2.9).

Fora do escopo proposital:
- HSTS: a Vercel já envia ``max-age=63072000`` na borda.
- COEP: ``require-corp`` bloquearia AdSense/imagens externas sem CORP.

Histórico da CSP (auditoria ao vivo contra os templates reais):
- PR #100: allowlist estática (Tailwind CDN, AdSense, Google Fonts, ViaCEP);
  ``script-src-attr 'none'`` ficou de fora por 25 handlers ``on*`` inline.
- #104 removeu o Google Fonts, #110 removeu o Tailwind Play CDN e #111
  tirou o ``<script>`` eager do AdSense — as allowlists mortas saem aqui.
- PR P4 (este): ``script-src`` sem ``'unsafe-inline'``/``'unsafe-eval'``;
  cada ``<script>`` inline recebe ``nonce`` por requisição
  (``g.csp_nonce`` = ``secrets.token_hex(32)``, sorteado em ``before_request``
  e reconciliado de forma preguiçosa para requests que encurtam o
  ``preprocess_request``); os 25 ``on*`` viraram data-attributes +
  delegação (``base.html``), habilitando ``script-src-attr 'none'``.
- PR P4b: ``style-src`` também perde ``'unsafe-inline'`` (último ``!!`` do
  Weblyzer) — os 12 blocos ``<style>`` ganham o mesmo nonce e os 9
  atributos ``style=""`` viraram classes utilitárias do Tailwind;
  ``style-src-attr 'none'`` fecha atributos de estilo. CSSOM
  (``el.style.x = ...`` nos scripts) não é afetado pela CSP.
"""

import secrets

from flask import g

DEFAULT_REFERRER_POLICY = "strict-origin-when-cross-origin"

PERMISSIONS_POLICY = (
    "accelerometer=(), camera=(), geolocation=(), gyroscope=(), "
    "magnetometer=(), microphone=(), payment=(), usb=()"
)

# Origens de anúncio: o loader do #111 cria o <script> dinamicamente e o
# loader leva nonce; os destinos seguem na allowlist (sem 'strict-dynamic').
AD_SCRIPT_ORIGINS = (
    "https://*.googlesyndication.com "
    "https://*.googleadservices.com "
    "https://*.doubleclick.net"
)


def csp_nonce():
    """Nonce atual da requisição (sorteia sob demanda)."""
    nonce = getattr(g, "csp_nonce", None)
    if not nonce:
        nonce = secrets.token_hex(32)
        g.csp_nonce = nonce
    return nonce


def csp_directives(nonce: str) -> list[str]:
    return [
        "default-src 'self'",
        f"script-src 'self' 'nonce-{nonce}' {AD_SCRIPT_ORIGINS}",
        "script-src-attr 'none'",
        f"style-src 'self' 'nonce-{nonce}'",
        "style-src-attr 'none'",
        "img-src 'self' data: blob: https:",
        "font-src 'self'",
        (
            "connect-src 'self' https://viacep.com.br "
            "https://www.google.com "
            f"{AD_SCRIPT_ORIGINS}"
        ),
        "frame-ancestors 'self'",
        f"frame-src {AD_SCRIPT_ORIGINS}",
        "object-src 'none'",
        "base-uri 'self'",
        "form-action 'self'",
        "upgrade-insecure-requests",
    ]


SECURITY_HEADERS = {
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": DEFAULT_REFERRER_POLICY,
    "Permissions-Policy": PERMISSIONS_POLICY,
    "Cross-Origin-Opener-Policy": "same-origin",
}


def install_security_headers(app) -> None:
    @app.before_request
    def _issue_csp_nonce():
        csp_nonce()

    @app.context_processor
    def _expose_csp_nonce():
        return {"csp_nonce": csp_nonce()}

    @app.after_request
    def _set_security_headers(response):
        for header, value in SECURITY_HEADERS.items():
            response.headers.setdefault(header, value)
        response.headers.setdefault(
            "Content-Security-Policy",
            "; ".join(csp_directives(csp_nonce())),
        )
        return response


__all__ = [
    "SECURITY_HEADERS",
    "csp_directives",
    "csp_nonce",
    "install_security_headers",
]
