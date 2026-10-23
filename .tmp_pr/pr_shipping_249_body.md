## O que muda

Corrige a intermitência de `test_carrinho_sem_cep_flag_on_mostra_estimativa`
(`tests/test_shipping_quote.py`): a busca global por `"249"` no HTML inteiro
falhava aleatoriamente porque o token CSRF (hexadecimal aleatório) pode conter
essa substring — ex.: `...66ee731476650feac78bf...` já derrubou o CI pós-merge
do PR #126.

- Novo helper `assert_frete_sem_tarifa_legada(html)`: extrai o valor **dentro
  do elemento específico de frete** do resumo (`<span>Frete</span>` seguido do
  `<span>` de valor) e afirma que o rótulo de estimativa está presente e que a
  tarifa legada (`249`, fallback anterior aos `10,00`/`18,90`) está ausente.
- Intenção e proteção preservadas: nenhuma tarifa concreta pode vazar no
  elemento de frete no estado pré-CEP com flag ligada. As demais asserções do
  teste (`"Frete calculado após informar o CEP"`, `"10,00"`, `"18,90"`) ficam
  como estão — valores com vírgula não ocorrem em token hexadecimal.
- Controle negativo commitado (`test_tarifa_indevida_dentro_da_celula_e_detectada`):
  forçando `_calculate_display_totals` a devolver frete 249,00 pela stack
  real, o helper reprova (`pytest.raises(AssertionError)`) — o teste detecta
  o comportamento quebrado.
- `test_tarifa_fora_do_resumo_nao_interfere_na_verificacao`: produto
  `"Vestido 249"` (substring fora do resumo) — a verificação escopada passa.
- Extração estrita: exige exatamente 1 ocorrência do elemento de frete;
  ausente ou ambíguo falha com mensagem clara (verificado 0 e 2 ocorrências).

## Verificação

- `tests/test_shipping_quote.py`: 74/74 em 5 execuções seguidas.
- Suíte completa: **2315 passed, 109 skipped**.
- `git diff --check` limpo.

Somente teste; sem merge/deploy; sem alteração em produção.
