"""Consulta direta por ID/URL e importação de candidato com despacho BR.

Contexto: a API oficial de busca (`aliexpress.ds.text.search`) não oferece
filtro por país de despacho, e o detalhe (`aliexpress.ds.product.get`) não
devolve origem de despacho em nenhum campo documentado. Para avaliar um
anúncio específico, o painel oferece a consulta direta por ID/URL
(`POST /buscar-id`), que usa somente o caminho existente de detalhe por ID.

Cobre, sem nenhuma chamada real de rede:
- validação estrita de ID/URL (só hosts AliExpress e caminho `/item/`);
- a rota de consulta direta (sucesso, inválido, ausente, erro sanitizado);
- busca geral preservada e sem parâmetros de despacho;
- importação idempotente de um candidato sintético com 2 variantes:
  identidade/SKUs/estoque/preço/moeda/atributos preservados, origem
  `unknown` até confirmação manual, e confirmação de UMA variante não
  marca as demais como nacionais (`mixed`, nunca `brazil`).
"""

from __future__ import annotations

from decimal import Decimal

from loja.integrations.aliexpress.errors import AliExpressAPIError
from loja.integrations.aliexpress.models import (
    AliExpressProductDetail,
    AliExpressProductSku,
)
from loja.integrations.aliexpress.product import parse_aliexpress_product_ref
from tests.test_aliexpress_admin_catalog import (  # noqa: F401  (fixtures)
    PREFIX,
    PRODUCT_ID,
    TITLE,
    FakeAliExpressClient,
    admin_catalog,  # noqa: F401
    ae_env,  # noqa: F401
    catalog,  # noqa: F401
    fake_client,  # noqa: F401
    make_detail,
    reference_quote_table,  # noqa: F401
)

import pytest

from loja.modules.models import (
    AliExpressSyncAttempt,
    Product,
    ProductSync,
    ProductVariant,
    Supplier,
    SupplierProduct,
    SupplierSkuShippingOrigin,
)


@pytest.fixture(autouse=True)
def _clean_br_candidate_data(db):
    """Isola os rascunhos/vínculos/evidências criados pelos testes deste módulo."""
    product_ids = db.session.query(Product.id).filter_by(source="aliexpress")
    supplier_ids = db.session.query(Supplier.id).filter_by(channel_type="aliexpress")
    SupplierSkuShippingOrigin.query.delete(synchronize_session=False)
    AliExpressSyncAttempt.query.delete(synchronize_session=False)
    ProductSync.query.filter(ProductSync.product_id.in_(product_ids)).delete(
        synchronize_session=False
    )
    ProductVariant.query.filter(ProductVariant.product_id.in_(product_ids)).delete(
        synchronize_session=False
    )
    SupplierProduct.query.filter(SupplierProduct.supplier_id.in_(supplier_ids)).delete(
        synchronize_session=False
    )
    Product.query.filter(Product.id.in_(product_ids)).delete(synchronize_session=False)
    Supplier.query.filter(Supplier.id.in_(supplier_ids)).delete(synchronize_session=False)
    db.session.commit()
    yield
    db.session.rollback()

BR_ID = "1005013003211750"
BR_URL = "https://pt.aliexpress.com/item/1005013003211750.html"


# --------------------------------------------------------------------------
# 1) Validação de ID/URL (função pura, sem rede)
# --------------------------------------------------------------------------

def test_aceita_id_numerico():
    assert parse_aliexpress_product_ref(BR_ID) == BR_ID
    assert parse_aliexpress_product_ref("  %s  " % BR_ID) == BR_ID
    assert parse_aliexpress_product_ref("32982857990") == "32982857990"


def test_aceita_url_de_anuncio_aliexpress():
    assert parse_aliexpress_product_ref(BR_URL) == BR_ID
    assert (
        parse_aliexpress_product_ref(
            "https://www.aliexpress.com/item/%s.html?src=ibdm&sk=abc" % BR_ID
        )
        == BR_ID
    )
    assert (
        parse_aliexpress_product_ref(
            "http://m.aliexpress.com/item/%s.htm" % BR_ID
        )
        == BR_ID
    )
    assert (
        parse_aliexpress_product_ref(
            "https://aliexpress.us/item/%s.html" % BR_ID
        )
        == BR_ID
    )


