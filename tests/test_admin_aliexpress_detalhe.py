"""Renderização segura da descrição no detalhe admin do AliExpress.

Referência: produto 1005011582433257 ("Calças Cargo Femininas"), cuja
descrição chega como HTML puro do fornecedor (detailmodule_html, atributos
slate-*, usemap, larguras fixas) e era exibida escapada (tags visíveis).

Cobre, com HTML sintético fiel ao formato real (sem chamadas de rede):
- descrição formatada renderizada (não escapada) via allowlist;
- payload malicioso neutralizado (sem |safe no HTML bruto);
- allowlist preservada (formatação legítima intacta);
- CSS de contenção para mobile;
- ausência de descrição não gera seção vazia.
"""

from types import SimpleNamespace

import pytest
from flask import render_template


@pytest.fixture()
def req_ctx(app):
    with app.test_request_context("/"):
        yield


def _detail(description, title="Calças Cargo Femininas"):
    return SimpleNamespace(
        product_id="1005011582433257",
        title=title,
        description=description,
        category_id="200003959",
        images=["https://ae01.alicdn.com/kf/Simg.jpg"],
        skus=[],
        currency="BRL",
        available_stock=10,
    )


def _descricao_referencia():
    # Fiel ao formato real armazenado (0.2): wrappers detailmodule, attrs
    # proprietárias, largura fixa, imagens do CDN.
    return (
        '<div class="detailmodule_html">'
        '<div class="detail-desc-decorate-richtext">'
        '<div style="width:790px">'
        '<p><strong>Calça cargo</strong> com <em>bolsos laterais</em>.</p>'
        '<img src="https://ae01.alicdn.com/kf/S7d35e416f91e492b978b9e6d1104f021g.jpg" '
        'usemap="#_sdmap_0" slate-data-type="image" />'
        "<ul><li>Algodão</li><li>Poliéster</li></ul>"
        "</div></div></div>"
    )


def test_detalhe_renderiza_descricao_formatada_nao_escapada(req_ctx):
    html = render_template(
        "admin/aliexpress/detalhe.html", detail=_detail(_descricao_referencia())
    )
    # Tags reais renderizadas (não escapadas como texto).
    assert "&lt;div" not in html
    assert "<strong>Calça cargo</strong>" in html
    assert "<em>bolsos laterais</em>" in html
    assert "<li>Algodão</li>" in html
    assert 'src="https://ae01.alicdn.com/kf/S7d35e416f91e492b978b9e6d1104f021g.jpg"' in html
    # Sujeira proprietária removida, estrutura mantida.
    assert "slate-data-type" not in html
    assert "usemap" not in html
    assert "width:790px" not in html


def test_detalhe_neutraliza_payload_malicioso(req_ctx):
    html = render_template(
        "admin/aliexpress/detalhe.html",
        detail=_detail(
            '<p>Oferta</p><script>alert(1)</script>'
            '<img src="https://ae01.alicdn.com/kf/x.jpg" onerror="alert(2)">'
            '<a href="javascript:alert(3)">clique</a>'
            "<iframe src='https://evil.test'></iframe>"
        ),
    )
    assert "<script" not in html.split('ae-descricao">')[1].split("</div>")[0]
    assert "onerror" not in html.split('ae-descricao">')[1].split("</div>")[0]
    assert "javascript:" not in html.split('ae-descricao">')[1].split("</div>")[0]
    assert "<iframe" not in html.split('ae-descricao">')[1].split("</div>")[0]
    # Conteúdo legítimo sobrevive e a seção existe.
    assert "Oferta" in html
    assert "clique" in html
    assert 'class="text-gray-600 ae-descricao"' in html


def test_detalhe_allowlist_preservada(req_ctx):
    html = render_template(
        "admin/aliexpress/detalhe.html",
        detail=_detail(
            "<p>Texto</p><br><b>n</b><strong>s</strong><i>i</i><em>e</em>"
            "<u>u</u><ul><li>a</li></ul><ol><li>b</li></ol>"
            '<a href="https://exemplo.com">link</a>'
            '<table><tr><th>H</th></tr><tr><td>C</td></tr></table>'
            "<h4>T</h4><span>S</span><div>D</div>"
        ),
    )
    for tag in ("<p>Texto</p>", "<strong>s</strong>", "<li>a</li>",
                'href="https://exemplo.com"', "<td>C</td>", "<h4>T</h4>"):
        assert tag in html, f"allowlist quebrou: {tag}"


def test_detalhe_css_contem_layout(req_ctx):
    from pathlib import Path

    tpl = (
        Path(__file__).resolve().parents[1]
        / "loja/templates/admin/aliexpress/detalhe.html"
    ).read_text(encoding="utf-8")
    assert "ae-descricao" in tpl
    assert "overflow-x" in tpl
    assert "max-width" in tpl


def test_detalhe_sem_descricao_sem_secao(req_ctx):
    html = render_template(
        "admin/aliexpress/detalhe.html", detail=_detail(None)
    )
    assert ">Descrição</h2>" not in html
