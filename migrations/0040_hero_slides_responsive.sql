-- Slides responsivos: imagem mobile + foco + posição/visibilidade do conteúdo.
--
-- Aditivo e idempotente; restrito ao schema `baianas_moda_feminina`. Todos os defaults
-- reproduzem o comportamento atual, sem alterar silenciosamente os slides
-- existentes: imagem atual vira desktop/fallback, foco centralizado,
-- conteúdo à esquerda e título/descrição exibidos.
--
-- - image_mobile: opcional; ausente => usa `image` (fallback).
-- - focal_desktop/focal_mobile: palavra-chave de `object-position`
--   (valores válidos reforçados na camada de aplicação).
-- - content_position: left (atual) | center | right (desktop).
-- - show_title/show_description: opt-out explícito por checkbox.
-- - description: texto novo (slides antigos não têm => nada muda).
--
-- DOWN em 0040_hero_slides_responsive_down.sql.

\set ON_ERROR_STOP on

SET search_path TO baianas_moda_feminina;

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS image_mobile TEXT;

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS focal_desktop VARCHAR(24)
        NOT NULL DEFAULT 'center';

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS focal_mobile VARCHAR(24)
        NOT NULL DEFAULT 'center';

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS content_position VARCHAR(16)
        NOT NULL DEFAULT 'left';

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS description TEXT;

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS show_title BOOLEAN
        NOT NULL DEFAULT TRUE;

ALTER TABLE hero_slides
    ADD COLUMN IF NOT EXISTS show_description BOOLEAN
        NOT NULL DEFAULT TRUE;
