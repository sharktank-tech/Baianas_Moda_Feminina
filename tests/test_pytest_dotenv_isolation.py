"""Confirma que a suíte não importa flags AliExpress do .env local."""

import io
import os

from dotenv import load_dotenv


def test_dotenv_does_not_inject_aliexpress_runtime_flags(monkeypatch, app):
    # app importa config.Config, que chama load_dotenv() no primeiro acesso.
    # A tentativa explícita com stream evita depender do .env real do runner.
    key = "ALIEXPRESS_SMOKE_BUYER_FREIGHT"
    monkeypatch.delenv(key, raising=False)
    assert load_dotenv(stream=io.StringIO(f"{key}=1\n")) is False
    assert key not in os.environ
