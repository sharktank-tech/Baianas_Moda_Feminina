# Design System — inspirado na Zattini

> **Nota de origem:** o acesso direto a `zattini.com.br` foi bloqueado por proteção
> anti-bot no momento da análise. Este documento foi construído a partir de
> informações públicas confirmadas da marca (Zattini é um e-commerce de moda
> lançado pela Netshoes em 2014, hoje parte do grupo Magalu, com identidade
> visual em rosa/magenta) combinadas com os padrões consolidados de UX de
> e-commerces de moda brasileiros da mesma categoria (Dafiti, Netshoes, Kanui):
> grade densa de produtos, parcelamento em destaque, selos de frete grátis,
> cupom/desconto agressivo, navegação por mega menu. **Se precisar de fidelidade
> pixel-perfect, tire e anexe screenshots reais do site** — o Claude Code deve
> tratá-los como fonte de verdade acima deste documento.

Este arquivo é a referência única de design para o projeto. Ao editar qualquer
página, componente ou documentação relacionada a UI, o Claude Code deve
consultar este arquivo e realinhar o que encontrar com o que está descrito
aqui.

---

## 1. Personalidade da marca

- **Categoria:** moda (roupas, calçados, acessórios, beleza) para todos os
  públicos — feminino, masculino, infantil, plus size.
- **Tom:** popular, acessível, vibrante, sem pretensão de luxo. Fala direto
  com quem quer se vestir bem gastando pouco.
- **Promessa central:** variedade + preço baixo + parcelamento facilitado +
  entrega para o Brasil todo.
- **Adjetivos de direção:** vibrante, feminino-comercial (mas não exclusivo),
  confiável, promocional, "vitrine de loja de shopping popular" — nunca
  editorial/minimalista de luxo.

---

## 2. Paleta de cores

| Token | Hex | Uso |
|---|---|---|
| `--brand-pink` | `#EC008C` (magenta/pink vibrante) | Logo, CTAs primários, links ativos, ícone de favoritos ativo |
| `--brand-pink-dark` | `#B0006A` | Hover/estado pressionado do CTA primário |
| `--ink` | `#1A1A1A` | Texto principal, preço "de" riscado |
| `--gray-700` | `#4D4D4D` | Texto secundário, descrições |
| `--gray-300` | `#D9D9D9` | Bordas, divisores |
| `--gray-100` | `#F5F5F5` | Fundo de seções alternadas, fundo de card |
| `--white` | `#FFFFFF` | Fundo base |
| `--sale-red` | `#E5251B` | Preço promocional, badge de "OFF %", contador de oferta relâmpago |
| `--success-green` | `#1DB954` / `#0A8A3E` | Frete grátis, "em estoque", confirmação |
| `--star-yellow` | `#FFC107` | Avaliação por estrelas |

Regras de aplicação:
- Fundo é **sempre branco ou cinza muito claro** — nunca dark mode nesta loja.
- O rosa é usado com **moderação estratégica**: logo, botão de comprar/adicionar,
  ícone de favorito ativo, tags de categoria em destaque. Não pintar blocos
  inteiros de rosa.
- Vermelho é reservado exclusivamente para **preço com desconto e urgência**
  (não usar para erros de formulário — usar uma cor de alerta neutra separada,
  ex. `#C0392B` com ícone, para não competir com o vermelho de oferta).

---

## 3. Tipografia

- **Família:** sans-serif humanista/grotesca, arredondada nos pesos bold —
  ex. `Poppins`, `Nunito Sans` ou `Inter` como alternativa de sistema.
- **Escala:**
  | Papel | Tamanho | Peso |
  |---|---|---|
  | H1 (categoria/página) | 28–32px | 700 |
  | H2 (seção) | 20–22px | 700 |
  | Nome do produto | 14px | 400–500 |
  | Preço promocional | 20–24px | 800 |
  | Preço "de" (riscado) | 13px | 400, `text-decoration: line-through`, cor `--gray-700` |
  | Parcelamento | 12–13px | 400 |
  | Legenda/badge | 11–12px | 700, uppercase, letter-spacing +0.02em |
