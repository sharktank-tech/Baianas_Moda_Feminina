"""Fluxo de origem first-touch até o pedido (T03).

Cenários sobre `capture_acquisition`/`order_acquisition` (sem banco):
primeira origem, navegação interna, segunda campanha, acesso direto,
referrer externo e passagem pelo gateway (snapshot na criação).
"""
from types import SimpleNamespace

from loja.services.acquisition_source import (
    acquisition_to_order_kwargs,
    capture_acquisition,
    order_acquisition,
)


def _req(args=None, referrer="", endpoint="main.home"):
    return SimpleNamespace(args=args or {}, referrer=referrer,
                           endpoint=endpoint, method="GET")


def test_primeira_origem_persistida():
    session = {}
    capture_acquisition(
        _req({"utm_source": "instagram", "utm_medium": "organic_social",
              "utm_campaign": "piloto_look01"}), session=session)
    assert order_acquisition(session=session) == {
        "source": "instagram", "medium": "organic_social",
        "campaign": "piloto_look01", "referrer": None,
    }


def test_navegacao_interna_nao_sobrescreve():
    session = {}
    capture_acquisition(_req({"utm_source": "instagram"}), session=session)
    capture_acquisition(_req({}, referrer="https://www.pollomodas.com.br/x",
                             endpoint="main.produto_individual"),
                        session=session)
    assert order_acquisition(session=session)["source"] == "instagram"


def test_segunda_campanha_nao_sobrescreve():
    session = {}
    capture_acquisition(_req({"utm_source": "instagram"}), session=session)
    capture_acquisition(_req({"utm_source": "tiktok",
                              "utm_campaign": "outra"}), session=session)
    stored = order_acquisition(session=session)
    assert (stored["source"], stored["campaign"]) == ("instagram", None)


def test_acesso_direto_sem_origem():
    session = {}
    out = capture_acquisition(_req({}), session=session)
    assert out == {"source": None, "medium": None, "campaign": None,
                   "referrer": None}
    assert session == {}
    assert order_acquisition(session=session) == {
        "source": None, "medium": None, "campaign": None, "referrer": None,
    }


def test_referrer_externo_capturado_sem_utm():
    session = {}
    capture_acquisition(
        _req({}, referrer="https://www.google.com/search?q=x"),
        session=session)
    assert order_acquisition(session=session)["referrer"] == "google.com"


def test_kwargs_do_pedido_mapeiam_snapshot():
    snap = {"source": "instagram", "medium": "organic_social",
            "campaign": "piloto_look01", "referrer": None}
    kwargs = acquisition_to_order_kwargs(snap)
    assert kwargs == {
        "acquisition_source": "instagram",
        "acquisition_medium": "organic_social",
        "acquisition_campaign": "piloto_look01",
        "acquisition_referrer": None,
    }
    # Mudança posterior na sessão não afeta o snapshot já extraído.
    snap["source"] = "tiktok"
    assert kwargs["acquisition_source"] == "instagram"


def test_gateway_nao_vira_origem():
    # O Order recebe acquisition_* na criação (pré-gateway); webhook/retorno
    # só tocam estados de pagamento. Aqui: sessão posterior com origem de
    # retorno não altera o snapshot congelado na criação.
    session = {}
    capture_acquisition(_req({"utm_source": "instagram"}), session=session)
    frozen = acquisition_to_order_kwargs(order_acquisition(session=session))
    capture_acquisition(
        _req({}, referrer="https://www.mercadopago.com.br/checkout"),
        session=session)
    assert frozen["acquisition_source"] == "instagram"
