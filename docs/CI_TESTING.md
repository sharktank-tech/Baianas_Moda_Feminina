# Testes e integração contínua

## Contrato do GitHub Actions

O workflow `.github/workflows/pr14-pytest.yml` deixou de ser exclusivo de
branches enumeradas. Apesar do nome histórico do arquivo, o workflow se chama
**Pytest** e executa a suíte SQLite completa nestes eventos:

- toda pull request cujo destino seja `master`;
- todo push efetivado em `master`;
- execução manual por `workflow_dispatch`.

A branch `feat/post-sale-status-sync-8.3.0-a` permanece temporariamente no
gatilho de `push` apenas para validar a PR #44, que introduz esta mudança antes
de o novo gatilho existir na branch padrão. Ela pode ser removida depois do
merge sem alterar o contrato permanente.

## Comando executado

```bash
python -m pytest -q tests/
```

O runner usa Python 3.12, instala `requirements-dev.txt`, define
`PYTHONPATH=.` e possui timeout de 20 minutos.

## Concorrência

Há no máximo uma execução relevante por PR/ref. Um novo commit cancela a
execução anterior da mesma PR, evitando consumo de runner com resultados
obsoletos.

## Interpretação dos checks

O preview da Vercel valida build e disponibilidade HTTP, mas não substitui o
pytest. Uma PR só deve declarar a suíte aprovada quando o check **Pytest /
pytest** concluir com sucesso.

## Testes locais e ambiente

O arquivo `.env` local pode alterar a configuração carregada pelos testes,
especialmente OAuth. Para reproduzir o ambiente limpo do CI, rode a suíte sem
variáveis locais conflitantes. Nunca versione credenciais, tokens ou o
conteúdo do `.env`.
