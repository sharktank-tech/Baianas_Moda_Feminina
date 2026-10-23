## Feedback de carregamento (somente apresentação)

Barra fina indeterminada no topo em navegação interna + spinner/texto no
botão acionado, seguindo o padrão visual (rosa da marca, `adm-btn`/`btn`
existentes). Nada muda em rotas, CSRF, validações ou regras.

- **Reutiliza o existente**: checkout mantém seu mecanismo próprio
  (`setFinishLoading`, gates de cupom/frete, idempotency-key) via
  `data-loading="off"`; `data-once` e `data-confirm` delegados intactos;
  quantidade do carrinho excluída (`data-loading="off"`).
- **Barra**: indeterminada (sem %), com atraso anti-flash; ignora âncoras,
  downloads, externos, `mailto:`/`tel:`, nova aba e Ctrl/Cmd/Shift; nunca
  intercepta a navegação.
- **Botão**: spinner + texto (`Entrando…` no login, padrão `Processando…`,
  demais via `data-loading-text`), largura preservada, `aria-busy`; 2º
  envio bloqueado sem desabilitar o 1º (name/value intactos, múltiplos
  botões OK); só após `checkValidity()` (vale p/ `novalidate`); Enter usa
  o submitter correto.
- **Admin fetch**: botão/ação com `aria-busy`, restaurado no `finally` com
  guarda anti-stale (nunca encerra o mais novo); filtros/retry/Abort
  preservados.
- **Checkout**: texto só no envio real; sem retry de POST em lugar algum.
- **Restauração**: `pageshow` limpa barra/botões/flags (bfcache incluso);
  `prefers-reduced-motion` mantém só texto; CSP sem inline (JS externo +
  CSSOM pontual, como o base já faz); sem JS tudo funciona.

## Testes e CI

- `tests/test_loading_feedback.py`: 9 passed (fiação estática + servidor:
  script incluído, opt-outs, textos, CSRF/métodos preservados, sem
  `<script>` nas parciais... — ver arquivo).
- Suíte completa: **2436 passed**; `git diff --check` limpo.
- Sem merge/deploy; sem alteração de dados.

## Limitação declarada (browser)

Sem Chromium/PyPI/npm neste ambiente: **não há testes de navegador nem
screenshots** — os testes acima cobrem fiação e servidor, não comportamento
real de clique/histórico. Roteiro manual (desktop/mobile, rede lenta):
login, busca, produto, carrinho (+/− sem spinner), checkout (texto de
redirecionamento), abas do admin (filtro com spinner e restore), voltar do
navegador após envio, `prefers-reduced-motion` ativo.
