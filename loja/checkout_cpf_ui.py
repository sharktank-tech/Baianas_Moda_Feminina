"""FASE 8.0.12C — campo CPF isolado nas páginas de checkout.

Evita duplicar/reformatar o template histórico de checkout. A transformação é
restrita aos endpoints GET de checkout e insere somente o campo de CPF após o
telefone. Segurança não depende desta camada: o backend continua validando e
falhando fechado antes do Mercado Pago.
"""

from __future__ import annotations

import re

from flask import request


_CHECKOUT_ENDPOINTS = {"main.checkout", "main.checkout_carrinho"}
_PHONE_GROUP = re.compile(
    r'(<div class="form-group">\s*'
    r'<label for="telefone">Telefone</label>\s*'
    r'<input\s+type="tel"\s+name="telefone"\s+id="telefone".*?>\s*'
    r'</div>)',
    re.DOTALL,
)

_CPF_FIELD = """

                    <div class="form-group">
                        <label for="cpf">CPF</label>
                        <input
                            type="text"
                            name="cpf"
                            id="cpf"
                            maxlength="14"
                            inputmode="numeric"
                            autocomplete="off"
                            placeholder="000.000.000-00"
                            required
                        >
                        <span class="form-help">Usado para identificação fiscal e entrega do pedido.</span>
                    </div>"""


def inject_checkout_cpf_field(html: str) -> str:
    if not html or 'name="cpf"' in html:
        return html
    return _PHONE_GROUP.sub(lambda match: match.group(1) + _CPF_FIELD, html, count=1)


def install_checkout_cpf_ui(app) -> None:
    @app.after_request
    def _inject_checkout_cpf(response):
        if request.method != "GET" or request.endpoint not in _CHECKOUT_ENDPOINTS:
            return response
        if response.status_code != 200 or not response.mimetype.startswith("text/html"):
            return response

        html = response.get_data(as_text=True)
        updated = inject_checkout_cpf_field(html)
        if updated != html:
            response.set_data(updated)
            response.content_length = len(response.get_data())
        return response


__all__ = ["inject_checkout_cpf_field", "install_checkout_cpf_ui"]
