"""Proxy HTTP local (somente testes): atrasa caminhos armados.

O Chromium é lançado com `--proxy-server` apontando para cá; caminhos em
`delay_paths` dormem `delay_seconds` em threads próprias do proxy — sem
nenhum sleep em handler do Playwright (que congelaria o cliente de teste)
e sem chamada cross-thread em objetos de rota. Caminhos fora da lista
passam instantâneos.
"""
import http.client
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

_HOP_BY_HOP = ("host", "connection", "proxy-connection",
               "proxy-authenticate", "keep-alive", "transfer-encoding",
               "upgrade")


class SlowProxy:
    def __init__(self, target_host="127.0.0.1", target_port=5111):
        self.target_host = target_host
        self.target_port = target_port
        self.delay_paths = set()
        self.delay_seconds = 2.5
        self.hits = {}
        self.delayed = []
        handler = self._make_handler()
        self._server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.port = self._server.server_address[1]
        self._thread = threading.Thread(target=self._server.serve_forever,
                                        daemon=True)

    def _make_handler(self):
        proxy = self

        class Handler(BaseHTTPRequestHandler):
            protocol_version = "HTTP/1.1"

            def _relay(self):
                url = self.path
                if url.startswith("http://") or url.startswith("https://"):
                    path = "/" + url.split("/", 3)[-1]
                else:
                    path = url
                key = path.split("?", 1)[0]
                proxy.hits[key] = proxy.hits.get(key, 0) + 1
                if key in proxy.delay_paths:
                    proxy.delayed.append(key)
                    time.sleep(proxy.delay_seconds)
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length) if length else None
                fwd = {k: v for k, v in self.headers.items()
                       if k.lower() not in _HOP_BY_HOP}
                conn = http.client.HTTPConnection(proxy.target_host,
                                                  proxy.target_port,
                                                  timeout=30)
                conn.request(self.command, path, body=body, headers=fwd)
                resp = conn.getresponse()
                data = resp.read()
                self.send_response(resp.status, resp.reason)
                for key, value in resp.getheaders():
                    if key.lower() not in ("connection", "transfer-encoding",
                                           "content-length"):
                        self.send_header(key, value)
                self.send_header("Content-Length", str(len(data)))
                self.end_headers()
                self.wfile.write(data)
                conn.close()

            do_GET = do_POST = do_HEAD = do_PUT = do_DELETE = \
                do_PATCH = do_OPTIONS = _relay

            def log_message(self, *args):
                pass

        return Handler

    def start(self):
        self._thread.start()
        return self

    def stop(self):
        self._server.shutdown()
        self._thread.join(timeout=10)
