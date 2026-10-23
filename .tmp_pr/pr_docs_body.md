## Contexto

Atualização do backlog após a rodada P6/P7 e o scan Weblyzer de 05/10 09:19.

## Mudanças (arquivo único: `PENDENCIAS.md`)

| Item | Mudança |
|---|---|
| Tabela "Rodada concluída" | + #114, #115, #116, #117, #118 com estado/validação |
| Linha de produção | `2a1e08f`, `2061 passed`, Security A+ nos 2 scans |
| Pendência 1 | reenquadrada: **Métrica independente (PSI)** — FCP do Weblyzer variou 3.1/8.4/9.3/20.5s com código idêntico e 20.5 contradiz DCL 3.1s (artefato); evidência do ganho real = DCL 8.4 → 3.1s |
| Pendência 4 | nota: 500 transitório não se repetiu nos merges #117/#118 |
| Pendência 7 | **✅ concluída** (#116 + #117 + #118, evidência de borda/filesystem em produção) |
| Decisões | + fonte única `public/static` (não reverter) e FCP do Weblyzer fora dos critérios de aceite até a revisão |

## Testes

Docs-only — nenhum teste de código afetado; CI do PR (pytest) roda por padrão.

## Rollback

Reverter `50f17b0` (1 arquivo).
