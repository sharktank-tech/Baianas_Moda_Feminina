-- Reverte 0040: remove as colunas responsivas dos slides.
-- Perde imagem mobile, foco, posição/visibilidade e descrições.
-- Slides voltam ao comportamento anterior (imagem única). Restrito a
-- pollomodas; idempotente.

\set ON_ERROR_STOP on

SET search_path TO pollomodas;

ALTER TABLE hero_slides
    DROP COLUMN IF EXISTS show_description,
    DROP COLUMN IF EXISTS show_title,
    DROP COLUMN IF EXISTS description,
    DROP COLUMN IF EXISTS content_position,
    DROP COLUMN IF EXISTS focal_mobile,
    DROP COLUMN IF EXISTS focal_desktop,
    DROP COLUMN IF EXISTS image_mobile;
