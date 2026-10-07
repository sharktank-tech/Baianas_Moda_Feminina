"""Rate Limit (ApiCallLimit/555) em consultas somente leitura.

Cobre, sem nenhuma chamada real de rede:
- classificação: `sub_code` ApiCallLimit (qualquer caixa/pontuação) vira
  `AliExpressRateLimitError`; `call()` puro continua sem retry;
- espera do fornecedor: campos explícitos (`retry_after` e variantes),
  menção na mensagem com intenção de nova tentativa, limites e inválidos;
- retry limitado com espera progressiva: bloqueio temporário, recuperação,
  esgotamento, sem retry em outros erros, parâmetros reconstruídos;
- rotas: aviso claro de nova tentativa (warning) em vez do texto técnico,
  recuperação com 2 chamadas, sem chamadas além do orçamento;
- formulários: guarda anti-duplo-clique presente nas consultas.

Caminhos de escrita nunca repetem chamadas: `client.call()` não tem retry.
"""

from __future__ import annotations

import json
import time

import pytest

from loja.integrations.aliexpress.client import AliExpressClient
from loja.integrations.aliexpress.errors import (
    AliExpressAPIError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from loja.integrations.aliexpress.rate_limit import (
    RATE_LIMIT_MESSAGE,
    call_with_rate_limit_retry,
    supplier_wait_seconds,
)
from tests.test_aliexpress_admin_catalog import (  # noqa: F401  (fixtures)
    PREFIX,
    PRODUCT_ID,
    TITLE,
    FakeAliExpressClient,
    admin_catalog,  # noqa: F401
    ae_env,  # noqa: F401
    catalog,  # noqa: F401
    fake_client,  # noqa: F401
)


def _error_body(**fields):
    return json.dumps({"error_response": {"code": 50, "msg": "Remote service error", **fields}})


class _SequencedTransport:
    """Transport fake com corpos em sequência (ou exceções)."""

    def __init__(self, bodies):
        self.bodies = list(bodies)
        self.calls = 0
        self.params_history = []

    def post(self, params):
        self.calls += 1
        self.params_history.append(dict(params))
        body = self.bodies[min(self.calls - 1, len(self.bodies) - 1)]
        if isinstance(body, Exception):
            raise body
        return body


def _rate_limit_body(**fields):
    base = {"code": 555, "msg": "rate limit exceeded"}
    base.update(fields)
    return json.dumps({"error_response": base})


def _client(bodies, **kwargs):
    return AliExpressClient(
        app_key="k", app_secret="s", transport=_SequencedTransport(bodies), **kwargs
    )


# --------------------------------------------------------------------------
# 1) Classificação
# --------------------------------------------------------------------------

def test_sub_code_apicalllimit_vira_rate_limit():
    client = _client([_error_body(sub_code="isv.ApiCallLimit")])
    with pytest.raises(AliExpressRateLimitError):
        client.call("aliexpress.x")


def test_sub_code_apicalllimit_insensivel_a_caixa_e_pontuacao():
    for sub in ("ISV.APICALLLIMIT", "isv_api_call_limit", "ApiCallLimit"):
        client = _client([_error_body(sub_code=sub)])
        with pytest.raises(AliExpressRateLimitError):
            client.call("aliexpress.x")


def test_sub_code_comum_nao_vira_rate_limit():
    client = _client(
        [_error_body(code=20020, msg="fail", sub_code="isv.invalid-parameter")]
    )
    with pytest.raises(AliExpressAPIError) as excinfo:
        client.call("aliexpress.x")
    assert not isinstance(excinfo.value, AliExpressRateLimitError)


def test_call_puro_continua_sem_retry():
    fake = _SequencedTransport([_rate_limit_body()])
    client = AliExpressClient(app_key="k", app_secret="s", transport=fake)
    with pytest.raises(AliExpressRateLimitError):
        client.call("aliexpress.x")
    assert fake.calls == 1


# --------------------------------------------------------------------------
# 2) Espera informada pelo fornecedor
# --------------------------------------------------------------------------

def _rate_exc(**fields):
    base = {"code": 555, "msg": "rate limit exceeded"}
    base.update(fields)
    try:
        _client([json.dumps({"error_response": base})]).call("aliexpress.x")
    except AliExpressRateLimitError as exc:
        return exc
    raise AssertionError("deveria levantar RateLimit")


def test_espera_explicita_retry_after():
    assert supplier_wait_seconds(_rate_exc(retry_after=7)) == 7.0


def test_espera_explicita_variantes_de_nome():
    assert supplier_wait_seconds(_rate_exc(RetryAfter=4)) == 4.0
    assert supplier_wait_seconds(_rate_exc(wait_seconds="3")) == 3.0


def test_espera_invalida_ignorada():
    assert supplier_wait_seconds(_rate_exc(retry_after=True)) is None
    assert supplier_wait_seconds(_rate_exc(retry_after=-5)) is None
    assert supplier_wait_seconds(_rate_exc(retry_after="em breve")) is None
    assert supplier_wait_seconds(_rate_exc()) is None


def test_espera_na_mensagem_com_intencao():
    exc = _rate_exc(sub_msg="tente novamente em 30 segundos")
    assert supplier_wait_seconds(exc) == 30.0
    exc = _rate_exc(msg="rate limit exceeded, retry after 5s")
    assert supplier_wait_seconds(exc) == 5.0


def test_numero_sem_intencao_nao_vira_espera():
    exc = _rate_exc(msg="limit 100 per 60s")
    assert supplier_wait_seconds(exc) is None


# --------------------------------------------------------------------------
# 3) Retry limitado com espera progressiva
# --------------------------------------------------------------------------

class _StubClient:
    def __init__(self, effects):
        self.effects = list(effects)
        self.calls = 0

    def call(self, method, **business):
        self.calls += 1
        effect = self.effects[min(self.calls - 1, len(self.effects) - 1)]
        if isinstance(effect, Exception):
            raise effect
        return effect


def _rate_exc_simple():
    return _rate_exc()


def test_bloqueio_temporario_recupera_com_espera_progressiva():
    sleeps = []
    stub = _StubClient(
        [_rate_exc_simple(), _rate_exc_simple(), {"ok": True}]
    )
    result = call_with_rate_limit_retry(
        stub, "aliexpress.x", {}, sleep=sleeps.append
    )
    assert result == {"ok": True}
    assert stub.calls == 3
    assert sleeps == [1.0, 2.0]


def test_recuperacao_imediata_uma_espera():
    sleeps = []
    stub = _StubClient([_rate_exc_simple(), {"ok": True}])
    assert call_with_rate_limit_retry(stub, "aliexpress.x", {}, sleep=sleeps.append)
    assert stub.calls == 2
    assert sleeps == [1.0]


def test_esgotamento_relanca_apos_orcamento():
    sleeps = []
    stub = _StubClient([_rate_exc_simple()] * 10)
    with pytest.raises(AliExpressRateLimitError):
        call_with_rate_limit_retry(
            stub, "aliexpress.x", {}, max_retries=2, sleep=sleeps.append
        )
    assert stub.calls == 3
    assert sleeps == [1.0, 2.0]


def test_max_retries_zero_nao_repete():
    sleeps = []
    stub = _StubClient([_rate_exc_simple(), {"ok": True}])
    with pytest.raises(AliExpressRateLimitError):
        call_with_rate_limit_retry(
            stub, "aliexpress.x", {}, max_retries=0, sleep=sleeps.append
        )
    assert stub.calls == 1
    assert sleeps == []


def test_outros_erros_nao_repetem():
    sleeps = []
    stub = _StubClient(
        [AliExpressAPIError("fail"), {"ok": True}]
    )
    with pytest.raises(AliExpressAPIError):
        call_with_rate_limit_retry(stub, "aliexpress.x", {}, sleep=sleeps.append)
    assert stub.calls == 1
    assert sleeps == []

    timeout_stub = _StubClient(
        [AliExpressTransportError("timeout"), {"ok": True}]
    )
    with pytest.raises(AliExpressTransportError):
        call_with_rate_limit_retry(
            timeout_stub, "aliexpress.x", {}, sleep=sleeps.append
        )
    assert timeout_stub.calls == 1


def test_espera_do_fornecedor_prevalece_e_respeita_teto():
    sleeps = []
    stub = _StubClient([_rate_exc(retry_after=7), {"ok": True}])
    call_with_rate_limit_retry(stub, "aliexpress.x", {}, sleep=sleeps.append)
    assert sleeps == [7.0]

    capped = []
    stub = _StubClient([_rate_exc(retry_after=120), {"ok": True}])
    call_with_rate_limit_retry(
        stub, "aliexpress.x", {}, max_delay=10.0, sleep=capped.append
    )
    assert capped == [10.0]


def test_parametros_reconstruidos_a_cada_tentativa(monkeypatch):
    state = {"t": 1700000000.0}

    def tick():
        state["t"] += 0.0011
        return state["t"]

    monkeypatch.setattr(time, "time", tick)
    fake = _SequencedTransport([_rate_limit_body(), _rate_limit_body(), _rate_limit_body()])
    client = AliExpressClient(app_key="k", app_secret="s", transport=fake)
    sleeps = []
    with pytest.raises(AliExpressRateLimitError):
        call_with_rate_limit_retry(
            client, "aliexpress.x", {"a": "1"}, sleep=sleeps.append
        )
    stamps = [p["timestamp"] for p in fake.params_history]
    assert len(set(stamps)) == 3


def test_busca_readonly_recupera_e_detalhe_tambem():
    search_ok = json.dumps(
        {"products": [{"itemId": PRODUCT_ID, "title": TITLE}], "totalCount": 1}
    )
    client = _client([_rate_limit_body(), search_ok])
    result = client.search_products(keyword="vestido")
    assert result.total == 1
    assert client._transport.calls == 2

    detail_ok = json.dumps(
        {"result": {"ae_item_base_info_dto": {"product_id": PRODUCT_ID}}}
    )
    client = _client([_rate_limit_body(), detail_ok])
    detail = client.get_product(product_id=PRODUCT_ID)
    assert detail.product_id == PRODUCT_ID
    assert client._transport.calls == 2


# --------------------------------------------------------------------------
# 4) Rotas: aviso claro e recuperação
# --------------------------------------------------------------------------

def test_buscar_bloqueio_mostra_aviso_claro(admin_catalog):
    FakeAliExpressClient.search_error = AliExpressRateLimitError(
        "rate limit exceeded", code="555"
    )
    try:
        body = admin_catalog.post(
            f"{PREFIX}/buscar", data={"q": "vestido"}
        ).get_data(as_text=True)
    finally:
        FakeAliExpressClient.search_error = None
    assert RATE_LIMIT_MESSAGE in body
    assert "bg-yellow-500" in body
    assert "rate limit exceeded" not in body


def test_buscar_erro_comum_mantem_perigo_sanitizado(admin_catalog):
    FakeAliExpressClient.search_error = AliExpressAPIError("falha simulada")
    try:
        body = admin_catalog.post(
            f"{PREFIX}/buscar", data={"q": "vestido"}
        ).get_data(as_text=True)
    finally:
        FakeAliExpressClient.search_error = None
    assert "falha simulada" in body
    assert RATE_LIMIT_MESSAGE not in body
    assert "bg-zinc-800" in body


def test_buscar_recupera_apos_bloqueio_sem_chamadas_extras(admin_catalog, monkeypatch):
    import loja.views.aliexpress_admin_routes as routes

    search_ok = json.dumps(
        {"products": [{"itemId": PRODUCT_ID, "title": TITLE}], "totalCount": 1}
    )
    transport = _SequencedTransport([_rate_limit_body(), search_ok])

    def factory(**kwargs):
        return AliExpressClient(app_key="k", app_secret="s", transport=transport)

    monkeypatch.setattr(routes, "AliExpressClient", factory)
    resp = admin_catalog.post(f"{PREFIX}/buscar", data={"q": "vestido"})
    body = resp.get_data(as_text=True)
    assert resp.status_code == 200
    assert TITLE in body
    # 1 bloqueio + 1 sucesso: sem chamadas além do orçamento.
    assert transport.calls == 2


def test_detalhe_bloqueio_mostra_aviso_claro(admin_catalog):
    FakeAliExpressClient.detail_error = AliExpressRateLimitError(
        "ApiCallLimit", code="50", sub_code="isv.ApiCallLimit"
    )
    try:
        resp = admin_catalog.get(
            f"{PREFIX}/{PRODUCT_ID}/detalhe", follow_redirects=True
        )
        body = resp.get_data(as_text=True)
    finally:
        FakeAliExpressClient.detail_error = None
    assert RATE_LIMIT_MESSAGE in body
    assert "bg-yellow-500" in body


# --------------------------------------------------------------------------
# 5) Formulários: guarda anti-duplo-clique
# --------------------------------------------------------------------------

def test_formularios_de_consulta_tem_guarda_anti_duplo_clique(admin_catalog):
    body = admin_catalog.get(f"{PREFIX}/").get_data(as_text=True)
    assert "form[data-once]" in body
    assert body.count("data-once") >= 3  # buscar, buscar-id (+ script)


def test_detalhe_e_comparar_tem_guarda(admin_catalog, monkeypatch):
    from tests.test_aliexpress_lookup_by_id import _br_candidate_detail

    FakeAliExpressClient.details[PRODUCT_ID] = _br_candidate_detail(
        product_id=PRODUCT_ID
    )
    body = admin_catalog.get(f"{PREFIX}/{PRODUCT_ID}/detalhe").get_data(
        as_text=True
    )
    # importar + 3 comparar + moeda
    assert body.count("data-once") >= 6
