# T03 — Convenção UTM e fluxo de origem (testado, sem alteração de código)

Nenhuma alteração de código foi necessária: o fluxo existente passou nos
7 testes de `tests/test_acquisition_origin_flow.py`. A allowlist foi
preservada integralmente.

## Convenção

`?utm_source=<canal>&utm_medium=<modalidade>&utm_campaign=<nome_estavel>`

- Ex.: `?utm_source=instagram&utm_medium=organic_social&utm_campaign=piloto_look01`
- Mídia paga usa `paid_social`; nomes estáveis por criativo.
- Nunca incluir nome, e-mail, telefone ou número de pedido.
- Nunca adicionar UTMs em links internos (sobrescreveria a origem).
- Allowlist de caracteres: letras, dígitos, `_`, `.`, `-`, espaço; limites
  64/64/120 (source/medium/campaign). Fora disso → `None` (fail-closed).

## Fluxo testado

Primeira origem válida vence (first-touch em sessão) → navegação interna,
segunda campanha e passagem pelo gateway não sobrescrevem → snapshot
congelado nas colunas `acquisition_*` do pedido na criação. Acesso direto
sem origem não cria registro. Referrer externo entra só sem UTM.
