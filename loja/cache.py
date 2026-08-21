import threading
import time


_cache = {}
_lock = threading.Lock()
_DEFAULT_TTL = 300


def get_cached(key):
    """Retorna o valor em cache se ainda estiver válido, senão None."""
    item = _cache.get(key)
    if item is None:
        return None
    value, expires = item
    if time.monotonic() < expires:
        return value
    return None


def set_cached(key, value, ttl=_DEFAULT_TTL):
    """Armazena um valor no cache por `ttl` segundos (idempotente)."""
    with _lock:
        _cache[key] = (value, time.monotonic() + ttl)