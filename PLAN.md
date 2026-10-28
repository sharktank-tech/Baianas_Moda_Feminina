# Plano MVP — As Baianas Moda Feminina

Decisão: manter Flask/PostgreSQL existente para reduzir migração de risco. Não habilitar produção antes dos testes.

## Fases
- [x] Auditar herança do projeto anterior.
- [~] Isolar schemas e migrations (revisão e testes de PostgreSQL ainda pendentes).
- [ ] Revisar 100% dos caminhos de dados, scripts e políticas de acesso.
- [ ] Vitrine responsiva com catálogo e imagens reais.
- [ ] Checkout WhatsApp para convidado, com telefone autorizado, pedido persistido e cálculo pelo servidor.
- [ ] Admin: produtos, estoque, pedidos, cupons e relatórios básicos.
- [ ] Pix sandbox e webhook, antes de ativar produção.
- [ ] Testes unitários, integração PostgreSQL, E2E mobile, acessibilidade e SEO.

Critério de lançamento: dados reais confirmados, credenciais exclusivas, migrações validadas em ambiente descartável e nenhuma referência executável à PolloModas.
