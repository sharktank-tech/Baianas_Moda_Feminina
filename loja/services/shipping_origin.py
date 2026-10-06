"""Origem de envio por variante com evidência (integração AliExpress, Etapa 1).

Responde, para cada variante, uma de três respostas honestas:

    'brazil'  -> envio do Brasil COMPROVADO (evidência persistida)
    'foreign' -> envio exterior COMPROVADO (evidência persistida)
    None      -> desconhecido (nunca é tratado como nacional)

Regras invariantes:
  * Ausência de linha = desconhecido (fail-closed). Nada é presumido.
  * Escopo XOR por linha: SKU específico ou cobertura total do vínculo; a
    resolução dá precedência ao SKU sobre a cobertura.
  * A cobertura total só vale para os SKUs EXPLICITAMENTE cobertos no
    momento da escrita (`covered_supplier_sku_ids`): SKU novo do vínculo
    nunca herda origem brasileira sozinho — sem snapshot, nada é coberto.
  * Classificação manual (admin) e observação de dado do fornecedor ficam em
    colunas separadas; a API NUNCA sobrescreve a confirmação manual. Divergência
    entre as duas é `conflict=True`, sinalizado — nunca silenciado.
  * Uma linha BR nunca torna o produto "nacional": o produto só é nacional
    quando TODAS as variantes selecionadas e ativas estiverem comprovadas BR.
  * O gate de operação nacional só enxerga variantes ativas MARCADAS com
    `national_shipping_operation`; o catálogo internacional não selecionado
    nunca bloqueia por este recurso.

Nenhum caminho desta API consulta o fornecedor: nenhuma chamada de frete, nenhum
token, nenhuma compra, nenhum pedido.
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Dict, Iterable, List, Optional, Sequence, Set
from uuid import UUID

from loja import db
from loja.modules.models import ProductVariant, SupplierSkuShippingOrigin

ORIGIN_BRAZIL = "brazil"
ORIGIN_FOREIGN = "foreign"
VALID_ORIGINS = (ORIGIN_BRAZIL, ORIGIN_FOREIGN)

SCOPE_SKU = "sku"
SCOPE_COVER_ALL = "cover_all"

#: Rótulos de produto (busca/filtros administrativos).
LABEL_BRAZIL = "brazil"
LABEL_FOREIGN = "foreign"
LABEL_UNKNOWN = "unknown"
LABEL_MIXED = "mixed"
LABEL_CONFLICT = "conflict"

#: Códigos estáveis do gate de operação nacional (readiness + publicação).
REASON_NATIONAL_CONFLICT = "national_origin_conflict"
REASON_NATIONAL_FOREIGN = "national_origin_foreign"
REASON_NATIONAL_UNPROVEN = "national_origin_unproven"

#: Ordem canônica de exibição dos motivos (nunca ordem de inserção).
NATIONAL_REASON_ORDER = (
    REASON_NATIONAL_CONFLICT,
    REASON_NATIONAL_FOREIGN,
    REASON_NATIONAL_UNPROVEN,
)

EVIDENCE_REF_MAX_LEN = 200


class ShippingOriginError(Exception):
    """Entrada inválida de origem/evidência (mensagem é um código estável)."""


# --------------------------------------------------------------------------
# validação de entrada
# --------------------------------------------------------------------------

def normalize_origin(value) -> Optional[str]:
    """Normaliza a classificação de origem. Ausente/vazio -> None.

    Aceita somente os valores canônicos ('brazil'/'foreign'), com tolerância a
    caixa e espaço. Qualquer outra coisa é rejeitada (nunca default silencioso).
    """
    if value is None:
        return None
    text = str(value).strip().lower()
    if not text:
        return None
    if text in VALID_ORIGINS:
        return text
    raise ShippingOriginError("origin_invalid")


def normalize_country(value) -> Optional[str]:
    """Normaliza o país de origem informado (2 letras, opcional)."""
    if value is None:
        return None
    text = str(value).strip().upper()
    if not text:
        return None
    if len(text) != 2 or not text.isalpha():
        raise ShippingOriginError("origin_country_invalid")
    return text


def validate_evidence_ref(value) -> str:
    """Referência da evidência manual: obrigatória e limitada."""
    text = (value or "").strip()
    if not text:
        raise ShippingOriginError("evidence_ref_required")
    if len(text) > EVIDENCE_REF_MAX_LEN:
        raise ShippingOriginError("evidence_ref_too_long")
    return text


# --------------------------------------------------------------------------
# resolução
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class OriginResolution:
    """Resultado da resolução de origem de UMA variante/escopo."""

    origin: Optional[str]
    scope: Optional[str]
    evidenced: bool
    conflict: bool
    row: Optional[SupplierSkuShippingOrigin] = None

    @property
    def known(self) -> bool:
        return self.origin in VALID_ORIGINS and self.evidenced


def _row_evidenced(row: Optional[SupplierSkuShippingOrigin]) -> bool:
    """Evidência persistida e íntegra para a linha informada."""
    if row is None:
        return False
    if row.manual_origin in VALID_ORIGINS:
        return bool(row.evidence_ref and row.confirmed_at)
    if row.api_origin in VALID_ORIGINS:
        return bool(row.api_observed_at)
    return False


def _row_conflict(row: Optional[SupplierSkuShippingOrigin]) -> bool:
    """manual × api divergentes na MESMA linha (sinalizado, nunca silenciado)."""
    if row is None:
        return False
    if not (row.manual_origin and row.api_origin):
        return False
    return row.manual_origin != row.api_origin


# --------------------------------------------------------------------------
# cobertura explícita da linha de cobertura total
# --------------------------------------------------------------------------

def current_supplier_skus(supplier_product_id) -> List[Optional[str]]:
    """SKUs do vínculo NO MOMENTO da escrita (ordem estável, sem duplicata).

    Inclui variantes inativas (a origem já confirmada de uma opção
    momentaneamente desativada continua sendo a mesma). `None` representa
    variante do vínculo sem `supplier_sku_id`.
    """
    if supplier_product_id is None:
        return []
    rows = (
        db.session.query(ProductVariant.supplier_sku_id)
        .filter_by(supplier_product_id=supplier_product_id)
        .distinct()
        .all()
    )
    skus = {value if value is None else str(value) for (value,) in rows}
    if not skus:
        # Vínculo ainda sem variantes: a cobertura total cobre o escopo do
        # produto (resolução sem supplier_sku_id) até existirem SKUs.
        return [None]
    present = sorted((s for s in skus if s is not None))
    if None in skus:
        present.append(None)
    return present


def _snapshot_covered_skus(supplier_product_id) -> str:
    """JSON dos SKUs cobertos agora (gravação da cobertura total)."""
    return json.dumps(current_supplier_skus(supplier_product_id))


def covered_supplier_skus(row: Optional[SupplierSkuShippingOrigin]) -> Optional[Set]:
    """SKUs explicitamente cobertos pela linha, ou None quando não há snapshot.

    Fail-closed: linha de cobertura total sem snapshot não cobre NINGUÉM.
    """
    if row is None:
        return None
    raw = getattr(row, "covered_supplier_sku_ids", None)
    if raw is None:
        return None
    try:
        data = json.loads(raw)
    except (TypeError, ValueError):
        return None
    if not isinstance(data, list):
        return None
    return {
        None if value is None else str(value)
        for value in data
    }


def _covers_sku(
    row: Optional[SupplierSkuShippingOrigin], supplier_sku_id
) -> bool:
    """A linha de cobertura total cobre este SKU? (nunca presume)."""
    covered = covered_supplier_skus(row)
    if covered is None:
        return False
    if supplier_sku_id is None:
        return None in covered
    return str(supplier_sku_id) in covered


def resolve_rows(
    rows: Sequence[SupplierSkuShippingOrigin], supplier_sku_id
) -> OriginResolution:
    """Resolve a origem para um SKU a partir das linhas já carregadas.

    Precedência: linha do SKU > linha de cobertura total. A cobertura total
    só entra quando o SKU está entre os explicitamente cobertos no momento
    da confirmação; SKU novo fica desconhecido até ganhar escrita própria.
    Conflito é avaliado na linha que vence a precedência.
    """
    sku_id = str(supplier_sku_id) if supplier_sku_id else None
    by_id = {
        str(row.supplier_sku_id): row
        for row in rows
        if row.supplier_sku_id
    }
    cover_all = next(
        (
            row
            for row in rows
            if row.covers_all_variants and _covers_sku(row, sku_id)
        ),
        None,
    )
    row = by_id.get(sku_id) if sku_id else None
    scope = SCOPE_SKU if row is not None else None
    if row is None and cover_all is not None:
        row = cover_all
        scope = SCOPE_COVER_ALL
    if row is None:
        return OriginResolution(
            origin=None, scope=None, evidenced=False, conflict=False, row=None
        )

    # Precedência da escrita: confirmação manual > observação do fornecedor.
    # A API jamais sobrescreve o que o admin confirmou.
    if row.manual_origin in VALID_ORIGINS and _row_evidenced(row):
        origin = row.manual_origin
    elif row.api_origin in VALID_ORIGINS and _row_evidenced(row):
        origin = row.api_origin
    else:
        origin = None
    return OriginResolution(
        origin=origin,
        scope=scope,
        evidenced=_row_evidenced(row),
        conflict=_row_conflict(row),
        row=row,
    )


def load_origin_rows(supplier_product_ids: Iterable) -> Dict[str, List]:
    """Carrega as linhas de origem de vários vínculos em UMA consulta."""
    ids = set()
    for value in supplier_product_ids:
        if not value:
            continue
        try:
            ids.add(UUID(str(value)))
        except (TypeError, ValueError):
            continue
    if not ids:
        return {}
    rows = (
        SupplierSkuShippingOrigin.query.filter(
            SupplierSkuShippingOrigin.supplier_product_id.in_(ids)
        )
        .all()
    )
    grouped: Dict[str, List] = {}
    for row in rows:
        grouped.setdefault(str(row.supplier_product_id), []).append(row)
    return grouped


def resolve_variant_origin(
    variant, origin_rows: Optional[Dict[str, List]] = None
) -> OriginResolution:
    """Origem comprovada de uma variante (None = desconhecida)."""
    supplier_product_id = getattr(variant, "supplier_product_id", None)
    if supplier_product_id is None:
        return OriginResolution(
            origin=None, scope=None, evidenced=False, conflict=False, row=None
        )
    if origin_rows is None:
        origin_rows = load_origin_rows([supplier_product_id])
    rows = origin_rows.get(str(supplier_product_id)) or []
    return resolve_rows(rows, getattr(variant, "supplier_sku_id", None))


# --------------------------------------------------------------------------
# resumo do produto (exibição/filtro administrativo)
# --------------------------------------------------------------------------

@dataclass(frozen=True)
class ProductOriginSummary:
    """Resumo da origem das variantes de um produto.

    `label` é o que a interface pode afirmar com honestidade:
      * 'brazil'   -> TODAS as variantes relevantes comprovadas BR
      * 'foreign'  -> todas comprovadas exterior
      * 'unknown'  -> nenhuma comprovada
      * 'mixed'    -> combinação (inclusive 1 BR + 1 desconhecida)
      * 'conflict' -> classificação manual divergente da observação da API
    """

    label: str
    total: int = 0
    brazil: int = 0
    foreign: int = 0
    unknown: int = 0
    conflicts: int = 0

    @property
    def is_national(self) -> bool:
        """Nacional comprovado: todas as variantes BR, nenhuma desconhecida."""
        return self.label == LABEL_BRAZIL and self.total > 0


def _relevant_variants(product) -> List:
    """Variantes ativas; sem ativas, todas (produto legado inativo)."""
    variants = list(getattr(product, "variants", None) or [])
    active = [v for v in variants if getattr(v, "is_active", False)]
    return active or variants


def _supplier_ids_for(product) -> set:
    """Vínculos de fornecedor cujas linhas de origem interessam ao produto."""
    variants = _relevant_variants(product)
    ids = {getattr(v, "supplier_product_id", None) for v in variants} - {None}
    if not variants:
        # Produto sem variantes: só a cobertura total do vínculo ativo serve.
        for link in getattr(product, "supplier_links", None) or []:
            if getattr(link, "is_active", False):
                ids.add(link.id)
                break
    return ids


def resolve_product_summary(
    product, origin_rows: Optional[Dict[str, List]] = None
) -> ProductOriginSummary:
    """Resumo honesto da origem do produto.

    `origin_rows` opcional evita consulta quando o chamador já carregou as
    linhas de vários produtos (lote administrativo).
    """
    variants = _relevant_variants(product)
    supplier_product_ids = _supplier_ids_for(product)
    if origin_rows is None:
        origin_rows = load_origin_rows(supplier_product_ids)

    resolutions: List[OriginResolution] = []
    for variant in variants:
        resolutions.append(resolve_variant_origin(variant, origin_rows))
    if not variants and supplier_product_ids:
        for supplier_product_id in supplier_product_ids:
            rows = origin_rows.get(str(supplier_product_id)) or []
            resolutions.append(
                resolve_rows(rows, supplier_sku_id=None)
            )

    if not resolutions:
        return ProductOriginSummary(label=LABEL_UNKNOWN)

    conflicts = sum(1 for r in resolutions if r.conflict)
    known = [r.origin for r in resolutions if r.known]
    unknown = sum(1 for r in resolutions if not r.known)

    if conflicts:
        label = LABEL_CONFLICT
    elif not known:
        label = LABEL_UNKNOWN
    elif unknown or len(set(known)) > 1:
        # 1 SKU BR + 1 desconhecida é 'mixed': nunca vira "nacional".
        label = LABEL_MIXED
    else:
        label = known[0]

    return ProductOriginSummary(
        label=label,
        total=len(resolutions),
        brazil=sum(1 for o in known if o == ORIGIN_BRAZIL),
        foreign=sum(1 for o in known if o == ORIGIN_FOREIGN),
        unknown=unknown,
        conflicts=conflicts,
    )


def resolve_product_summaries(products) -> Dict[str, ProductOriginSummary]:
    """Resumo de vários produtos em UMA consulta de origem (lista admin)."""
    products = list(products or ())
    supplier_product_ids = set()
    for product in products:
        supplier_product_ids |= _supplier_ids_for(product)
    origin_rows = load_origin_rows(supplier_product_ids)
    return {
        str(product.id): resolve_product_summary(product, origin_rows)
        for product in products
    }


def list_origin_rows(supplier_product_id) -> List:
    """Linhas de origem de um vínculo, na ordem estável de exibição."""
    if not supplier_product_id:
        return []
    rows = SupplierSkuShippingOrigin.query.filter_by(
        supplier_product_id=supplier_product_id
    ).all()
    return sorted(
        rows,
        key=lambda row: (
            0 if row.covers_all_variants else 1,
            str(row.supplier_sku_id or ""),
        ),
    )


# --------------------------------------------------------------------------
# gate de operação nacional (readiness + publicação)
# --------------------------------------------------------------------------

def selected_national_variants(product) -> List:
    """Variantes ativas selecionadas para a operação nacional."""
    return [
        variant
        for variant in (getattr(product, "variants", None) or [])
        if getattr(variant, "is_active", False)
        and getattr(variant, "national_shipping_operation", False)
    ]


def national_operation_reasons(product) -> List[str]:
    """Motos de bloqueio do gate, em ordem canônica (lista pode ser vazia).

    Só olha variantes ativas selecionadas. Uma variante selecionada sem
    evidência bloqueia; com evidência exterior bloqueia; divergência
    manual × api bloqueia. Variante NÃO selecionada nunca aparece aqui.
    """
    selected = selected_national_variants(product)
    if not selected:
        return []

    origin_rows = load_origin_rows(
        {v.supplier_product_id for v in selected} - {None}
    )
    found = set()
    for variant in selected:
        resolution = resolve_variant_origin(variant, origin_rows)
        if resolution.conflict:
            found.add(REASON_NATIONAL_CONFLICT)
        elif resolution.origin == ORIGIN_FOREIGN:
            found.add(REASON_NATIONAL_FOREIGN)
        elif not resolution.known:
            found.add(REASON_NATIONAL_UNPROVEN)
    return [code for code in NATIONAL_REASON_ORDER if code in found]


# --------------------------------------------------------------------------
# escritas (somente pela rota administrativa, com validação prévia)
# --------------------------------------------------------------------------

def _find_row(
    *, supplier_product_id, sku_id=None, covers_all: bool = False
) -> Optional[SupplierSkuShippingOrigin]:
    query = SupplierSkuShippingOrigin.query.filter_by(
        supplier_product_id=supplier_product_id
    )
    if covers_all:
        sku_id = None
    return query.filter(
        SupplierSkuShippingOrigin.supplier_sku_id.is_(None)
        if sku_id is None
        else SupplierSkuShippingOrigin.supplier_sku_id == str(sku_id)
    ).filter(
        SupplierSkuShippingOrigin.covers_all_variants.is_(bool(covers_all))
    ).first()


def record_manual_confirmation(
    *,
    supplier_product_id,
    origin,
    evidence_ref: str,
    sku_id=None,
    covers_all: bool = False,
    origin_country=None,
    user_id=None,
    now=None,
) -> SupplierSkuShippingOrigin:
    """Grava a classificação manual do admin com evidência.

    Nunca toca nas colunas da API (`api_origin`/`api_observed_at`): as duas
    fontes convivem e um conflito posterior fica visível, não apagado.
    """
    from loja.utils import utcnow

    if not covers_all and not sku_id:
        raise ShippingOriginError("scope_required")
    if bool(covers_all) and sku_id:
        raise ShippingOriginError("scope_invalid")
    origin = normalize_origin(origin)
    if origin is None:
        raise ShippingOriginError("origin_required")
    evidence_ref = validate_evidence_ref(evidence_ref)
    country = normalize_country(origin_country)

    row = _find_row(
        supplier_product_id=supplier_product_id,
        sku_id=None if covers_all else sku_id,
        covers_all=covers_all,
    )
    if row is None:
        row = SupplierSkuShippingOrigin(
            supplier_product_id=supplier_product_id,
            supplier_sku_id=None if covers_all else str(sku_id),
            covers_all_variants=bool(covers_all),
        )
        db.session.add(row)
    row.manual_origin = origin
    row.origin_country = country
    row.evidence_ref = evidence_ref
    row.confirmed_by_user_id = user_id
    row.confirmed_at = now or utcnow()
    if covers_all:
        # Reconfirmar a cobertura total redefine quem ela cobre: os SKUs
        # existentes AGORA. SKU nascido depois fica fora até nova escrita.
        row.covered_supplier_sku_ids = _snapshot_covered_skus(
            supplier_product_id
        )
    db.session.flush()
    return row


def clear_manual_confirmation(
    *, supplier_product_id, sku_id=None, covers_all: bool = False
) -> bool:
    """Apaga a confirmação manual, preservando qualquer observação da API.

    Linha que fica sem nenhuma origem é removida (nada de registro vazio).
    """
    if not covers_all and not sku_id:
        raise ShippingOriginError("scope_required")
    row = _find_row(
        supplier_product_id=supplier_product_id,
        sku_id=None if covers_all else sku_id,
        covers_all=covers_all,
    )
    if row is None:
        return False
    if row.api_origin in VALID_ORIGINS:
        row.manual_origin = None
        row.origin_country = None
        row.evidence_ref = None
        row.confirmed_by_user_id = None
        row.confirmed_at = None
        db.session.flush()
    else:
        db.session.delete(row)
        db.session.flush()
    return True


def record_api_observation(
    *,
    supplier_product_id,
    origin,
    observed_at=None,
    sku_id=None,
    covers_all: bool = False,
    origin_country=None,
    now=None,
) -> SupplierSkuShippingOrigin:
    """Registra valor OBSERVADO em dado do fornecedor (coluna da API).

    Único caminho de escrita da coluna `api_origin`. Nunca altera
    `manual_origin`: se divergirem, a resolução sinaliza conflito.
    Chamado apenas quando o código realmente observa ship_from_country em
    resposta do fornecedor — nunca com valor presumido.
    """
    from loja.utils import utcnow

    if not covers_all and not sku_id:
        raise ShippingOriginError("scope_required")
    if bool(covers_all) and sku_id:
        raise ShippingOriginError("scope_invalid")
    origin = normalize_origin(origin)
    if origin is None:
        raise ShippingOriginError("origin_required")
    country = normalize_country(origin_country)

    row = _find_row(
        supplier_product_id=supplier_product_id,
        sku_id=None if covers_all else sku_id,
        covers_all=covers_all,
    )
    created = row is None
    if row is None:
        row = SupplierSkuShippingOrigin(
            supplier_product_id=supplier_product_id,
            supplier_sku_id=None if covers_all else str(sku_id),
            covers_all_variants=bool(covers_all),
        )
        db.session.add(row)
    row.api_origin = origin
    if country is not None:
        row.origin_country = country
    row.api_observed_at = observed_at or now or utcnow()
    if covers_all and created:
        # A observação da API tampouco herda SKUs futuros: a cobertura é
        # fixada nos SKUs existentes quando a linha nasce.
        row.covered_supplier_sku_ids = _snapshot_covered_skus(
            supplier_product_id
        )
    db.session.flush()
    return row


def set_national_operation(product, variant_ids: Optional[Iterable]) -> int:
    """Define QUEM participa da operação nacional (seleção explícita).

    `variant_ids is None` -> nenhuma variante selecionada. IDs fora do
    produto ou de variante inativa são ignorados (a seleção só vale para
    variante ativa). Retorna quantas variantes ficaram selecionadas.
    """
    wanted = (
        {str(value) for value in variant_ids}
        if variant_ids is not None
        else set()
    )
    selected = 0
    for variant in getattr(product, "variants", None) or []:
        active = bool(getattr(variant, "is_active", False))
        checked = active and str(variant.id) in wanted
        variant.national_shipping_operation = checked
        if checked:
            selected += 1
    db.session.flush()
    return selected


def summary_label(label: str) -> str:
    """Rótulo legível do resumo (exibição administrativa)."""
    return {
        LABEL_BRAZIL: "Origem Brasil confirmada",
        LABEL_FOREIGN: "Origem exterior confirmada",
        LABEL_UNKNOWN: "Origem não verificada",
        LABEL_MIXED: "Origem mista/desconhecida",
        LABEL_CONFLICT: "Origem divergente (manual × API)",
    }.get(label, "Origem não verificada")


__all__ = [
    "ORIGIN_BRAZIL",
    "ORIGIN_FOREIGN",
    "VALID_ORIGINS",
    "SCOPE_SKU",
    "SCOPE_COVER_ALL",
    "LABEL_BRAZIL",
    "LABEL_FOREIGN",
    "LABEL_UNKNOWN",
    "LABEL_MIXED",
    "LABEL_CONFLICT",
    "REASON_NATIONAL_CONFLICT",
    "REASON_NATIONAL_FOREIGN",
    "REASON_NATIONAL_UNPROVEN",
    "NATIONAL_REASON_ORDER",
    "ShippingOriginError",
    "OriginResolution",
    "ProductOriginSummary",
    "normalize_origin",
    "normalize_country",
    "validate_evidence_ref",
    "resolve_rows",
    "load_origin_rows",
    "resolve_variant_origin",
    "resolve_product_summary",
    "resolve_product_summaries",
    "list_origin_rows",
    "selected_national_variants",
    "national_operation_reasons",
    "record_manual_confirmation",
    "clear_manual_confirmation",
    "record_api_observation",
    "set_national_operation",
    "summary_label",
]
