"""Navegador real (Playwright/Chromium): galeria do produto no desktop.

Pula localmente sem `playwright`; roda no CI dedicado com banco
descartável e produto sintético de 12 imagens (seed_gallery_product.py).
Somente leitura pública; nenhum carrinho, pagamento ou login.
"""
import os
import subprocess
import sys

import pytest

playwright = pytest.importorskip("playwright.sync_api")

BASE_URL = os.environ.get("BROWSER_BASE_URL", "http://127.0.0.1:5111")
SHOTS = "/tmp/galleryshots"

WIDTHS = [1024, 1280, 1440]
MOBILE = {"width": 390, "height": 844}


@pytest.fixture(scope="module")
def browser():
    os.makedirs(SHOTS, exist_ok=True)
    with playwright.sync_playwright() as runner:
        instance = runner.chromium.launch(args=["--no-sandbox"])
        yield instance
        instance.close()


@pytest.fixture()
def page(browser, request):
    param = getattr(request, "param", None)
    if param == "mobile" or os.environ.get("BROWSER_MOBILE") == "1":
        viewport, tag = MOBILE, "mobile"
    elif isinstance(param, int):
        viewport, tag = {"width": param, "height": 900}, f"w{param}"
    else:
        viewport, tag = {"width": 1280, "height": 900}, "desktop"
    context = browser.new_context(viewport=viewport)
    tab = context.new_page()
    tab._shot_tag = tag
    tab._shot_w = viewport["width"]
    yield tab
    context.close()


def seed_product():
    proc = subprocess.run(
        [sys.executable, "tests/browser/seed_gallery_product.py"],
        cwd=os.getcwd(), capture_output=True, text=True, timeout=120)
    assert proc.returncode == 0, proc.stderr[-2000:]
    return proc.stdout.strip().split()[-1]


def shot(page, name):
    page.screenshot(path=f"{SHOTS}/{name}-{page._shot_tag}.png",
                    clip={"x": 0, "y": 0, "width": page._shot_w, "height": 900})


def thumb_boxes(page):
    return [loc.bounding_box()
            for loc in page.locator(".product-gallery-thumb").all()]


@pytest.mark.parametrize("page", WIDTHS, indirect=True)
def test_thumbs_sem_esmagamento(page):
    """Todas as miniaturas com a mesma altura e proporção 4/5."""
    pid = seed_product()
    page.goto(f"{BASE_URL}/produtos/{pid}")
    page.wait_for_selector(".product-gallery-thumb")
    if page._shot_w == 1280:
        shot(page, "galeria-muitas")
    boxes = thumb_boxes(page)
    assert len(boxes) == 12
    heights = {round(b["height"]) for b in boxes if b}
    assert len(heights) == 1, f"alturas divergentes: {sorted(heights)}"
    for b in boxes:
        assert abs(b["height"] - b["width"] * 5 / 4) <= 2, b


def test_principal_contem_sem_rolagem_horizontal(page):
    pid = seed_product()
    page.goto(f"{BASE_URL}/produtos/{pid}")
    page.wait_for_selector("#productGalleryMain")
    fit = page.evaluate(
        "getComputedStyle(document.querySelector('#productGalleryMain'))"
        ".objectFit")
    assert fit == "contain"
    overflow = page.evaluate(
        "document.documentElement.scrollWidth - window.innerWidth")
    assert overflow <= 1, f"rolagem horizontal: {overflow}px"
    main = page.locator("#productGalleryMain").bounding_box()
    assert main and main["height"] > 200


def test_selecao_atualiza_principal(page):
    pid = seed_product()
    page.goto(f"{BASE_URL}/produtos/{pid}")
    page.wait_for_selector(".product-gallery-thumb")
    before = page.get_attribute("#productGalleryMain", "src")
    page.locator(".product-gallery-thumb").nth(5).click()
    page.wait_for_function(
        "() => document.querySelector('#productGalleryCounter')"
        ".textContent.trim() === '6 / 12'",
        timeout=8000)
    after = page.get_attribute("#productGalleryMain", "src")
    assert before != after
    active = page.locator(".product-gallery-thumb.is-active").count()
    assert active == 1


def test_zoom_abre_lightbox(page):
    pid = seed_product()
    page.goto(f"{BASE_URL}/produtos/{pid}")
    page.wait_for_selector("[data-gallery-zoom]")
    page.click("[data-gallery-zoom]")
    page.wait_for_selector("#productLightbox:not([hidden])", timeout=8000)
    assert page.locator("#productLightboxImage").is_visible()
    page.click("[data-lightbox-close]")
    page.wait_for_selector("#productLightbox[hidden]", timeout=8000)


@pytest.mark.parametrize("page", ["mobile"], indirect=True)
def test_mobile_fileira_com_rolagem(page):
    pid = seed_product()
    page.goto(f"{BASE_URL}/produtos/{pid}")
    page.wait_for_selector(".product-gallery-thumb")
    shot(page, "galeria-muitas")
    boxes = thumb_boxes(page)
    assert len(boxes) == 12
    assert len({round(b["height"]) for b in boxes if b}) == 1
