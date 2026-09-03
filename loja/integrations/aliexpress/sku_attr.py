"""Helper puro/canônico de identidade de SKU para snapshot de compra (Fase 8.0.10.1).

Nenhuma dependência de models/db: recebe os atributos da variante e devolve a
string canônica ``pid:vid;pid:vid`` (sku_property_id: property_value_id),
ordenada de forma determinística.

Contrato:
* aceita apenas IDs numéricos;
* NÃO inventa identidade por labels visíveis (Color/Black etc.);
* ordenação determinística;
* retorno: ``pid:vid;pid:vid`` ou None se falta identidade completa.
"""

from __future__ import annotations

from typing import Mapping, Optional


def build_sku_attr(attributes) -> Optional[str]:
    """Converte IDs persistidos de propriedades em ``pid:vid;pid:vid``.

    Labels visíveis (Color/Black etc.) NÃO são suficientes para compra.
    Retorna None se qualquer atributo não trouxer identidade numérica.
    """
    if not isinstance(attributes, (list, tuple)) or not attributes:
        return None

    pairs = []
    for attr in attributes:
        if not isinstance(attr, Mapping):
            return None
        pid = str(attr.get("sku_property_id") or "").strip()
        vid = str(attr.get("property_value_id") or "").strip()
        if not pid or not vid or not pid.isdigit() or not vid.isdigit():
            return None
        pairs.append((int(pid), int(vid)))

    if not pairs:
        return None
    pairs.sort()
    return ";".join(f"{pid}:{vid}" for pid, vid in pairs)


__all__ = ["build_sku_attr"]
