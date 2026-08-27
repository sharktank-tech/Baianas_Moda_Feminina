"""Smoke seguro do cliente AliExpress (APENAS validação local, sem chamadas reais).

Como usar:
    ALIEXPRESS_APP_KEY=... ALIEXPRESS_APP_SECRET=... \
    python scripts/smoke_aliexpress_client.py

O que faz:
    - Valida presença de credenciais no ambiente.
    - Constrói uma requisição assinada (método placeholder) e mostra um resumo
      SANITIZADO (app_key, base_url, método, e a presença de sign/token mascarados).
    - Não envia nada à rede e não imprime segredos, token, sign ou DATABASE_URL.

Para uma chamada read-only real, defina ALIEXPRESS_SMOKE_METHOD com um método
documentado read-only e ALIEXPRESS_SMOKE_ALLOW_NET=1 (ainda assim, nenhum dado
de escrita é executado pelo cliente nesta fase).
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

# Permite execução direta (python scripts/smoke_aliexpress_client.py)
_PROJECT_ROOT = Path(__file__).resolve().parent.parent
if str(_PROJECT_ROOT) not in sys.path:
    sys.path.insert(0, str(_PROJECT_ROOT))

from loja.integrations.aliexpress import (
    AliExpressClient,
    AliExpressConfigError,
    create_client_from_env,
)
from loja.integrations.aliexpress.sign import redact_params


def main() -> int:
    try:
        client = create_client_from_env()
    except AliExpressConfigError as exc:
        print(f"[smoke] config incompleta: {exc}", file=sys.stderr)
        return 2

    method = os.environ.get("ALIEXPRESS_SMOKE_METHOD", "aliexpress.smoke.placeholder")
    params = client._build_params(method)

    summary = {
        "base_url": client.base_url,
        "method": method,
        "app_key_present": bool(client.credentials.app_key),
        "access_token_present": client.credentials.has_token(),
        "sign_method": client.sign_method,
        "sign_present": "sign" in params and bool(params.get("sign")),
        "sanitized_params": redact_params(params),
    }
    print("[smoke] cliente AliExpress construído com sucesso:")
    print(jsonify(summary))

    if os.environ.get("ALIEXPRESS_SMOKE_ALLOW_NET") == "1":
        print(
            "[smoke] ALIEXPRESS_SMOKE_ALLOW_NET=1 definido, mas NENHUMA chamada de "
            "rede é feita nesta fase (endpoint read-only ainda não implementado).",
            file=sys.stderr,
        )
    return 0


def jsonify(obj) -> str:
    import json

    return json.dumps(obj, indent=2)


if __name__ == "__main__":
    raise SystemExit(main())
