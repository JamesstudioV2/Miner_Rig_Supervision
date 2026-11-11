"""A fake stock Antminer for tests: CGMiner API on TCP + web CGI with HTTP digest auth."""

import hashlib
import json
import socketserver
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

REALM = "antMiner Configuration"
NONCE = "a1b2c3d4e5f6"

STATS = {
    "STATUS": [{"STATUS": "S", "When": 1700000000, "Code": 70, "Msg": "BMMiner stats", "Description": "bmminer 1.0.0"}],
    "STATS": [
        {"BMMiner": "2.0.0 rwglr", "Miner": "uart_trans.1.3", "CompileTime": "Mon Jan 1", "Type": "Antminer S19j Pro"},
        {"STATS": 0, "ID": "BC50", "Elapsed": 5422, "GHS 5s": "104283.72", "GHS av": 104049.29, "miner_count": 3,
         "fan_num": 4, "fan1": 5400, "fan2": 5460, "fan3": 5400, "fan4": 0,
         "temp_num": 3, "temp1": 55, "temp2": 54, "temp3": 56, "temp2_1": 70, "temp2_2": 69, "temp2_3": 71,
         "temp_pcb1": "48-50-63-63", "temp_pcb2": "47-48-62-62", "temp_pcb3": "48-50-64-63", "temp_pcb4": "0-0-0-0",
         "temp_chip1": "63-65-78-78", "temp_chip2": "62-63-77-77", "temp_chip3": "63-65-79-78", "temp_chip4": "0-0-0-0",
         "total_rateideal": 104000.0, "rate_unit": "GH/s",
         "chain_acn1": 126, "chain_acn2": 126, "chain_acn3": 126, "chain_acn4": 0,
         "chain_acs1": " oooooooo oooooooo", "chain_acs2": " oooooxoo oooooooo", "chain_acs3": " oooooooo oooooooo",
         "chain_rate1": "34761.24", "chain_rate2": "34700.00", "chain_rate3": "34822.48"},
    ],
    "id": 1,
}
SUMMARY = {"STATUS": [{"STATUS": "S", "Msg": "Summary"}],
           "SUMMARY": [{"Elapsed": 5422, "GHS 5s": "104283.72", "GHS av": 104049.29, "Accepted": 1234, "Rejected": 2,
                        "Hardware Errors": 5}], "id": 1}
POOLS = {"STATUS": [{"STATUS": "S", "Msg": "1 Pool(s)"}],
         "POOLS": [{"POOL": 0, "URL": "stratum+tcp://pool.example.com:3333", "Status": "Alive", "Priority": 0,
                    "Stratum Active": True, "User": "worker.1", "Accepted": 1234, "Rejected": 2}], "id": 1}


def md5(s: str) -> str:
    return hashlib.md5(s.encode("utf-8")).hexdigest()


