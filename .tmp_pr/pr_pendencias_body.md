## Contexto

Fase 5 (docs) da rodada aprovada — fecha o ciclo: branches → Tailwind → AdSense → CSP nonce → **docs**.

## Conteúdo

- **`PENDENCIAS.md` novo** com backlog real pós-#112 e data de revisão (2026-11-04):
  1. Medição Weblyzer (alvos Security ≥90 / PageLoad ≤3.5s / requests ≤25)
  2. `style-src` sem `unsafe-inline` (nonce nos 12 blocos `<style>` + 9 attrs `style=""`)
  3. Cache de cupons — issue **#113** criada nesta fase
  4. Monitorar o 500 transitório `FUNCTION_INVOCATION_FAILED` observado pós-deploy do #112
- Referência da rodada: #108/#110/#111/#112 merged, #106/#107/#109 fechados com justificativa.

## Testes

Docs-only (sem código). CI do repositório roda por padrão.

## Validação

Sem mudança visível ao cliente. Merge sob a aprovação da etapa Fase 5.
