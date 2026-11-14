from __future__ import annotations

import argparse
from contextlib import contextmanager
from datetime import date
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
import json
import threading
from urllib.parse import parse_qs, urlparse

from .data import canonical, load_bundle, visible_snapshot
from .store import ArtifactStore, Conflict


class Server(ThreadingHTTPServer):
    daemon_threads = True


def make_server(bundle, store, host="127.0.0.1", port=0):
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, format, *args):
            return

        def respond(self, status, value):
            payload = canonical(value).encode()
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(payload)))
            self.end_headers()
            self.wfile.write(payload)

        def do_GET(self):
            target = urlparse(self.path)
            try:
                if target.path == "/health":
                    self.respond(200, {"status": "ok", "product": bundle["product"]})
                elif target.path == "/snapshot":
                    as_of = parse_qs(target.query).get("as_of", ["2026-01-01"])[0]
                    date.fromisoformat(as_of)
                    self.respond(200, visible_snapshot(bundle, as_of))
                elif target.path == "/policy":
                    self.respond(200, store.current_policy())
                elif target.path.startswith("/artifacts/"):
                    value = store.get(target.path.rsplit("/", 1)[-1])
                    self.respond(404 if value is None else 200, {"error": "成果不存在"} if value is None else value)
                else:
                    self.respond(404, {"error": "接口不存在"})
            except ValueError as error:
                self.respond(400, {"error": str(error)})

        def do_POST(self):
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if length < 1 or length > 4_000_000:
                    raise ValueError("请求大小不合适")
                value = json.loads(self.rfile.read(length))
                if self.path == "/artifacts":
                    artifact = value["artifact"]
                    expected = visible_snapshot(bundle, artifact["as_of"])
                    if expected["snapshot_id"] != artifact["snapshot_id"]:
                        raise ValueError("资料指纹与来源不一致")
                    self.respond(201, store.publish(value["request_key"], artifact, value["policy_revision"]))
                elif self.path == "/policy":
                    self.respond(200, store.update_policy(value["expected_revision"], value["policy"]))
                else:
                    self.respond(404, {"error": "接口不存在"})
            except Conflict as error:
                self.respond(409, {"error": str(error)})
            except (ValueError, KeyError, TypeError) as error:
                self.respond(400, {"error": str(error)})
    return Server((host, port), Handler)


@contextmanager
def running_service(bundle, store):
    server = make_server(bundle, store)
    worker = threading.Thread(target=server.serve_forever, daemon=True)
    worker.start()
    try:
        yield "http://127.0.0.1:" + str(server.server_address[1])
    finally:
        server.shutdown()
        server.server_close()
        worker.join(timeout=2)


def main():
    parser = argparse.ArgumentParser(description="启动本地资料与成果服务")
    parser.add_argument("--bundle", default="fixtures/bundle.json")
    parser.add_argument("--state", default="local-state.sqlite3")
    parser.add_argument("--port", type=int, default=8765)
    args = parser.parse_args()
    bundle = load_bundle(args.bundle)
    store = ArtifactStore(args.state, bundle.get("policy"))
    server = make_server(bundle, store, port=args.port)
    print("本地服务已启动", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        server.server_close()
        store.close()


if __name__ == "__main__":
    main()
