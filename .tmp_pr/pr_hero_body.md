## Slides responsivos da homepage (sem trocar imagens)

Refina desktop + mobile mantendo o padrão do carrossel (links, ordem,
ativação, autoplay, teclado, `prefers-reduced-motion`). Só apresentação +
configuração; nenhuma imagem de produção trocada.

- **Modelo + migração 0040** (com down + validador; **não aplicada em
  produção**): `image_mobile` opcional (fallback = desktop), foco
  desktop/mobile, posição do conteúdo, `description` + `show_title`/
  `show_description`. Defaults reproduzem o comportamento atual.
- **Template**: `<picture>` com `source` mobile (media 700px; sem ela,
  desktop serve tudo sem download duplo); foco via classes (CSP sem
  `style` inline); conteúdo left/center/right com gradiente localizado;
  título/descrição condicionais; `aspect-ratio` 21/9 (máx 700px) e 4/5 no
  mobile (máx 78vh) em vez de alturas fixas; preload com `media` por
  dispositivo; primeira imagem `eager`/`high`.
- **Admin**: campos identificados (desktop/mobile, focos, posição,
  visibilidade com hidden+checkbox explícitos), fallback documentado e
  dimensões recomendadas (**desktop 1600×700 21:9, mobile 1080×1350 4:5**);
  títulos/chamadas seguem como texto (artes sem texto/botão).
- **A11y/carga**: labels+`for`, controles com `aria-label` (existentes),
  foco visível, espaço reservado pelo aspect-ratio, sem `contain`.

## Orientação para exportar as artes

Desktop 1600×700 (21:9) com assunto centralizado ou ajustado pelo foco;
mobile 1080×1350 (4:5) recompondo o assunto para vertical, sem textos,
botões, selos ou tarjas incorporados; manter o produto fora das margens
onde o conteúdo pode sobrepor (base no mobile, lateral conforme posição).

## Verificação

- `tests/test_hero_slides_responsive.py`: 12 passed (defaults, sanitize
  fail-closed, admin add/edit incl. opt-out e foco inválido, picture/
  fallback/posição/toggles, título longo, CSS responsivo, validador
  positivo + negativo); estável 3×.
- `tests/test_hero_slides_pg.py`: 2 passed no CI (migração desde legado +
  negativos), registrado no job `pytest-postgres`.
- Suíte completa: **2373 passed**; `git diff --check` limpo.
- Sem merge/deploy; migração não aplicada; imagens de produção intactas.

## Limitação declarada

Sem navegador/screenshots neste ambiente: larguras 360/390/768/1366/1920
validadas por regras CSS + renderização (não equivalem a teste visual).
Roteiro manual: abrir a home nessas larguras com slide longo, sem mobile,
com mobile, com título/descrição ocultos e com cada posição/foco;
confirmar enquadramento, legibilidade sem sobreposição e controles.
