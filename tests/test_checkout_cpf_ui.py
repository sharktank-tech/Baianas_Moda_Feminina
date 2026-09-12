"""FASE 8.0.12C — campo CPF isolado no HTML do checkout."""

from loja.checkout_cpf_ui import inject_checkout_cpf_field


def test_inject_checkout_cpf_field_after_phone_group():
    html = """
    <form id="checkoutForm">
      <div class="form-group">
        <label for="telefone">Telefone</label>
        <input type="tel" name="telefone" id="telefone" value="">
      </div>
    </form>
    """

    updated = inject_checkout_cpf_field(html)

    assert 'name="cpf"' in updated
    assert 'id="cpf"' in updated
    assert 'maxlength="14"' in updated
    assert 'required' in updated
    assert updated.index('name="telefone"') < updated.index('name="cpf"')


def test_inject_checkout_cpf_field_is_idempotent():
    html = """
    <div class="form-group">
      <label for="telefone">Telefone</label>
      <input type="tel" name="telefone" id="telefone" value="">
    </div>
    """

    once = inject_checkout_cpf_field(html)
    twice = inject_checkout_cpf_field(once)

    assert once == twice
    assert twice.count('name="cpf"') == 1


def test_inject_checkout_cpf_field_leaves_unrelated_html_untouched():
    html = '<form><input name="email"></form>'
    assert inject_checkout_cpf_field(html) == html
