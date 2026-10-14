"""Proxy de atraso (suporte aos testes de navegador): relay + atraso armado.

Puro stdlib: sobe origem e proxy locais, sem Playwright e sem o app.
"""
import os
import sys
import threading
import time
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "browser"))

from slow_proxy import SlowProxy


class _Origin(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def _ok(self):
        body = ("eco:" + self.path + ":").encode()
        if self.command == "POST":
            size = int(self.headers.get("Content-Length") or 0)
            if size:
                body += self.rfile.read(size)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    do_GET = do_POST = _ok

    def log_message(self, *args):
        pass


def test_relay_instantaneo_preserva_caminho_e_corpo():
    origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    proxy = SlowProxy(target_host="127.0.0.1",
                      target_port=origin.server_address[1]).start()
    try:
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": f"http://127.0.0.1:{proxy.port}"}))
        start = time.monotonic()
        assert opener.open("http://qualquer/r").read() == b"eco:/r:"
        assert time.monotonic() - start < 1.0
        req = urllib.request.Request("http://qualquer/post", data=b"acao=b")
        assert opener.open(req).read() == b"eco:/post:acao=b"
    finally:
        proxy.stop()
        origin.shutdown()


def test_caminho_armado_atrasa():
    origin = ThreadingHTTPServer(("127.0.0.1", 0), _Origin)
    threading.Thread(target=origin.serve_forever, daemon=True).start()
    proxy = SlowProxy(target_host="127.0.0.1",
                      target_port=origin.server_address[1]).start()
    try:
        proxy.delay_seconds = 1.0
        proxy.delay_paths.add("/lento")
        opener = urllib.request.build_opener(urllib.request.ProxyHandler(
            {"http": f"http://127.0.0.1:{proxy.port}"}))
        start = time.monotonic()
        assert opener.open("http://qualquer/lento").read() == b"eco:/lento:"
        assert time.monotonic() - start >= 1.0
    finally:
        proxy.stop()
        origin.shutdown()
