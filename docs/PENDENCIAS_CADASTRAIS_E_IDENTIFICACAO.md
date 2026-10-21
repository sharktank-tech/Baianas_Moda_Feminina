# Pendências cadastrais e identificação do fornecedor (interno)

**Documento interno — não é página pública.** Revisão de 20/10/2026
(PR #144). Registra, sem invenção de dados, a situação cadastral da operação
e as opções de regularização.

## 1. Status declarado (sem invenção e sem conformidade declarada)

- A operação **não possui CNPJ** e **não possui endereço comercial
  definido** (confirmado pelo lojista).
- O site **não exibe razão social, CNPJ nem endereço comercial** —
  comportamento correto enquanto não há dado real; **nenhuma declaração de
  conformidade completa** com o Decreto 7.962/2013 é feita.
- O modelo `ContactInfo` possui apenas `address`, `phone`, `email` e
  `hours` (`loja/modules/models.py`); **não existem campos** de CNPJ ou
  razão social no sistema. Telefone/WhatsApp: `phone` sem valor → bloco
  oculto no rodapé (correto; não configurar sem dado real).
- Endereço parcial anteriormente lido em produção ("Avenida JK, Centro,
  Palmas-TO", sem número/CEP) **não** está publicado no rodapé (render
  condicional exibe apenas se preenchido) e não deve ser tratado como
  endereço comercial completo.

## 2. Exigências aplicáveis à operação atual

Fonte: [Decreto 7.962/2013](https://www.planalto.gov.br/ccivil_03/_ato2011-2014/2013/decreto/d7962.htm)
(regulamenta o CDC no comércio eletrônico).

O sítio eletrônico deve disponibilizar, em local de destaque e de fácil
visualização (art. 2º):

- **I — nome empresarial e número de inscrição do fornecedor, quando
  houver**, no CPF ou CNPJ: hoje inexistentes; enquanto não houver inscrição,
  o "quando houver" dá alguma margem, mas a **identificação plena** depende
  de regularização (seção 3).
- **II — endereço físico e eletrônico, e demais informações necessárias
  para localização e contato**: **exigível mesmo sem CNPJ**; parcialmente
  atendido (e-mail único `atendimento@pollomodas.com.br`), mas sem endereço
  físico publicado → **pendência**.
- **IV — discriminação, no preço, de despesas adicionais ou acessórias,
  tais como as de entrega**: frete exibido no checkout (ok).
- **V — condições integrais da oferta, incluídas modalidades de pagamento,
  disponibilidade, forma e prazo da entrega**: o site **não exibe** prazo de
  entrega pré-compra; a página de trocas foi corrigida nesta PR para **não
  prometer** prazo que não existe (texto: frete no checkout; dúvidas via
  atendimento). **Exibir o prazo real** (produto/checkout) é pendência de
  implementação futura, fora deste PR.
- **VI — restrições à fruição da oferta**: nenhuma restrição oculta
  identificada.
- Art. 4º, parágrafo único: resposta ao consumidor em até 5 dias (refletido
  na página pública). Art. 5º: meios de arrependimento ostensivos
  (página `/trocas-e-devolucoes`).

## 3. Opções de regularização (fontes oficiais)

### 3.1 MEI — caminho mais simples para obter CNPJ

- Formalização **gratuita** no
  [Portal do Empreendedor](https://www.gov.br/pt-br/servicos/realizar-registro-como-microempreendedor-individual-mei):
  obtém CNPJ, emite nota fiscal, DAS simplificado, previdência própria.
- Limite de receita bruta (Tabela A): **R$ 81.000/ano** (R$ 6.750/mês
  proporcionais no ano de abertura). Tabela B (transportador autônomo de
  cargas): R$ 251.600/ano. O PLP 186/2026 propõe aumentos para 2027–2028,
  **ainda não vigente**
  ([MEMP — Teto do MEI](https://www.gov.br/memp/pt-br/teto-do-mei)).
- Condições: ocupação prevista no Anexo XI da Res. CGSN 140/2018 (comércio
  varejista de artigos do vestuário costuma constar da Tabela A —
  **confirmar no Portal**); no máximo 1 empregado; não ser sócio,
  administrador ou titular de outra empresa
  ([gov.br — condições MEI](https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/quero-ser-mei/o-que-e-ser-um-mei/verifique-se-voce-atende-as-condicoes-para-ser-mei-1)).
- **Endereço:** o serviço oficial de atualização cadastral do MEI aceita
  **"Endereço comercial ou residencial"**
  ([gov.br — atualização cadastral MEI](https://www.gov.br/pt-br/servicos/solicitar-alteracao-cadastral-do-microempreendedor-individual)):
  o **endereço residencial do lojista pode ser usado como sede** do MEI,
  sem necessidade de ponto comercial alugado. "MEI digital" **não** é
  regime distinto: é o app oficial "Meu MEI Digital" para gestão do mesmo
  MEI.
- Pré-requisitos: conta gov.br (nível prata/ouro), documento de
  identificação, dados de contato, dados do negócio; **consulta prévia na
  Prefeitura** para confirmar que a atividade comercial é permitida no
  endereço escolhido
  ([gov.br FAQ](https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/perguntas-frequentes)).
  Alvará de Funcionamento Provisório é emitido no ato da inscrição, com
  possibilidade de início imediato da atividade (Res. CGSIM 48/57, arts. 24
  e 46; dispensa de alvará para baixo risco A — Res. 51/2019).

### 3.2 Alternativa: ME / empresário individual (Simples Nacional)

Para faturamento acima do teto do MEI ou necessidade de sócios: abertura
como Microempresa (ME) via Junta Comercial — processo mais custoso e
burocrático; fora do alcance do MEI. Decisão do lojista.

### 3.3 O que não fazer

- Não exibir CNPJ fictício, endereço inventado ou "razão social" preenchida
  apenas com o nome fantasia sem registro.
- Não declarar o site "100% em conformidade" com o Decreto 7.962 enquanto os
  incisos I, II e V do art. 2º não forem plenamente atendidos.

## 4. O que pode ser feito agora × o que depende do lojista

| Agora (equipe técnica, sem CNPJ) | Depende do lojista |
|---|---|
| Manter o site sem dados inexistentes (feito) | Escolher regime: MEI (Tabela A) vs ME |
| Registrar a pendência neste documento (feito) | Consulta prévia na Prefeitura + formalização no Portal do Empreendedor |
| Textos corrigidos nesta PR: sem promessa de prazo inexistente, política de troca/arrependimento/defeito clara, contato único | Definir endereço (residencial MEI ou comercial) |
| Exibir prazo real de entrega pré-compra (produto/checkout) — implementação futura, requer decisão de UX | Informar CNPJ + endereço definitivos quando obtidos |
| Estender `ContactInfo`/rodapé para exibir CNPJ, razão social e endereço — **somente quando houver dado real** | Confirmar dados publicáveis (nome empresarial vs nome fantasia) |
| Manter telefone/WhatsApp ocultos até configuração real | Fornecer telefone/WhatsApp se desejar exibir |

## 5. Fontes

- Decreto 7.962/2013: https://www.planalto.gov.br/ccivil_03/_ato2011-2014/2013/decreto/d7962.htm
- gov.br — Cadastrar/formalizar MEI: https://www.gov.br/pt-br/servicos/realizar-registro-como-microempreendedor-individual-mei
- gov.br — Condições MEI (teto R$ 81 mil): https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/quero-ser-mei/o-que-e-ser-um-mei/verifique-se-voce-atende-as-condicoes-para-ser-mei-1
- gov.br — FAQ Empreendedor (consulta prévia, documentos): https://www.gov.br/empresas-e-negocios/pt-br/empreendedor/perguntas-frequentes
- gov.br — Atualização cadastral MEI (endereço comercial ou residencial): https://www.gov.br/pt-br/servicos/solicitar-alteracao-cadastral-do-microempreendedor-individual
- MEMP — Teto do MEI (PLP 186/2026 não vigente): https://www.gov.br/memp/pt-br/teto-do-mei
- Res. CGSIM 48 alterada pela 57 (arts. 24, 46): https://www.gov.br/empresas-e-negocios/pt-br/drei/cgsim/arquivos/Resoluo_48_alterada_pela_57.pdf/@@display-file/file
