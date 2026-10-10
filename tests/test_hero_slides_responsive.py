"""Slides responsivos da homepage (mobile + foco + posição/visibilidade).

Imagem atual vira desktop/fallback; mobile opcional via <picture>;
foco e posição com fallbacks fail-closed; título/descrição com opt-out
explícito. Sem alterar silenciosamente slides existentes.
"""
import re
import uuid

import pytest

from loja.modules.models import HeroSlide
from loja.modules.services import (
    HERO_CONTENT_POSITIONS,
    HERO_FOCAL_POSITIONS,
    sanitize_hero_slide,
)


def _slide(db, **over):
    data = dict(
        id=uuid.uuid4(), image="https://exemplo.com/desk.jpg",
        alt="Alt", eyebrow="Novo", title="Título",
    )
    data.update(over)
    slide = HeroSlide(**data)
    db.session.add(slide)
    db.session.commit()
    return slide


@pytest.fixture(autouse=True)
def _clean_hero_slides(db):
    yield
    HeroSlide.query.delete()
    db.session.commit()


# --------------------------------------------------------------------------
# 1) Modelo: defaults reproduzem o comportamento anterior
# --------------------------------------------------------------------------

def test_modelo_defaults_preservam_comportamento(db):
    slide = _slide(db)
    assert slide.image_mobile is None
    assert slide.focal_desktop == "center"
    assert slide.focal_mobile == "center"
    assert slide.content_position == "left"
    assert slide.description is None
    assert slide.show_title is True
    assert slide.show_description is True


def test_sanitize_falha_fechado_para_valores_invalidos():
    from types import SimpleNamespace as NS

    snap = sanitize_hero_slide(NS(
        image="d.jpg", image_mobile=None, alt=None, eyebrow=None,
        title="T", description="D", link=None,
        focal_desktop="canto-invalido", focal_mobile=None,
        content_position="diagonal", show_title=True,
        show_description=True,
    ))
    assert snap["focal_desktop"] == "center"
    assert snap["focal_mobile"] == "center"
    assert snap["content_position"] == "left"


def test_sanitize_respeita_opt_out_e_slide_antigo():
    from types import SimpleNamespace as NS

    off = sanitize_hero_slide(NS(
        image="d.jpg", image_mobile="m.jpg", alt=None, eyebrow=None,
        title="T", description="D", link=None,
        focal_desktop="top", focal_mobile="bottom",
        content_position="center", show_title=False,
        show_description=False,
    ))
    assert off["title"] is None and off["description"] is None
    assert off["image_mobile"] == "m.jpg"
    assert (off["focal_desktop"], off["focal_mobile"],
            off["content_position"]) == ("top", "bottom", "center")

    # Slide antigo (sem atributos novos): tudo visível, padrões atuais.
    old = sanitize_hero_slide(NS(image="d.jpg", title="T"))
    assert old["title"] == "T"
    assert old["image_mobile"] is None
    assert old["content_position"] == "left"


# --------------------------------------------------------------------------
# 2) Admin: persistência incl. toggles e validação
# --------------------------------------------------------------------------

def _post_slide(client, **over):
    data = {
        "image": "https://exemplo.com/desk.jpg", "alt": "Alt",
        "eyebrow": "Novo", "title": "Título",
        "image_mobile": "https://exemplo.com/mob.jpg",
        "focal_desktop": "top", "focal_mobile": "bottom",
        "content_position": "center", "description": "Descrição",
        "show_title": "1", "show_description": "1",
    }
    data.update(over)
    return client.post("/admin/add_hero_slide", data=data,
                       follow_redirects=True)


def test_admin_cria_com_todos_os_campos(client, db, admin_user, login):
    login(admin_user.email)
    resp = _post_slide(client)
    assert resp.status_code == 200
    slide = HeroSlide.query.first()
    assert slide.image_mobile == "https://exemplo.com/mob.jpg"
    assert (slide.focal_desktop, slide.focal_mobile,
            slide.content_position) == ("top", "bottom", "center")
    assert slide.description == "Descrição"
    assert slide.show_title is True and slide.show_description is True


def test_admin_opt_out_explicito_e_foco_invalido(client, db, admin_user,
                                                 login):
    login(admin_user.email)
    _post_slide(client, **{
        "image_mobile": "", "focal_desktop": "canto",
        "content_position": "diagonal",
        "show_title": "0", "show_description": "0",
    })
    slide = HeroSlide.query.first()
    assert slide.image_mobile is None
    assert slide.focal_desktop == "center"
    assert slide.content_position == "left"
    assert slide.show_title is False and slide.show_description is False