def test_recusa_nao_ids():
    assert parse_aliexpress_product_ref(None) is None
    assert parse_aliexpress_product_ref("") is None
    assert parse_aliexpress_product_ref("   ") is None
    assert parse_aliexpress_product_ref("vestido floral") is None
    assert parse_aliexpress_product_ref("69.85") is None
    assert parse_aliexpress_product_ref("R$ 69,85") is None
    assert parse_aliexpress_product_ref("1234567") is None  # curto demais
    assert parse_aliexpress_product_ref("1" * 21) is None  # longo demais


def test_recusa_urls_fora_do_padrao():
    # Host fora da AliExpress: nunca buscar nada a partir daqui.
    assert (
        parse_aliexpress_product_ref(
            "https://evil-aliexpress.com/item/%s.html" % BR_ID
        )
        is None
    )
    assert (
        parse_aliexpress_product_ref(
            "https://pt.aliexpress.com.evil.com/item/%s.html" % BR_ID
        )
        is None
    )
    assert (
        parse_aliexpress_product_ref(
            "https://exemplo.com/loja/%s" % BR_ID
        )
        is None
    )
    # Caminhos que não são anúncio (busca, loja, categoria).
    assert (
        parse_aliexpress_product_ref(
            "https://pt.aliexpress.com/w/wholesale-vestido.html"
        )
        is None
    )
    assert (
        parse_aliexpress_product_ref(
            "https://pt.aliexpress.com/store/12345.html"
        )
        is None
    )
    # ID não numérico no caminho e esquema inseguro.
    assert (
        parse_aliexpress_product_ref(
            "https://pt.aliexpress.com/item/vestido-floral.html"
        )
        is None
    )
    assert (
        parse_aliexpress_product_ref(
            "ftp://pt.aliexpress.com/item/%s.html" % BR_ID
        )
        is None
    )
    assert (
        parse_aliexpress_product_ref(
            "javascript:alert(%s)" % BR_ID
        )
        is None
    )
    # Host sem esquema não é URL válida (use o ID avulso).
    assert (
        parse_aliexpress_product_ref(
            "pt.aliexpress.com/item/%s.html" % BR_ID
        )
        is None
    )


# --------------------------------------------------------------------------
# 2) Rota POST /buscar-id
# --------------------------------------------------------------------------

def test_buscar_id_por_numero_renderiza_detalhe(admin_catalog):
    resp = admin_catalog.post(
        f"{PREFIX}/buscar-id", data={"ref": PRODUCT_ID}
    )
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert TITLE in body
    assert PRODUCT_ID in body
    assert "Importar como rascunho" in body
    assert FakeAliExpressClient.calls[-1] == {
        "op": "get_product",
        "product_id": PRODUCT_ID,
    }


def test_buscar_id_por_url_extracao_e_detalhe(admin_catalog):
    FakeAliExpressClient.details[BR_ID] = make_detail(product_id=BR_ID)
    resp = admin_catalog.post(f"{PREFIX}/buscar-id", data={"ref": BR_URL})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BR_ID in body
    assert FakeAliExpressClient.calls[-1] == {
        "op": "get_product",
        "product_id": BR_ID,
    }


def test_buscar_id_do_piloto_abre_detalhe_exato(admin_catalog):
    """1005010478234256 (piloto): consulta por ID abre o detalhe com as
    3 variações e seus IDs — o mesmo caminho do navegador, sem importar."""
    FakeAliExpressClient.details[BR_ID] = _br_candidate_detail()
    resp = admin_catalog.post(f"{PREFIX}/buscar-id", data={"ref": BR_ID})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert BR_ID in body
    assert "Variações (2)" in body
    assert "%s-M-AZUL" % BR_ID in body
    assert "%s-G-VERMELHO" % BR_ID in body
    assert "Importar como rascunho" in body


