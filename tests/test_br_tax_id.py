import pytest

from loja.utils.br_tax_id import BrazilianTaxIdError, normalize_br_cpf


@pytest.mark.parametrize(
    "raw, expected",
    [
        ("52998224725", "52998224725"),
        ("529.982.247-25", "52998224725"),
        (" 529.982.247-25 ", "52998224725"),
    ],
)
def test_normalize_br_cpf_accepts_valid_values(raw, expected):
    assert normalize_br_cpf(raw) == expected


@pytest.mark.parametrize(
    "raw",
    [
        "",
        None,
        "11111111111",
        "52998224724",
        "123",
        "ABC",
    ],
)
def test_normalize_br_cpf_rejects_invalid_values_without_echo(raw):
    with pytest.raises(BrazilianTaxIdError) as exc:
        normalize_br_cpf(raw)

    assert exc.value.args == ("cpf_invalid",)
    if raw:
        assert str(raw) not in str(exc.value)