def test_admin_editar_preserva_e_atualiza(client, db, admin_user, login):
    login(admin_user.email)
    slide = _slide(db)
    resp = client.post(
        f"/admin/edit_hero_slide/{slide.id}",
        data={
            "image": slide.image, "alt": "Alt", "eyebrow": "E",
            "title": "T", "image_mobile": "https://exemplo.com/m.jpg",
            "focal_desktop": "left", "focal_mobile": "right",
            "content_position": "right", "description": "D",
            "show_title": "1", "show_description": "0",
        },
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.expire_all()
    assert slide.image_mobile == "https://exemplo.com/m.jpg"
    assert slide.content_position == "right"
    assert slide.show_title is True and slide.show_description is False


# --------------------------------------------------------------------------
# 3) Template: picture, fallback, posições, toggles
# --------------------------------------------------------------------------

def _render_home(client, db, admin_user, slides):
    for data in slides:
        _slide(db, **data)
    return client.get("/").get_data(as_text=True)


def test_picture_mobile_e_fallback(client, db, admin_user):
    _slide(db, image_mobile="https://exemplo.com/mob.jpg")
    _slide(db, title="Só desktop")
    html = client.get("/").get_data(as_text=True)
    assert html.count("<picture>") == 2
    assert 'media="(max-width: 700px)"' in html
    assert "https://exemplo.com/mob.jpg" in html
    # Fallback: sem mobile, nenhum <source> para o 2º slide.
    assert html.count("<source") == 1


def test_posicao_foco_e_toggles(client, db, admin_user):
    _slide(db, focal_desktop="top", focal_mobile="bottom",
           content_position="center", show_title=False,
           description="Desc visível", show_description=True,
           title="Escondido")
    html = client.get("/").get_data(as_text=True)
    assert "hero-focal-d-top" in html
    assert "hero-focal-m-bottom" in html
    assert 'style="' not in html  # CSP: sem estilos inline
    assert "hero-carousel__slide--content-center" in html
    assert "Escondido" not in html
    assert "Desc visível" in html
    assert 'class="hero-carousel__description"' in html


def test_titulo_longo_e_slide_padrao(client, db, admin_user):
    _slide(db, title="Vista o estilo que combina com você em todos os dias")
    html = client.get("/").get_data(as_text=True)
    assert "Vista o estilo" in html
    assert "hero-carousel__slide--content-left" in html


def test_css_responsivo_presente():
    src = open("loja/templates/main/index.html", encoding="utf-8").read()
    assert "aspect-ratio: 21 / 9" in src
    assert "aspect-ratio: 4 / 5" in src
    assert "object-position: var(--hero-focal-mobile" in src
    assert "prefers-reduced-motion" in src
    assert "hero-carousel__slide--content-right" in src


# --------------------------------------------------------------------------
# 4) Migração 0040: validador (positivo + negativo, sem banco real)
# --------------------------------------------------------------------------

def test_validate_0040_aceita_schema_correto():
    from migrations.validate_0040 import validate_schema

    cols = {
        "image_mobile": ("text", True, None),
        "focal_desktop": ("character varying", False,
                          "'center'::character varying"),
        "focal_mobile": ("character varying", False,
                         "'center'::character varying"),
        "content_position": ("character varying", False,
                             "'left'::character varying"),
        "description": ("text", True, None),
        "show_title": ("boolean", False, "true"),
        "show_description": ("boolean", False, "true"),
    }

    class FakeCursor:
        def execute(self, query, params=None):
            self.last = query
            if "information_schema" in query:
                self.rows = [
                    (name, dtype, nullable, default)
                    for name, (dtype, nullable, default) in cols.items()
                ]
            elif "to_regclass" in query:
                self.rows = [(None,)]

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    assert validate_schema(FakeConn()) == []


def test_validate_0040_recusa_estrutura_incorreta():
    from migrations.validate_0040 import validate_schema

    class FakeCursor:
        def execute(self, query, params=None):
            if "information_schema" in query:
                # Falhas: sem description, tipo errado, default ausente.
                self.rows = [
                    ("image_mobile", "text", True, None),
                    ("focal_desktop", "text", False, None),
                    ("focal_mobile", "character varying", False,
                     "'center'::character varying"),
                    ("content_position", "character varying", False,
                     "'left'::character varying"),
                    ("show_title", "boolean", False, "true"),
                    ("show_description", "boolean", False, "true"),
                ]
            else:
                self.rows = [(None,)]

        def fetchall(self):
            return self.rows

        def fetchone(self):
            return self.rows[0]

    class FakeConn:
        def cursor(self):
            return FakeCursor()

    errors = validate_schema(FakeConn())
    assert len(errors) >= 2, errors
    assert any("description" in e for e in errors)
    assert any("focal_desktop" in e for e in errors)