def test_buscar_id_invalido_volta_ao_catalogo_sem_chamar_api(admin_catalog):
    before = list(FakeAliExpressClient.calls)
    resp = admin_catalog.post(
        f"{PREFIX}/buscar-id",
        data={"ref": "loja física no centro"},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "ID numérico ou uma URL" in body
    assert FakeAliExpressClient.calls == before


def test_buscar_id_ausente_avisa_sem_chamar_importacao(admin_catalog):
    resp = admin_catalog.post(
        f"{PREFIX}/buscar-id",
        data={"ref": "1005000000000000"},
        follow_redirects=True,
    )
    assert "não encontrado no catálogo do fornecedor" in resp.get_data(
        as_text=True
    )


def test_buscar_id_erro_da_api_e_sanitizado(admin_catalog):
    FakeAliExpressClient.detail_error = AliExpressAPIError(
        "falha simulada",
        code="40001",
        sub_code="isv.invalid",
        raw={"app_secret": "SEGREDO", "sign": "ASSINATURA"},
    )
    resp = admin_catalog.post(
        f"{PREFIX}/buscar-id",
        data={"ref": PRODUCT_ID},
        follow_redirects=True,
    )
    body = resp.get_data(as_text=True)
    assert "falha simulada" in body
    assert "SEGREDO" not in body
    assert "ASSINATURA" not in body


def test_catalogo_expoe_consulta_por_id_e_nota_de_limitacao(admin_catalog):
    """A limitação (sem filtro de despacho) é comunicada na interface."""
    resp = admin_catalog.get(f"{PREFIX}/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert 'name="ref"' in body
    assert "não tem filtro por país de despacho" in body
    assert "origem brasileira só vale com evidência por variante" in body
    # Consulta por ID é a primeira ação do catálogo, antes da busca geral
    # e da tabela de importados: facilmente acessível no topo.
    assert body.index("/buscar-id") < body.index('/buscar"')
    assert body.index("/buscar-id") < body.index("Produtos importados")


def _form_segment(body, action_suffix):
    """Segmento <form ...>...</form> da action indicada (único).

    O sufixo casa até a aspa de fechamento: "/buscar" não confunde com
    "/buscar-id".
    """
    import re

    pattern = re.compile(
        r'<form\b[^>]*action="[^"]*%s"[^>]*>(.*?)</form>' % action_suffix,
        re.S,
    )
    matches = pattern.findall(body)
    assert len(matches) == 1, action_suffix
    return matches[0]


def test_botao_consultar_anuncio_pertence_so_ao_form_buscar_id(admin_catalog):
    """Botão visível e exclusivo: só o form /buscar-id o contém."""
    body = admin_catalog.get(f"{PREFIX}/").get_data(as_text=True)
    lookup = _form_segment(body, "/buscar-id")
    assert 'type="submit"' in lookup
    assert "Consultar anúncio" in lookup
    search = _form_segment(body, '/buscar')
    assert "Consultar anúncio" not in search


def test_enter_no_campo_id_envia_o_proprio_formulario(admin_catalog):
    """Cada campo está dentro do seu form com submit próprio (Enter envia o
    form que contém o campo focado, sem JS e sem cruzar formulários)."""
    body = admin_catalog.get(f"{PREFIX}/").get_data(as_text=True)
    lookup = _form_segment(body, "/buscar-id")
    assert 'name="ref"' in lookup
    search = _form_segment(body, '/buscar')
    assert 'name="q"' in search
    assert 'name="ref"' not in search


def test_botao_usa_so_classes_existentes_no_css_compilado(admin_catalog):
    """Regressão do botão invisível: bg-gray-700 não existe no tailwind.css
    compilado (purgado). O botão só pode usar classes presentes no build."""
    import re
    from pathlib import Path

    body = admin_catalog.get(f"{PREFIX}/").get_data(as_text=True)
    lookup = _form_segment(body, "/buscar-id")
    btn = re.search(r"<button\b[^>]*class=\"([^\"]+)\"", lookup)
    assert btn, "botão com class no form /buscar-id"
    css = Path("public/static/styles/tailwind.css").read_text()
    for cls in btn.group(1).split():
        selector = cls.replace("hover:", r"hover\:") if ":" in cls else "." + cls
        if ":" in cls:
            assert selector in css, cls
        else:
            assert ("." + cls) in css, cls
    assert "bg-gray-700" not in lookup


# --------------------------------------------------------------------------
# 3) Resumo sanitizado para o registro live (sem rede, detalhe sintético)
# --------------------------------------------------------------------------

def test_summarize_registra_produto_e_skus_sem_raw_ou_segredos():
    from loja.integrations.aliexpress.product import summarize_product_detail

    summary = summarize_product_detail(_br_candidate_detail())
    assert summary["product_id"] == BR_ID
    assert "Vestido" in (summary["title"] or "")
    assert summary["currency"] == "BRL"
    assert len(summary["skus"]) == 2
    first = summary["skus"][0]
    assert first["sku_id"] == "%s-M-AZUL" % BR_ID
    assert first["available_stock"] == 7
    assert first["sale_price"] == "69.85"
    assert {"name": "Tamanho", "value": "M"} in first["attributes"]
    assert "raw" not in summary
    assert all("raw" not in sku for sku in summary["skus"])
    dumped = str(summary)
    assert "SEGREDO" not in dumped


def test_format_candidate_error_nunca_inclui_raw():
    from loja.integrations.aliexpress.product import format_candidate_error

    formatted = format_candidate_error(
        AliExpressAPIError(
            "falha simulada",
            code="40001",
            sub_code="isv.invalid",
            raw={"app_secret": "SEGREDO", "sign": "ASSINATURA"},
        )
    )
    assert formatted == {
        "type": "AliExpressAPIError",
        "code": "40001",
        "sub_code": "isv.invalid",
        "message": "falha simulada",
    }

def test_catalogo_sem_config_explica_em_vez_de_ocultar_formularios(
    client, admin_user, login, fake_client, monkeypatch
):
    """Preview sem env: sem sessão possível, a página explica (não some)."""
    monkeypatch.delenv("ALIEXPRESS_ADMIN_SESSION_KEY", raising=False)
    login(admin_user.email)
    resp = client.get(f"{PREFIX}/")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "não configurada neste ambiente" in body
    # Sem sessão não há formulários que funcionem — e a tabela de
    # importados (dados existentes) continua visível.
    assert "/buscar-id" not in body
    assert "Produtos importados" in body


# --------------------------------------------------------------------------
# 4) Busca geral preservada, sem filtro de despacho
# --------------------------------------------------------------------------

def test_busca_geral_nao_envia_filtro_de_despacho():
    from loja.integrations.aliexpress.search import build_search_params

    params = build_search_params(
        keyword="vestido floral",
        locale="en_US",
        country_code="BR",
        currency="BRL",
        category_id=None,
        sort_by="orders,desc",
        page_index=1,
        page_size=20,
        search_extend=None,
        selection_name=None,
    )
    # countryCode é destino/moeda local (comprador BR), nunca prova de
    # despacho; nenhum parâmetro de origem de envio existe aqui.
    assert params["countryCode"] == "BR"
    lowered = {str(k).lower() for k in params}
    assert not {
        k for k in lowered if "ship" in k or "dispatch" in k or "origin" in k
    }
    assert set(params) == {
        "local",
        "countryCode",
        "currency",
        "sortBy",
        "pageIndex",
        "pageSize",
        "keyWord",
    }


# --------------------------------------------------------------------------
# 6) Detalhe exibe IDs DS e dimensões por variante (mesma resposta)
# --------------------------------------------------------------------------

def test_detalhe_exibe_ids_e_dimensoes_por_variante(admin_catalog):
    """Cada linha amarra sku_id, cor, tamanho e sku_property_ids."""
    import re

    FakeAliExpressClient.details[BR_ID] = _br_candidate_detail()
    resp = admin_catalog.get(f"{PREFIX}/{BR_ID}/detalhe")
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert "IDs atributo/valor" in body

    def row_for(sku_id):
        segs = [s.split("</tr>")[0] for s in body.split("<tr") if sku_id in s]
        assert len(segs) == 1, sku_id
        return segs[0]

    azul = row_for("%s-M-AZUL" % BR_ID)
    assert "Azul" in azul
    assert re.search(r">\s*M\s*<", azul)
    assert "Tamanho (14) = M (771)" in azul
    assert "Cor (10) = Azul (19)" in azul
    assert "10:19;14:771" in azul
    assert "69,85" in azul and "79,90" not in azul
    assert "Vermelho" not in azul
    assert "BRL" in azul

    vermelho = row_for("%s-G-VERMELHO" % BR_ID)
    assert "Vermelho" in vermelho
    assert re.search(r">\s*G\s*<", vermelho)
    assert "Tamanho (14) = G (772)" in vermelho
    assert "Cor (10) = Vermelho (20)" in vermelho
    assert "10:20;14:772" in vermelho
    assert "10:19;14:771" not in vermelho
    assert "79,90" in vermelho and "69,85" not in vermelho
    assert "Azul" not in vermelho


def test_sku_id_e_sku_attr_nunca_se_confunden_e_ausente_e_explicito(admin_catalog):
    """sku_id, combinação e IDs vão em células separadas, sem substituição.

    SKU X tem id numérico distinto da combinação; SKU Y não tem id — a
    combinação aparece mesmo assim, e a célula do ID diz "Não fornecido"
    em vez de repetir a combinação.
    """
    import re

    def sku(color, size, pid_c, vid_c, pid_t, vid_t, sku_id):
        def attr(name, value, pid, vid):
            return {
                "name": name,
                "value": value,
                "sku_property_id": pid,
                "property_value_id": vid,
            }

        return AliExpressProductSku(
            sku_id=sku_id,
            sale_price=Decimal("69.85"),
            original_price=Decimal("69.85"),
            currency="BRL",
            available_stock=7,
            attributes=[
                attr("Tamanho", size, pid_t, vid_t),
                attr("Cor", color, pid_c, vid_c),
            ],
        )

    detail = _br_candidate_detail()
    detail.skus = [
        sku("Azul", "M", "10", "20", "14", "772", "1005000000001"),
        sku("Vermelho", "G", "10", "21", "14", "773", None),
    ]
    FakeAliExpressClient.details[BR_ID] = detail

    body = admin_catalog.get(f"{PREFIX}/{BR_ID}/detalhe").get_data(as_text=True)

    def cells_for(marker):
        segs = [
            s.split("</tr>")[0] for s in body.split("<tr") if marker in s
        ]
        assert len(segs) == 1, marker
        return [
            re.sub(r"\s+", " ", t).strip()
            for t in re.findall(r"<td\b[^>]*>(.*?)</td>", segs[0], re.S)
        ]

    com_id = cells_for("1005000000001")
    assert com_id[0] == "1005000000001"  # SKU (ID DS)
    assert com_id[1] == "10:20;14:772"  # Combinação (sku_attr)
    assert com_id[0] != com_id[1]

    sem_id = cells_for("10:21;14:773")
    assert sem_id[0] == "Não fornecido"
    assert sem_id[1] == "10:21;14:773"

    # ID do produto junto à tabela (identifica exportações).
    assert ("Produto %s" % BR_ID) in body


def test_detalhe_sem_ids_mostra_traco(admin_catalog):
    FakeAliExpressClient.details[PRODUCT_ID] = make_detail()
    body = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe").get_data(
        as_text=True
    )
    assert "%s-1" % PRODUCT_ID in body  # sku_id exibido mesmo sem IDs
    assert "IDs atributo/valor" in body


def test_detalhe_exige_login_admin(client):
    resp = client.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith("/login")


def test_detalhe_exige_sessao_aliexpress(client, admin_user, login, ae_env, fake_client):
    login(admin_user.email)
    resp = client.get(f"{PREFIX}/{PRODUCT_ID}/detalhe")
    assert resp.status_code == 302
    assert resp.headers["Location"].endswith(PREFIX + "/")

# --------------------------------------------------------------------------
# 7) Runner de comparação on/off (offline, cliente fake, sem rede)
# --------------------------------------------------------------------------

def _load_query_module():
    import sys
    from pathlib import Path

    scripts = str(Path("scripts").resolve())
    if scripts not in sys.path:
        sys.path.insert(0, scripts)
    import query_aliexpress_candidates as qc
    return qc


class _FakeQueryClient:
    """Fake mínimo: registra flags e devolve detalhe ou erro por (id, flag)."""

    def __init__(self, detail, errors=()):
        self.detail = detail
        self.errors = set(errors)
        self.calls = []

    def get_product(self, *, product_id, remove_personal_benefit=True, **kwargs):
        self.calls.append((product_id, remove_personal_benefit))
        if (product_id, remove_personal_benefit) in self.errors:
            raise AliExpressAPIError("falha simulada", code="40001")
        return self.detail


def test_compare_benefit_registra_flag_e_mercado_por_chamada():
    qc = _load_query_module()
    client = _FakeQueryClient(_br_candidate_detail())
    records, failed = qc.query_candidates(client, compare_benefit=True)

    assert failed == 0
    by_flag = {}
    for record in records:
        assert record["ok"] is True
        assert record["market"] == {
            "ship_to_country": "BR",
            "target_currency": "BRL",
            "target_language": "pt",
        }
        by_flag.setdefault(record["product_id"], []).append(
            record["remove_personal_benefit"]
        )
    # Comparação on/off só nos dois produtos; demais, uma vez ligada.
    assert sorted(by_flag["1005010478234256"]) == [False, True]
    assert sorted(by_flag["1005010502074833"]) == [False, True]
    assert by_flag["1005013003211750"] == [True]
    assert client.calls.count(("1005010478234256", True)) == 1
    assert client.calls.count(("1005010478234256", False)) == 1


def test_compare_benefit_isola_erro_por_flag():
    """Erro numa flag não contamina a outra: saídas iguais com flags
    diferentes são evidência de parâmetro ignorado — nunca sucesso."""
    qc = _load_query_module()
    client = _FakeQueryClient(
        _br_candidate_detail(), errors={("1005010478234256", False)}
    )
    records, failed = qc.query_candidates(client, compare_benefit=True)

    assert failed == 1
    flagged = {
        (r["product_id"], r["remove_personal_benefit"]): r for r in records
    }
    bad = flagged[("1005010478234256", False)]
    assert bad["ok"] is False
    assert bad["error"]["code"] == "40001"
    good = flagged[("1005010478234256", True)]
    assert good["ok"] is True
    assert good["detail"]["product_id"] == BR_ID


def test_sem_comparacao_chama_uma_vez_ligado():
    qc = _load_query_module()
    client = _FakeQueryClient(_br_candidate_detail())
    records, failed = qc.query_candidates(client, compare_benefit=False)

    assert failed == 0
    assert len(records) == len(qc.CANDIDATES)
    assert {r["remove_personal_benefit"] for r in records} == {True}

# --------------------------------------------------------------------------
# 5) Candidato sintético com 2 variantes: importação, origem e confirmação
# --------------------------------------------------------------------------

def _br_candidate_detail(product_id=BR_ID):
    """Rascunho sintético no formato do anúncio BR observado (2 variantes).

    Os valores espelham o anúncio público listado na investigação
    (vestido, ~R$ 69,85, 2 opções de tamanho/cor); a origem NÃO vem na
    resposta — entra só por confirmação manual com evidência.
    """
    skus = [
        AliExpressProductSku(
            sku_id="%s-M-AZUL" % product_id,
            sale_price=Decimal("69.85"),
            original_price=Decimal("69.85"),
            currency="BRL",
            available_stock=7,
            attributes=[
                {
                    "name": "Tamanho",
                    "value": "M",
                    "sku_property_id": "14",
                    "property_value_id": "771",
                },
                {
                    "name": "Cor",
                    "value": "Azul",
                    "sku_property_id": "10",
                    "property_value_id": "19",
                },
            ],
        ),
        AliExpressProductSku(
            sku_id="%s-G-VERMELHO" % product_id,
            sale_price=Decimal("79.90"),
            original_price=Decimal("79.90"),
            currency="BRL",
            available_stock=4,
            attributes=[
                {
                    "name": "Tamanho",
                    "value": "G",
                    "sku_property_id": "14",
                    "property_value_id": "772",
                },
                {
                    "name": "Cor",
                    "value": "Vermelho",
                    "sku_property_id": "10",
                    "property_value_id": "20",
                },
            ],
        ),
    ]
    return AliExpressProductDetail(
        product_id=product_id,
        title="Vestido candidato BR (sintético)",
        description="Detalhe sintético para o fluxo de origem.",
        images=["https://ae.example/br1.jpg"],
        skus=skus,
        available_stock=11,
        sale_price=Decimal("69.85"),
        original_price=Decimal("79.90"),
        currency="BRL",
    )


def _draft_from_db(product_id=BR_ID):
    from loja.modules.models import Product

    return Product.query.filter_by(
        source="aliexpress", external_id=product_id, is_draft=True
    ).first()


def test_importacao_do_candidato_preserva_identidade_e_e_idempotente(
    admin_catalog, db
):
    FakeAliExpressClient.details[BR_ID] = _br_candidate_detail()

    first = admin_catalog.post(
        f"{PREFIX}/{BR_ID}/importar", follow_redirects=True
    )
    assert "Produto importado como rascunho" in first.get_data(as_text=True)

    product = _draft_from_db()
    assert product is not None
    variants = {v.supplier_sku_id: v for v in product.variants}
    assert set(variants) == {"%s-M-AZUL" % BR_ID, "%s-G-VERMELHO" % BR_ID}
    # Estoque, preços e moeda coerentes com o anúncio.
    assert variants["%s-M-AZUL" % BR_ID].stock == 7
    assert variants["%s-G-VERMELHO" % BR_ID].stock == 4
    assert product.currency == "BRL"
    assert product.price == Decimal("69.85")
    # Atributos necessários ao pedido no fornecedor preservados por SKU.
    attrs = {
        a["name"]: a
        for a in variants["%s-M-AZUL" % BR_ID].attributes or []
    }
    assert attrs["Tamanho"]["sku_property_id"] == "14"
    assert attrs["Tamanho"]["property_value_id"] == "771"
    assert attrs["Cor"]["property_value_id"] == "19"

    # Reimportação idempotente: sem duplicatas.
    second = admin_catalog.post(
        f"{PREFIX}/{BR_ID}/importar", follow_redirects=True
    )
    assert "Produto importado como rascunho" in second.get_data(as_text=True)
    db.session.expire_all()
    assert len(_draft_from_db().variants) == 2


def test_candidato_importado_nasce_com_origem_desconhecida(admin_catalog, db):
    from loja.services.shipping_origin import resolve_product_summary

    FakeAliExpressClient.details[BR_ID] = _br_candidate_detail()
    admin_catalog.post(f"{PREFIX}/{BR_ID}/importar", follow_redirects=True)

    db.session.expire_all()
    summary = resolve_product_summary(_draft_from_db())
    assert summary.label == "unknown"
    assert summary.unknown == 2


def test_confirmacao_de_uma_variante_nao_nacionaliza_as_demais(
    admin_catalog, db
):
    from loja.modules.models import SupplierProduct
    from loja.services.shipping_origin import (
        record_manual_confirmation,
        resolve_product_summary,
    )

    FakeAliExpressClient.details[BR_ID] = _br_candidate_detail()
    admin_catalog.post(f"{PREFIX}/{BR_ID}/importar", follow_redirects=True)

    db.session.expire_all()
    supplier_product = SupplierProduct.query.filter_by(
        product_id=_draft_from_db().id
    ).first()
    record_manual_confirmation(
        supplier_product_id=supplier_product.id,
        origin="brazil",
        evidence_ref="https://pt.aliexpress.com/item/%s.html" % BR_ID,
        sku_id="%s-M-AZUL" % BR_ID,
    )
    db.session.commit()

    db.session.expire_all()
    summary = resolve_product_summary(_draft_from_db())
    # 1 BR + 1 desconhecida = mixed; jamais "brazil" para o produto todo.
    assert summary.label == "mixed"
    assert summary.brazil == 1
    assert summary.unknown == 1