- Preços **sempre em destaque tipográfico maior** que o nome do produto — é a
  informação que mais importa nesta categoria de loja.

---

## 4. Grid e espaçamento

- Grid de 12 colunas, contêiner máximo ~1280px, calhas de 16–24px.
- Espaçamento em escala de 4px (4, 8, 12, 16, 24, 32, 48, 64).
- Cantos arredondados moderados: `border-radius: 8px` em cards e botões,
  `999px` (pill) em badges/tags.
- Sombra suave em cards no hover: `box-shadow: 0 2px 8px rgba(0,0,0,0.08)`.

---

## 5. Estrutura de página (home)

Legenda de confiança usada nesta seção:
- ✅ **Confirmado** — verificado em fontes públicas sobre o site real da Zattini.
- 🔶 **Inferido** — padrão típico de e-commerces de moda da mesma categoria
  (Dafiti, Netshoes, Kanui), usado como melhor suposição na ausência de
  acesso direto ao site.

A home se organiza em 8 blocos empilhados verticalmente:

```
┌─────────────────────────────────────────────┐
│ 1. Barra topo: frete grátis / promo genérica │  🔶 faixa fina, fundo rosa ou preto, texto branco
├─────────────────────────────────────────────┤
│ 2. [Logo]  [Busca ampla]  [Login]            │  🔶
│            [Sacola] [Favoritos]              │
├─────────────────────────────────────────────┤
│ 3. Menu horizontal: Feminino Masculino       │  🔶 mega menu no hover/click
│    Infantil Calçados Bolsas Beleza           │
│    OFERTAS (destaque rosa)                   │
├─────────────────────────────────────────────┤
│ 4. [ Carrossel hero — banners de campanha ]  │  🔶
├─────────────────────────────────────────────┤
│ 5. Tira de categorias em ícone circular      │  🔶 "Tênis" "Vestidos" "Bolsas"...
├─────────────────────────────────────────────┤
│ 6. "Ofertas do dia" (canto sup. direito,     │  ✅ confirmado: bloco com
│    com cronômetro regressivo)                │     cronômetro até o fim da oferta
│    Vitrine "Mais vendidos" → grade de cards  │  🔶
│    Vitrine "Novidades"     → grade de cards  │  🔶
├─────────────────────────────────────────────┤
│ 7. Selos de confiança (compra segura, troca, │  🔶
│    parcelamento, entrega Brasil todo)        │
│    + Cadastro de newsletter em destaque      │  ✅ confirmado: newsletter
├─────────────────────────────────────────────┤
│ 8. Rodapé: institucional / atendimento /     │  🔶
│    redes sociais / formas de pagamento       │
└─────────────────────────────────────────────┘
```

### 5.1 Detalhamento bloco a bloco

**1. Faixa de topo (utility bar)**
Tira fina, geralmente na cor de marca ou preta, com mensagem fixa de reforço
comercial (ex. "frete grátis para todo o Brasil" ou cupom genérico). Ocupa
pouquíssima altura, mas é a primeira coisa vista.

**2. Header principal**
Logo à esquerda, **barra de busca larga e sempre aberta** ao centro — a
busca é o principal jeito de navegar nesse tipo de loja, então nunca fica
escondida atrás de um ícone — e à direita os ícones de conta, favoritos e
sacola, tudo compacto numa única linha.

**3. Menu de categorias**
Linha horizontal abaixo do header com as categorias principais (feminino,
masculino, infantil, calçados, bolsas...). Quase sempre há um item
**"Ofertas" destacado na cor da marca** para puxar o olho para a página de
promoções.

