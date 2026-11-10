"""Loopback-only browser UI and JSON API."""

from __future__ import annotations

import json
import secrets
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

from callmap.store import GraphStore


def make_server(store: GraphStore, port: int) -> ThreadingHTTPServer:
    token = secrets.token_urlsafe(32)
    html = (Path(__file__).parent / "web" / "index.html").read_text()
    html = html.replace("__CALLMAP_TOKEN__", token)

    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            pass

        def send(self, payload, status=200, mime="application/json; charset=utf-8"):
            data = (
                payload.encode()
                if isinstance(payload, str)
                else json.dumps(payload, ensure_ascii=False).encode()
            )
            self.send_response(status)
            self.send_header("Content-Type", mime)
            self.send_header("Content-Length", str(len(data)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.end_headers()
            self.wfile.write(data)

        def valid_host(self):
            return self.headers.get("Host") in {
                f"127.0.0.1:{self.server.server_port}",
                f"localhost:{self.server.server_port}",
            }

        def do_GET(self):
            if not self.valid_host():
                self.send({"error": "Invalid host"}, 403)
                return
            parsed = urlparse(self.path)
            query = {key: values[0] for key, values in parse_qs(parsed.query).items()}
            try:
                if parsed.path == "/":
                    self.send(html, mime="text/html; charset=utf-8")
                elif parsed.path == "/api/stats":
                    self.send(store.stats())
                elif parsed.path == "/api/search":
                    self.send(
                        store.search(
                            query.get("q", ""),
                            **{
                                k: query[k]
                                for k in (
                                    "symbol_type",
                                    "path",
                                    "decorator",
                                    "route",
                                    "owner",
                                    "entry_kind",
                                )
                                if k in query
                            },
                        )
                    )
                elif parsed.path == "/api/references":
                    self.send(
                        store.references(
                            query.get("q", ""),
                            kind=query.get("kind"),
                            path=query.get("path"),
                            offset=int(query.get("offset", "0")),
                        )
                    )
                elif parsed.path == "/api/impact":
                    from callmap.impact import impact

                    self.send(
                        impact(
                            store,
                            base=query.get("base", "HEAD"),
                            offset=int(query.get("offset", "0")),
                        )
                    )
                elif parsed.path == "/api/entries":
                    self.send(
                        store.entries(
                            int(query.get("limit", "50")), int(query.get("offset", "0"))
                        )
                    )
                elif parsed.path == "/api/graph":
                    self.send(
                        store.expand(
                            query["symbol"],
                            int(query.get("depth", "2")),
                            int(query.get("max_nodes", "300")),
                            context_entry=query.get("context_entry"),
                        )
                    )
                elif parsed.path == "/api/node":
                    self.send(store.read_node(query["symbol"]))
                elif parsed.path == "/api/callers":
                    self.send(
                        store.callers(
                            query["symbol"],
                            int(query.get("depth", "1")),
                            int(query.get("max_nodes", "300")),
                            context_entry=query.get("context_entry"),
                        )
                    )
                elif parsed.path == "/api/context":
                    self.send(
                        {
                            "text": store.context(
                                query["symbol"],
                                int(query.get("budget", "12000")),
                                direction=query.get("direction", "downstream"),
                            )
                        }
                    )
                else:
                    self.send({"error": "Not found"}, 404)
            except (ValueError, KeyError, OSError) as exc:
                self.send({"error": str(exc)}, 400)

        def do_POST(self):
            if not self.valid_host() or self.headers.get("X-Callmap-Token") != token:
                self.send({"error": "Invalid request token"}, 403)
                return
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 0 or length > 1_000_000:
                    raise ValueError("请求体过大")
                body = json.loads(self.rfile.read(length))
                if self.path == "/api/annotate":
                    self.send(
                        store.annotate(
                            body["symbol"], body.get("doc", ""), body.get("targets")
                        )
                    )
                elif self.path == "/api/refresh":
                    self.send(store.stats(force=True))
                else:
                    self.send({"error": "Not found"}, 404)
            except (ValueError, KeyError, TypeError, OSError) as exc:
                self.send({"error": str(exc)}, 400)

    return ThreadingHTTPServer(("127.0.0.1", port), Handler)


def serve(store: GraphStore, port: int) -> None:
    server = make_server(store, port)
    print(f"Callmap: http://127.0.0.1:{server.server_port}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
