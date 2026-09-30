"""Identificação centralizada de cor/tamanho em atributos de variante.

Única fonte de verdade usada pela importação (AliExpress) e pela
apresentação (seletor, carrinho, checkout, pedido).

Allowlist explícita — nomes de atributo conhecidos e sku_property_id
conhecidos. Atributos desconhecidos NUNCA são classificados como cor ou
tamanho (retornam None): nenhuma dimensão é presumida.

Casos reconhecidos além dos aliases originais:
  * "Tamanho infantil dos EUA" (tamanhos infantis US, ex.: "5T(Size 120)"),
    também via sku_property_id "200000463" (estável entre traduções).
  * "Tamanho do sapato" (numeração BR de calçados, ex.: "46"), também via
    sku_property_id "200000124".
  * Propriedade canônica de cor do AliExpress (sku_property_id "14").

Valores originais são preservados integralmente: sem conversão
(ex.: tamanhos infantis NÃO viram P/M/G), sem equivalências deduzidas,
apenas strip de espaços — como já faziam importador e apresentação.
"""

from __future__ import annotations

from typing import Dict, List, Optional, Tuple

# União dos aliases que já existiam no importador (color/colour/cor,
# size/tamanho) e na apresentação (cor/color, tamanho/size/talla), mais o
# atributo do caso real "Tamanho infantil dos EUA".
COLOR_NAMES = frozenset({"color", "colour", "cor"})
SIZE_NAMES = frozenset({
    "size",
    "tamanho",
    "talla",
    "tamanho infantil dos eua",
    "tamanho do sapato",
})

# sku_property_id conhecidos do AliExpress (estáveis entre traduções):
# "14" = cor; "200000463" = "Tamanho infantil dos EUA";
# "200000124" = "Tamanho do sapato".
COLOR_PROPERTY_IDS = frozenset({"14"})
SIZE_PROPERTY_IDS = frozenset({"200000463", "200000124"})

# Teto dos snapshots de rótulo (CartItem.selected_size e
# OrderItem.selected_size após a migração 0028). Rótulos acima disso falham
# de forma explícita — nunca há truncamento silencioso.
VARIANT_LABEL_MAX_LEN = 40
# Teto próprio da cor (ProductVariant.color VARCHAR(60, inalterado): cores
# de 41–60 caracteres são aceitas; acima disso, rejeição explícita.
VARIANT_COLOR_MAX_LEN = 60


def _norm_name(value) -> str:
    return str(value or "").strip().lower()


def _norm_prop(value) -> str:
    return str(value or "").strip()


def classify_variant_attribute(attr) -> Optional[str]:
    """Classifica UM atributo como "color", "size" ou None.

    Casa por nome (allowlist, case-insensitive) ou por sku_property_id
    (allowlist). Precedência da cor preservada do importador original.
    Qualquer atributo fora das listas retorna None.
    """
    if not isinstance(attr, dict):
        return None
    name = _norm_name(attr.get("name"))
    prop = _norm_prop(attr.get("sku_property_id", attr.get("property_id")))
    if name in COLOR_NAMES or (prop and prop in COLOR_PROPERTY_IDS):
        return "color"
    if name in SIZE_NAMES or (prop and prop in SIZE_PROPERTY_IDS):
        return "size"
    return None


def _iter_attributes(attributes):
    """Normaliza listas e dicionários para o mesmo formato de dicts.

    Aceita a lista bruta do importer ([{"name","value","sku_property_id"}])
    e dict nome->valor legado: ambos produzem o MESMO resultado nas funções
    de classificação/extração (seletor, carrinho, checkout e pedido).
    """
    if isinstance(attributes, dict):
        for name, value in attributes.items():
            yield {"name": name, "value": value}
        return
    for attr in attributes or []:
        if isinstance(attr, dict):
            yield attr


def variant_attribute_value(attributes, dim: str) -> Optional[str]:
    """Primeiro valor não vazio do atributo da dimensão ("color"/"size").

    Recebe lista bruta ou dict (normalizados por `_iter_attributes`).
    Dimensão desconhecida ou ausência de valor retorna None.
    """
    if dim not in ("color", "size"):
        return None
    for attr in _iter_attributes(attributes):
        if classify_variant_attribute(attr) != dim:
            continue
        value = str(attr.get("value") or "").strip()
        if value:
            return value
    return None


def extract_variant_color_size(attributes) -> Tuple[Optional[str], Optional[str]]:
    """Extrai (color, size) da lista bruta de atributos. Usado na importação."""
    return (
        variant_attribute_value(attributes, "color"),
        variant_attribute_value(attributes, "size"),
    )


def resolve_variant_dim(column_value, attributes, dim: str) -> Optional[str]:
    """Rótulo de exibição: coluna normalizada primeiro, attributes como fallback.

    É o que permite corrigir a apresentação de produtos EXISTENTES sem
    reimportação e sem atualização em massa: quando color/size estão NULL,
    o valor preservado em attributes é usado. Coluna preenchida sempre vence
    (nenhum comportamento anterior muda nesses casos).
    """
    column = str(column_value or "").strip()
    if column:
        return column
    return variant_attribute_value(attributes, dim)


def variant_dim_value(variant, dim: str) -> Optional[str]:
    """Rótulo de exibição a partir do objeto variante (None-safe).

    Usado nos templates e nos rótulos de carrinho/checkout/pedido: separa a
    identidade da variante (variant_id/SKU) do texto apresentado.
    """
    if variant is None or dim not in ("color", "size"):
        return None
    column = getattr(variant, "color" if dim == "color" else "size", None)
    return resolve_variant_dim(column, getattr(variant, "attributes", None), dim)


__all__ = [
    "COLOR_NAMES",
    "SIZE_NAMES",
    "COLOR_PROPERTY_IDS",
    "SIZE_PROPERTY_IDS",
    "VARIANT_LABEL_MAX_LEN",
    "VARIANT_COLOR_MAX_LEN",
    "classify_variant_attribute",
    "variant_attribute_value",
    "extract_variant_color_size",
    "resolve_variant_dim",
    "variant_dim_value",
]