**4. Hero / carrossel de banners**
Banner largo e rotativo (3–5 slides com indicadores/bolinhas embaixo)
anunciando campanhas sazonais, coleções novas ou grandes descontos. É a
área mais "editorial" da página, mas ainda comercial — texto grande com CTA
tipo "compre agora".

**5. Tira de categorias em ícone circular**
Carrossel horizontal de ícones redondos (tênis, vestidos, bolsas, beleza...)
funcionando como atalho visual rápido, comum porque o público costuma
entrar já sabendo o que quer.

**6. Ofertas do dia + vitrines de produtos** ✅ (bloco "Ofertas do dia" confirmado)
A seção **"Ofertas do dia"** aparece no canto superior direito da área de
conteúdo, com um **cronômetro regressivo** mostrando o prazo até a troca dos
produtos em promoção (a cada 24h os itens são substituídos). Abaixo dela,
seguem outras vitrines empilhadas ("Mais vendidos", "Novidades"), cada uma
com título curto seguido de uma grade de cards de produto. Cada card traz:
- foto do produto sobre fundo branco
- ícone de coração para favoritar, sempre visível
- selo de desconto (ex. "-30%") no canto
- preço "de" riscado em cinza e preço final em destaque, geralmente em
  vermelho
- linha de parcelamento ("3x sem juros")

Essa é a área com maior densidade de informação da página.

**7. Selos de confiança + newsletter** ✅ (newsletter confirmada)
Tira com 3–4 ícones simples reforçando frete, parcelamento, troca fácil e
compra segura. Nesse mesmo miolo da página (ou logo abaixo), fica o
**formulário de cadastro na newsletter, em posição simples e destacada** —
confirmado como prática real do site, usado para capturar e-mail em troca
de promoções e conteúdo exclusivo.

**8. Rodapé**
Bloco denso e institucional: links de atendimento, sobre a empresa, redes
sociais, formas de pagamento aceitas e selos de segurança.

### 5.2 O que caracteriza esse design, em resumo

- **Prioridade absoluta ao preço e à promoção** — tipografia do preço é
  sempre maior e mais pesada que o nome do produto.
- **Densidade alta**: muitas vitrines empilhadas, pouco espaço em branco
  comparado a um site editorial/luxo.
- **Rosa como cor de assinatura, usado com moderação** — aparece no logo,
  CTAs e destaques, nunca como fundo de blocos inteiros.
- **Vermelho reservado só para desconto/urgência**, nunca para outra
  função (ex. erros de formulário usam outra cor).
- **Urgência via cronômetro** ✅ — o bloco de ofertas do dia usa contagem
  regressiva para reforçar a decisão de compra imediata.
- **Captura de e-mail cedo na jornada** ✅ — newsletter em posição de
  destaque na própria home, não escondida no rodapé ou em popup tardio.
- **Mobile como prioridade real**, não um adendo — grade de 2 colunas,
  busca sempre visível, botão de comprar fixo no rodapé da tela em produto.

---

## 6. Componentes-chave

### 6.1 Card de produto
- Imagem quadrada/retrato do produto sobre fundo branco, sem sombra na
  própria foto.
- Ícone de coração (favoritar) no canto superior direito, sempre visível
  (não só no hover, pois grande parte do tráfego é mobile).
- Badge de desconto (`-30%`) no canto superior esquerdo, pill vermelha.
- Nome da marca (ex. "Nike", "Renner") em cinza acima do nome do produto.
- Nome do produto truncado em 2 linhas.
- Bloco de preço:
  ```
  R$ 199,90  (riscado, cinza)
  R$ 139,90  (bold, preto ou vermelho se em oferta)
  em 3x de R$ 46,63 sem juros
  ```
- Avaliação em estrelas + contagem de avaliações abaixo do preço (opcional,
  quando disponível).
- Toda a área do card é clicável; botão de comprar aparece só no hover
  (desktop) ou fixo no rodapé do card (mobile).

### 6.2 Botões
- **Primário (CTA):** fundo `--brand-pink`, texto branco, bold, radius 8px,
  padding generoso (`12px 24px`). Hover: `--brand-pink-dark`.
