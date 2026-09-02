"""Enriquecimento idempotente da identidade SKU necessária ao placeorder.

Fase 8.0.10: produtos/variantes já existentes foram importados antes de
``product.py`` preservar ``sku_property_id``/``property_value_id``. Este módulo
relê ``aliexpress.ds.product.get`` e atualiza SOMENTE
``ProductVariant.attributes`` das variantes AliExpress correspondentes.

Não altera Product.price/cost/pricing_status/publicação, SupplierProduct,
estoque, imagens, UUIDs, supplier_sku_id nem snapshots de fulfillment.
Não executa nenhuma escrita remota: todas as chamadas AliExpress são read-only.

Segurança/runtime:
* usa a credencial persistente criptografada da Fase 8;
* exige exatamente um supplier AliExpress ativo com credencial ativa;
* nunca loga product_id, sku_id, token, secret, payload ou atributos externos;
* processa exatamente UM produto por execução no runtime serverless;
* cliente operacional usa timeout de 5 s;
* o POST operacional pode pular a recomputação de cobertura e retornar logo
  após o commit; a cobertura é lida no GET seguinte, em outra invocação;
* cada produto é commitado isoladamente; falha não corrompe progresso anterior.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Optional

from sqlalchemy.orm import selectinload

from loja import db
from loja.modules.models import Product, ProductVariant
from loja.utils import utcnow

from .client import AliExpressClient
from .credential_store import AliExpressCredential, get_fulfillment_access_token
from .errors import (
    AliExpressAPIError,
    AliExpressAuthError,
    AliExpressConfigError,
    AliExpressRateLimitError,
    AliExpressTransportError,
)
from .submission_snapshot import build_sku_attr

ENRICH_HTTP_TIMEOUT = 5.0


class SkuIdentityEnrichmentError(Exception):
    """Erro sanitizado de configuração/credencial do enriquecimento."""


@dataclass(frozen=True)
class SkuIdentityCoverage:
    total: int
    complete: int
    incomplete: int


@dataclass(frozen=True)
class SkuIdentityEnrichmentResult:
    processed_products: int
    updated_variants: int
    unresolved_variants: int
    failed_products: int
    coverage: Optional[SkuIdentityCoverage]


def has_complete_sku_identity(attributes) -> bool:
    """True somente quando os atributos já produzem um sku_attr canônico."""
    return build_sku_attr(attributes) is not None


def get_sku_identity_coverage(session=None) -> SkuIdentityCoverage:
    session = session or db.session
    variants = (
        session.query(ProductVariant)
        .join(Product, ProductVariant.product_id == Product.id)
        .filter(Product.source == "aliexpress")
        .all()
    )
    complete = sum(
        1 for variant in variants if has_complete_sku_identity(variant.attributes)
    )
    total = len(variants)
    return SkuIdentityCoverage(
        total=total,
        complete=complete,
        incomplete=total - complete,
    )


def _single_active_supplier_id():
    credentials = AliExpressCredential.query.filter_by(status="active").all()
    eligible = [
        credential
        for credential in credentials
        if credential.supplier is not None
        and credential.supplier.channel_type == "aliexpress"
        and bool(credential.supplier.seller_id)
    ]
    if len(eligible) != 1:
        raise SkuIdentityEnrichmentError("active_aliexpress_credential_not_unique")
    return eligible[0].supplier_id


def _build_client(supplier_id) -> AliExpressClient:
    app_key = (os.environ.get("ALIEXPRESS_APP_KEY") or "").strip()
    app_secret = (os.environ.get("ALIEXPRESS_APP_SECRET") or "").strip()
    if not app_key or not app_secret:
        raise SkuIdentityEnrichmentError("aliexpress_app_config_missing")

    try:
        token = get_fulfillment_access_token(supplier_id)
    except Exception:
        raise SkuIdentityEnrichmentError("aliexpress_credential_unavailable") from None

    return AliExpressClient(
        app_key=app_key,
        app_secret=app_secret,
        access_token=token,
        timeout=ENRICH_HTTP_TIMEOUT,
    )


def _supplier_link(product: Product, supplier_id):
    links = [
        link
        for link in (product.supplier_links or [])
        if link.supplier_id == supplier_id
    ]
    return links[0] if len(links) == 1 else None


def _variants_for_link(product: Product, supplier_product_id):
    return [
        variant
        for variant in (product.variants or [])
        if variant.supplier_product_id == supplier_product_id
    ]


def enrich_sku_identity_batch(
    *,
    batch_size: int = 1,
    client: Optional[AliExpressClient] = None,
    supplier_id=None,
    session=None,
    include_coverage: bool = True,
) -> SkuIdentityEnrichmentResult:
    """Enriquece exatamente um produto ainda pendente por execução.

    O argumento ``batch_size`` é mantido por compatibilidade/testes, mas somente
    o valor 1 é aceito. Cada execução gera no máximo uma chamada lógica
    ``ds.product.get``. Não há retry/fallback automático.

    ``include_coverage=False`` existe para o POST serverless operacional: após
    persistir o produto, a função pode responder imediatamente. O GET seguinte
    calcula a cobertura em uma invocação separada.
    """
    if batch_size != 1:
        raise SkuIdentityEnrichmentError("invalid_batch_size")

    session = session or db.session
    supplier_id = supplier_id or _single_active_supplier_id()
    client = client or _build_client(supplier_id)

    products = (
        session.query(Product)
        .options(
            selectinload(Product.variants),
            selectinload(Product.supplier_links),
        )
        .filter(Product.source == "aliexpress")
        .filter(Product.external_id.isnot(None))
        .order_by(Product.created_at.asc(), Product.id.asc())
        .all()
    )

    candidate = None
    for product in products:
        link = _supplier_link(product, supplier_id)
        if link is None:
            continue
        variants = _variants_for_link(product, link.id)
        if any(not has_complete_sku_identity(v.attributes) for v in variants):
            candidate = (product, link.id)
            break

    processed_products = 0
    updated_variants = 0
    unresolved_variants = 0
    failed_products = 0

    if candidate is not None:
        product, supplier_product_id = candidate
        try:
            detail = client.get_product(product_id=str(product.external_id))
            sku_by_id = {
                str(sku.sku_id): sku
                for sku in (detail.skus or [])
                if getattr(sku, "sku_id", None)
            }
            product_updates = 0
            product_unresolved = 0

            for variant in _variants_for_link(product, supplier_product_id):
                if has_complete_sku_identity(variant.attributes):
                    continue
                sku = sku_by_id.get(str(variant.supplier_sku_id or ""))
                attrs = list(getattr(sku, "attributes", None) or []) if sku else []
                if not has_complete_sku_identity(attrs):
                    product_unresolved += 1
                    continue
                variant.attributes = attrs
                variant.updated_at = utcnow()
                product_updates += 1

            session.commit()
            processed_products = 1
            updated_variants = product_updates
            unresolved_variants = product_unresolved

        except (
            AliExpressAPIError,
            AliExpressAuthError,
            AliExpressRateLimitError,
            AliExpressTransportError,
            AliExpressConfigError,
        ):
            session.rollback()
            failed_products = 1
        except Exception:
            session.rollback()
            failed_products = 1

    coverage = get_sku_identity_coverage(session=session) if include_coverage else None
    return SkuIdentityEnrichmentResult(
        processed_products=processed_products,
        updated_variants=updated_variants,
        unresolved_variants=unresolved_variants,
        failed_products=failed_products,
        coverage=coverage,
    )


__all__ = [
    "ENRICH_HTTP_TIMEOUT",
    "SkuIdentityEnrichmentError",
    "SkuIdentityCoverage",
    "SkuIdentityEnrichmentResult",
    "has_complete_sku_identity",
    "get_sku_identity_coverage",
    "enrich_sku_identity_batch",
]
