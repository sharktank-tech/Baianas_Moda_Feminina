"""Galeria do produto no desktop: sem esmagamento com muitas imagens.

Causa: `.product-gallery-thumb` é item flex em coluna com `max-height` +
`overflow-y:auto` sem `flex-shrink: 0` — ao estourar a altura, os itens
encolhem (quebrando o aspect-ratio) em vez de rolar.

Regras travadas aqui:
- thumbs nunca comprimem (`flex-shrink: 0`) e têm dimensão consistente;
- principal com `object-fit: contain` (produto inteiro, sem distorção);
- faixa de miniaturas com rolagem no desktop;
- mobile intacto (fileira com `flex: 0 0 72px`).
"""
import re
from pathlib import Path


def _css():
    src = Path("loja/templates/main/produto_individual.html").read_text()
    m = re.search(r"<style[^>]*>(.*?)</style>", src, re.S)
    assert m, "bloco <style> da galeria ausente"
    return m.group(1)


def _rule(css, selector):
    m = re.search(re.escape(selector) + r"\s*\{([^}]*)\}", css)
    assert m, f"regra {selector} ausente"
    return m.group(1)


def _media(css, query):
    i = css.find(query)
    assert i != -1, f"media {query} ausente"
    depth, j, opened = 0, i, False
    while j < len(css):
        if css[j] == "{":
            depth += 1
            opened = True
        elif css[j] == "}":
            depth -= 1
        j += 1
        if opened and depth == 0:
            break
    return css[i:j]


def test_thumbs_nunca_comprimem_no_desktop():
    base = _rule(_css(), ".product-gallery-thumb")
    assert re.search(r"flex-shrink\s*:\s*0", base) or \
        re.search(r"flex\s*:\s*0\s+0\s+", base), \
        "thumbs precisam de flex-shrink: 0 (coluna com max-height)"


def test_thumbs_com_dimensao_consistente():
    base = _rule(_css(), ".product-gallery-thumb")
    assert "aspect-ratio" in base
    assert "width" in base


def test_principal_com_contain():
    main = _rule(_css(), ".product-gallery-main")
    assert re.search(r"object-fit\s*:\s*contain", main)


def test_faixa_com_rolagem_no_desktop():
    strip = _rule(_css(), ".product-gallery-thumbs")
    assert "overflow-y" in strip and "auto" in strip
    assert "max-height" in strip


def test_mobile_preservado():
    mobile = _media(_css(), "@media (max-width: 800px)")
    assert "flex-direction" in mobile and "row" in mobile
    assert re.search(r"flex\s*:\s*0\s+0\s+72px", mobile)
    assert "overflow-x" in mobile


def test_galeria_sem_limite_artificial():
    from loja.services.product_content import build_product_gallery
    from types import SimpleNamespace
    imgs = [SimpleNamespace(url=f"https://exemplo.com/{i}.jpg", position=i)
            for i in range(12)]
    product = SimpleNamespace(images=imgs, image1=None, image2=None,
                              image3=None, image4=None, source="interno",
                              description="", name="P")
    gallery = build_product_gallery(product)
    assert len(gallery) == 12
