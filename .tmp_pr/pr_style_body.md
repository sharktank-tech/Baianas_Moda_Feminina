## Contexto

PENDENCIAS #2 (aprovada): o scan do Weblyzer de 21:00 continuou com `!!` em CSP porque `style-src` ainda carregava `'unsafe-inline'` — verificado ao vivo: `script-src` já estava limpo (nonce). Este PR é o último `!!` do Security (alvo ≥90).

## Implementação

| Item | Mudança |
|---|---|
| CSP | `style-src 'self' 'nonce-{n}'` (mesmo nonce do header) + **`style-src-attr 'none'`** |
| 12 blocos `<style>` | ganham `nonce="{{ csp_nonce }}"` (12 templates) |
| 9 atributos `style=""` | → classes Tailwind: `mt-7` ×3 (conta ×2 + footer), `bg-[#fef2f2] border border-[#ef4444] text-[#991b1b] py-3 px-3.5 rounded-lg mb-[1.15rem] text-sm` (alerta de redefinir-senha), `mt-2.5`, `text-[#991b1b] font-bold`; os 3 `color:inherit` do footer foram **removidos** (preflight do Tailwind já faz `a{color:inherit}`) |
| `tailwind.css` | regerado (667ms, 23KB) com as novas classes |
| Cascata | conferido: `.account-panel`/`.footer-col-title` não têm `margin-top` conflitante; nenhum JS usa `cssText`/`setAttribute("style")` (só CSSOM `.style.x`, permitido pela CSP) |
| PENDENCIAS.md | item #2 marcado como concluído |

## Testes (por item)

| Item | Teste |
|---|---|
| `style-src` sem `unsafe-inline` + `style-src-attr 'none'` + nonce | `test_security_headers.py::test_csp_drops_unsafe_inline_and_eval` (atualizado) |
| 12 `<style>` todos nonced (source scan) | `::test_templates_style_blocks_all_nonced` |
| 0 atributos `style=""` (source scan) | `::test_templates_have_no_style_attributes` |
| CSS regerado com as classes convertidas | `::test_converted_utilities_are_in_built_css` |

Invariantes do script (nonce, script-src, ads, 0 `on*`) seguem verdes.

**Suíte completa:** `2055 passed, 90 skipped` (2052 + 3 novos)

## Risco / validação (mudança visível — sua aprovação antes do merge)

`style-src-attr 'none'` **bloqueia atributos de estilo de terceiros também** — risco central: o **AdSense pode setar `style=""` nos elementos que injeta na página**. Validar no preview:

1. Anúncios continuam renderizando (visual + Console sem violação `style-src`)
2. Visual idêntico: alerta vermelho de `/redefinir-senha` (com erro), painéis da conta, rodapé (telefone/e-mail/Pagamento)
3. Re-scanar Weblyzer após merge (Security ≥90)

Se o AdSense quebrar: rollback completo do commit ou fallback `style-src-attr 'unsafe-inline'` (perderia o score).

## Rollback

Reverter `02f429b` (18 arquivos: módulo + 16 templates + CSS + testes).