class FakeAntminer:
    def __init__(self, username="root", password="root", mode=0, string_mode_only=False, fan_error=True):
        self.username = username
        self.password = password
        self.string_mode_only = string_mode_only
        self.fan_error = fan_error
        self.conf = {
            "pools": [{"url": "stratum+tcp://pool.example.com:3333", "user": "worker.1", "pass": "x"},
                      {"url": "", "user": "", "pass": ""}, {"url": "", "user": "", "pass": ""}],
            "api-listen": True, "bitmain-fan-ctrl": False, "bitmain-fan-pwm": "100", "bitmain-freq-level": "100",
            "bitmain-work-mode": str(mode),
        }
        self.posts = []
        self.reboots = 0
        self.api_commands = []
        outer = self

        class ApiHandler(socketserver.BaseRequestHandler):
            def handle(self):
                data = b""
                while True:
                    chunk = self.request.recv(4096)
                    if not chunk:
                        break
                    data += chunk
                    try:
                        req = json.loads(data.decode())
                        break
                    except ValueError:
                        continue
                cmd = req.get("command")
                outer.api_commands.append(cmd)
                resp = {"stats": STATS, "summary": SUMMARY, "pools": POOLS}.get(cmd)
                if resp is None:
                    resp = {"STATUS": [{"STATUS": "E", "Msg": "Invalid command"}]}
                self.request.sendall(json.dumps(resp).encode() + b"\x00")

        class WebHandler(BaseHTTPRequestHandler):
            def log_message(self, *args):
                pass

            def _auth_ok(self):
                h = self.headers.get("Authorization") or ""
                if not h.startswith("Digest "):
                    return False
                p = urllib.request.parse_keqv_list(urllib.request.parse_http_list(h[7:]))
                ha1 = md5("%s:%s:%s" % (outer.username, REALM, outer.password))
                ha2 = md5("%s:%s" % (self.command, p.get("uri", "")))
                if p.get("qop") == "auth":
                    expected = md5("%s:%s:%s:%s:auth:%s" % (ha1, p["nonce"], p["nc"], p["cnonce"], ha2))
                else:
                    expected = md5("%s:%s:%s" % (ha1, p.get("nonce"), ha2))
                return p.get("username") == outer.username and p.get("response") == expected

            def _challenge(self):
                body = b"401 Unauthorized"
                self.send_response(401)
                self.send_header("WWW-Authenticate", 'Digest realm="%s", nonce="%s", qop="auth", algorithm=MD5'
                                 % (REALM, NONCE))
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def _json(self, obj):
                body = json.dumps(obj).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                self.wfile.write(body)

            def do_GET(self):
                if not self._auth_ok():
                    return self._challenge()
                if self.path == "/cgi-bin/get_miner_conf.cgi":
                    return self._json(outer.conf)
                if self.path == "/cgi-bin/summary.cgi":
                    status = [{"type": "rate", "status": "s", "code": 0, "msg": ""},
                              {"type": "network", "status": "s", "code": 0, "msg": ""}]
                    if outer.fan_error:
                        status.append({"type": "fans", "status": "e", "code": 2, "msg": "Fan lost"})
                    return self._json({"SUMMARY": [{"rate_5s": 104283.72, "rate_unit": "GH/s", "status": status}]})
                if self.path == "/cgi-bin/reboot.cgi":
                    outer.reboots += 1
                    return self._json({})
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

            def do_POST(self):
                length = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(length)
                if not self._auth_ok():
                    return self._challenge()
                if self.path == "/cgi-bin/set_miner_conf.cgi":
                    payload = json.loads(body.decode())
                    outer.posts.append(payload)
                    mode = payload.get("miner-mode")
                    if not (outer.string_mode_only and not isinstance(mode, str)):
                        outer.conf["bitmain-work-mode"] = str(int(mode))
                    return self._json({"stats": "success", "code": "M000", "msg": "OK!"})
                self.send_response(404)
                self.send_header("Content-Length", "0")
                self.end_headers()

        socketserver.ThreadingTCPServer.allow_reuse_address = True
        self.api_server = socketserver.ThreadingTCPServer(("127.0.0.1", 0), ApiHandler)
        self.api_server.daemon_threads = True
        self.web_server = ThreadingHTTPServer(("127.0.0.1", 0), WebHandler)
        self.web_server.daemon_threads = True
        self.api_port = self.api_server.server_address[1]
        self.web_port = self.web_server.server_address[1]

    def start(self):
        for srv in (self.api_server, self.web_server):
            threading.Thread(target=srv.serve_forever, daemon=True).start()
        return self

    def stop(self):
        for srv in (self.api_server, self.web_server):
            srv.shutdown()
            srv.server_close()

    def miner_cfg(self, password=None):
        return {"id": "m1", "name": "S19-test", "host": "127.0.0.1", "type": "antminer", "api_port": self.api_port,
                "web_port": self.web_port, "username": self.username,
                "password": self.password if password is None else password, "enabled": True}
