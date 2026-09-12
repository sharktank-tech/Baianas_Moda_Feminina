"""Validação canônica de identificação fiscal brasileira usada no checkout.

O módulo é puro: não acessa banco, request, sessão, logs ou provider.
Valores inválidos nunca são incluídos na mensagem da exceção.
"""

from __future__ import annotations

import re


class BrazilianTaxIdError(ValueError):
    """Erro sanitizado de identificação fiscal brasileira."""


def _digits(value) -> str:
    return re.sub(r"\D", "", str(value or ""))


def normalize_br_cpf(value) -> str:
    """Retorna CPF canônico com 11 dígitos e checksum válido.

    Não gera, completa nem corrige CPF. Sequências repetidas são inválidas.
    A exceção contém somente um código fixo sanitizado.
    """
    digits = _digits(value)
    if len(digits) != 11 or len(set(digits)) == 1:
        raise BrazilianTaxIdError("cpf_invalid")

    numbers = [int(ch) for ch in digits]

    total = sum(numbers[i] * (10 - i) for i in range(9))
    check1 = (total * 10) % 11
    if check1 == 10:
        check1 = 0

    total = sum(numbers[i] * (11 - i) for i in range(10))
    check2 = (total * 10) % 11
    if check2 == 10:
        check2 = 0

    if numbers[9] != check1 or numbers[10] != check2:
        raise BrazilianTaxIdError("cpf_invalid")

    return digits


__all__ = ["BrazilianTaxIdError", "normalize_br_cpf"]
