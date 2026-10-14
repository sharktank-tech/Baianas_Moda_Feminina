"""Link do hero slide: adição e edição no admin + imagem clicável.

- Admin aceita caminho interno (ex.: /produtos?categoria=sapatos) ou
  https externa; javascript:/data:/http:/rota inexistente/rota POST →
  None (fail-closed, cai em /produtos). Limite 255 (coluna).
- Imagem do slide vira link (além do botão); botão preservado.
"""
import pytest

from loja.modules.models import HeroSlide


def _post_add(client, **over):
    from loja import db as _db
    HeroSlide.query.delete()
    _db.session.commit()
    data = {
        "image": "https://exemplo.com/desk.jpg", "alt": "Alt",
        "eyebrow": "Novo", "title": "Sapatos",
        "description": "D", "link": "/produtos?categoria=sapatos",
    }
    data.update(over)
    return client.post("/admin/add_hero_slide", data=data,
                       follow_redirects=True)


def _make(db, **over):
    import uuid
    data = {"id": uuid.uuid4(), "image": "https://exemplo.com/d.jpg",
            "title": "T"}
    data.update(over)
    slide = HeroSlide(**data)
    db.session.add(slide)
    db.session.commit()
    return slide


@pytest.mark.parametrize("link,ok", [
    ("/produtos?categoria=sapatos", True),
    ("/produtos", True),
    ("https://instagram.com/pollomodas", True),
    ("javascript:alert(1)", False),
    ("data:text/html,x", False),
    ("http://exemplo.com", False),
    ("/rota-que-nao-existe-xyz", False),
    ("", False),
    ("x" * 256, False),
])
def test_validacao_link(client, db, admin_user, login, link, ok):
    login(admin_user.email)
    _post_add(client, link=link)
    slides = HeroSlide.query.all()
    assert [s.link for s in slides] == [link if ok else None]


def test_admin_adicao_salva_link(client, db, admin_user, login):
    login(admin_user.email)
    resp = _post_add(client)
    assert resp.status_code == 200
    assert HeroSlide.query.first().link == "/produtos?categoria=sapatos"


def test_admin_edicao_salva_e_limpa_link(client, db, admin_user, login):
    login(admin_user.email)
    slide = _make(db, link="/produtos")
    resp = client.post(
        f"/admin/edit_hero_slide/{slide.id}",
        data={"image": slide.image, "title": "T",
              "link": "/produtos?categoria=sapatos"},
        follow_redirects=True,
    )
    assert resp.status_code == 200
    db.session.expire_all()
    assert slide.link == "/produtos?categoria=sapatos"
    client.post(f"/admin/edit_hero_slide/{slide.id}",
                data={"image": slide.image, "title": "T", "link": ""},
                follow_redirects=True)
    db.session.expire_all()
    assert slide.link is None


def test_formularios_expoem_campo_link(client, admin_user, login):
    login(admin_user.email)
    assert 'name="link"' in client.get("/admin/add_hero_slide").get_data(
        as_text=True)
    # cria via rota para reaproveitar o POST válido
    _post_add(client)
    s = HeroSlide.query.first()
    html = client.get(f"/admin/edit_hero_slide/{s.id}").get_data(as_text=True)
    assert 'name="link"' in html
    assert "/produtos?categoria=sapatos" in html


def test_home_imagem_e_botao_apontam_para_link(client, db):
    _make(db, title="Sapatos", link="/produtos?categoria=sapatos")
    html = client.get("/").get_data(as_text=True)
    assert 'class="hero-carousel__link"' in html
    assert 'href="/produtos?categoria=sapatos"' in html
    assert html.count('href="/produtos?categoria=sapatos"') >= 2  # imagem + botão


def test_home_sem_link_cai_em_produtos(client, db):
    _make(db, title="Sem link", link=None)
    html = client.get("/").get_data(as_text=True)
    assert 'href="/produtos"' in html


def test_css_link_block_no_template():
    from pathlib import Path
    index = Path("loja/templates/main/index.html").read_text()
    assert ".hero-carousel__link" in index
