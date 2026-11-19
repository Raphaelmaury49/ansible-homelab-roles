#!/usr/bin/env python3
"""Mini serveur HTTPS de test (127.0.0.1 uniquement).

Usage : python3 -I server.py PORT CERT KEY STATE_DIR
Le comportement est lu a CHAQUE requete dans STATE_DIR/scenario.json :
  {"keys": SPEC, "sig": SPEC}  (SPEC pour <user>.keys et <user>.keys.sig)
SPEC : {"status": 200, "body_file": "chemin", "body": "texte", "sleep": 0,
        "headers": {"Location": "/x"}, "chunked": false, "size": N, "close": false}
 - size : corps de N octets ('a' + LF) ; chunked : sans Content-Length
 - close : ferme la connexion sans repondre
Chaque requete est ajoutee a STATE_DIR/requests.log ("GET /root.keys").
"""
import json
import os
import ssl
import sys
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

STATE = sys.argv[4]


class H(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"

    def log_message(self, *a):
        pass

    def do_GET(self):
        with open(os.path.join(STATE, "requests.log"), "a") as f:
            f.write("GET %s\n" % self.path)
        try:
            with open(os.path.join(STATE, "scenario.json")) as f:
                scn = json.load(f)
        except Exception:
            scn = {}
        spec = scn.get("sig" if self.path.endswith(".sig") else "keys", {"status": 404})
        try:
            self.respond(spec)
        except (BrokenPipeError, ConnectionResetError, ssl.SSLError, OSError):
            pass

    def respond(self, s):
        if s.get("close"):
            self.close_connection = True
            self.connection.close()
            return
        time.sleep(s.get("sleep", 0))
        if "size" in s:
            body = (b"a" * 63 + b"\n") * (s["size"] // 64 + 1)
            body = body[: s["size"]]
        elif "body_file" in s:
            with open(s["body_file"], "rb") as f:
                body = f.read()
        else:
            body = s.get("body", "").encode()
        self.send_response(s.get("status", 200))
        self.send_header("Content-Type", "text/plain")
        for k, v in s.get("headers", {}).items():
            self.send_header(k, v)
        if s.get("chunked"):
            self.send_header("Transfer-Encoding", "chunked")
            self.end_headers()
            for i in range(0, len(body), 1024):
                c = body[i : i + 1024]
                self.wfile.write(b"%x\r\n%s\r\n" % (len(c), c))
            self.wfile.write(b"0\r\n\r\n")
        else:
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
        self.wfile.flush()
        self.close_connection = True


def main():
    port, cert, key = int(sys.argv[1]), sys.argv[2], sys.argv[3]
    ctx = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    ctx.load_cert_chain(cert, key)
    srv = ThreadingHTTPServer(("127.0.0.1", port), H)
    srv.daemon_threads = True
    srv.socket = ctx.wrap_socket(srv.socket, server_side=True)
    srv.serve_forever()


if __name__ == "__main__":
    main()