- **Secundário:** contorno cinza, texto `--ink`, fundo branco.
- **Terciário/link:** texto rosa, sem fundo, sublinhado no hover.

### 6.3 Badges e selos
- Pill de desconto: fundo vermelho, texto branco, bold, uppercase pequeno.
- Selo "Frete grátis": fundo verde-claro ou apenas ícone caminhão + texto verde.
- Selo "Chegou": fundo preto ou rosa, texto branco, para lançamentos.

### 6.4 Busca
- Campo de busca amplo e centralizado no header, sempre visível (não
  colapsado atrás de um ícone), com placeholder tipo "Busque por vestido,
  tênis, marca...".

### 6.5 Filtros (página de listagem)
- Sidebar esquerda no desktop (Categoria, Marca, Tamanho, Cor, Preço),
  colapsável em accordion no mobile via botão "Filtrar" fixo no topo da
  grade de resultados.
- Chips removíveis mostrando os filtros ativos acima da grade.

### 6.6 Avaliações
- Estrelas amarelas preenchidas, meia-estrela suportada, número de
  avaliações entre parênteses.

---

## 7. Tom de voz / copy

- Frases curtas, imperativas e de benefício direto: **"Aproveite"**,
  **"Garanta o seu"**, **"Frete grátis para todo o Brasil"**.
- Sempre reforçar parcelamento sem juros e desconto no PIX/boleto quando
  aplicável.
- Evitar jargão técnico; usar o vocabulário do consumidor final (ex.
  "Sacola", não "Carrinho de compras" — mas ambos são aceitos culturalmente
  no Brasil, mantenha consistência de um só termo em todo o produto).
- Mensagens de erro/vazio devem orientar a próxima ação ("Sua sacola está
  vazia. Que tal dar uma olhada nas ofertas do dia?"), nunca soar como culpa
  do usuário.

---

## 8. Responsividade

- Mobile-first: mais de 70% do tráfego deste tipo de loja é mobile.
- Grade de produtos: 2 colunas no mobile, 4–5 no desktop.
- Barra de busca e ícone de sacola sempre visíveis no header mobile, mesmo
  com menu hambúrguer para navegação de categorias.
- Botão de comprar fixo (sticky) no rodapé da tela em páginas de produto no
  mobile.

---

## 9. Instruções para o Claude Code

Ao usar este arquivo para alinhar o projeto:

1. **Varra a documentação e o código de UI existentes** (componentes, temas,
   arquivos de estilo, `README`s de design, Storybook, tokens JSON/CSS) e
   identifique divergências de cor, tipografia, espaçamento e nomenclatura
   de componentes em relação ao especificado aqui.
2. **Atualize os design tokens primeiro** (arquivo de cores/tipografia
   central, ex. `tailwind.config`, `theme.css`, `tokens.json`) antes de tocar
   em componentes individuais, para que a mudança se propague.
3. **Padronize nomenclatura**: use os nomes de token definidos na Seção 2
   (`--brand-pink`, `--sale-red` etc.) em vez de valores hex soltos
   espalhados pelo código.
4. **Não decida silenciosamente**: se encontrar um componente que não tem
   equivalente claro aqui (ex. um modal de newsletter), aplique os princípios
   gerais (Seções 2–4 e 7) e sinalize a decisão tomada em vez de inventar um
   padrão novo sem registrar.
5. **Priorize fidelidade visual real**: se o usuário anexar screenshots do
   site real da Zattini em algum momento, trate-os como fonte de verdade e
   atualize este próprio `design.md` para refletir as correções antes de
   propagar para o resto do código.
6. **Não afirme conformidade de marca 1:1** em nenhum texto/documentação
   gerado — este é um design "inspirado em", não uma cópia exata da Zattini,
   e isso deve ficar claro em qualquer lugar que descreva a origem do
   sistema visual.