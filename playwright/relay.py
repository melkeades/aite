"""Local relay between scripts and open Webflow Designers. Playwright is not in this path, so Designer API calls keep
working while the driver is busy, stuck or restarting, and a hung call times out without blocking anything else.

    script (wb.py) --HTTP--> relay.py <--WebSocket-- Designer page (relay.js) --postMessage--> MCP Bridge App frame

Stdlib only. `open_site()` starts it when it isn't running; `python relay.py` also works. See README "Relay".

Security: binds to 127.0.0.1 only. The token file .relay-token-<port> holds two tokens and survives restarts, so open
Designers reconnect by themselves. `control` runs code in a Designer: it stays in that file and never reaches a page.
`client` only lets a page connect: it is embedded in the page script, and the socket also requires a Designer Origin.
HTTP requests with an Origin header (web pages) or a non-localhost Host (DNS rebinding) are refused."""
import base64, hashlib, hmac, itertools, json, os, secrets, socket, struct, threading, time
from http.server import ThreadingHTTPServer, BaseHTTPRequestHandler

HERE = os.path.dirname(os.path.abspath(__file__))
HTTP_PORT = int(os.environ.get("RELAY_PORT", 8770))
WS_PORT = HTTP_PORT + 1
TOKEN_FILE = os.path.join(HERE, f".relay-token-{HTTP_PORT}")
DESIGN = ".design.webflow.com"
HOSTS = (f"127.0.0.1:{HTTP_PORT}", f"localhost:{HTTP_PORT}")
GUID = "258EAFA5-E914-47DA-95CA-C5AB0DC85B11"
MAX_TIMEOUT, MAX_BODY = 300, 5_000_000


def load_tokens():
    if os.path.exists(TOKEN_FILE):
        return json.load(open(TOKEN_FILE))
    t = {"client": secrets.token_urlsafe(24), "control": secrets.token_urlsafe(32)}
    json.dump(t, open(TOKEN_FILE, "w"))
    return t


TOKENS = load_tokens()
clients = {}  # short name -> Client
pending = {}  # call id -> {"done": Event, "reply": dict, "site": short name}
guard = threading.Lock()
ids = itertools.count(1)


def recvn(c, n):
    b = bytearray()
    while len(b) < n:
        x = c.recv(min(n - len(b), 65536))
        if not x:
            raise EOFError
        b += x
    return bytes(b)


def unmask(data, mask):
    if not data:
        return data
    key = (mask * (len(data) // 4 + 1))[:len(data)]
    return (int.from_bytes(data, "big") ^ int.from_bytes(key, "big")).to_bytes(len(data), "big")


class Client:
    def __init__(self, sock, site):
        self.sock, self.site, self.since, self.lock = sock, site, time.time(), threading.Lock()

    def send(self, text):
        p = text.encode("utf-8")
        n = len(p)
        head = b"\x81" + (bytes([n]) if n < 126 else b"\x7e" + struct.pack(">H", n) if n < 65536
                          else b"\x7f" + struct.pack(">Q", n))
        with self.lock:
            self.sock.sendall(head + p)

    def read_loop(self):
        buf = b""
        while True:
            a, b = recvn(self.sock, 2)
            op, n = a & 15, b & 127
            if n == 126:
                n = struct.unpack(">H", recvn(self.sock, 2))[0]
            elif n == 127:
                n = struct.unpack(">Q", recvn(self.sock, 8))[0]
            mask = recvn(self.sock, 4) if b & 128 else b"\0\0\0\0"
            data = unmask(recvn(self.sock, n), mask)
            if op == 8:
                return
            if op in (0, 1, 2):
                buf += data
                if a & 128:
                    m = json.loads(buf.decode("utf-8"))
                    buf = b""
                    slot = pending.get(m.get("id"))
                    if slot:
                        slot["reply"] = m
                        slot["done"].set()


def serve_client(c):
    client = None
    try:
        req = b""
        while b"\r\n\r\n" not in req:
            x = c.recv(4096)
            if not x or len(req) > 16384:
                return
            req += x
        lines = req.decode("latin1").split("\r\n")
        h = {l.split(":", 1)[0].lower(): l.split(":", 1)[1].strip() for l in lines[1:] if ":" in l}
        origin, target = h.get("origin", ""), lines[0].split(" ")[1] if " " in lines[0] else ""
        given = target.split("t=", 1)[1].split("&")[0] if "t=" in target else ""
        host = origin.split("//", 1)[-1]
        if not (origin.startswith("https://") and host.endswith(DESIGN) and hmac.compare_digest(given, TOKENS["client"])):
            c.sendall(b"HTTP/1.1 403 Forbidden\r\nContent-Length: 0\r\n\r\n")
            return
        accept = base64.b64encode(hashlib.sha1((h["sec-websocket-key"] + GUID).encode()).digest()).decode()
        c.sendall(("HTTP/1.1 101 Switching Protocols\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
                   f"Sec-WebSocket-Accept: {accept}\r\n\r\n").encode())
        client = Client(c, host[:-len(DESIGN)])
        with guard:
            old = clients.get(client.site)
            clients[client.site] = client
        if old:
            try:
                old.sock.close()
            except OSError:
                pass
        print("connected", client.site, flush=True)
        client.read_loop()
    except (EOFError, OSError, ValueError, KeyError):
        pass
    finally:
        try:
            c.close()
        except OSError:
            pass
        if client:
            with guard:
                if clients.get(client.site) is client:
                    del clients[client.site]
                    for slot in pending.values():  # fail at once instead of waiting out each timeout
                        if slot["site"] == client.site:
                            slot["reply"] = {"ok": False, "err": "DESIGNER DISCONNECTED during the call"}
                            slot["done"].set()
            print("disconnected", client.site, flush=True)


def accept_loop():
    s = socket.socket()
    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
    s.bind(("127.0.0.1", WS_PORT))
    s.listen(16)
    while True:
        c, _ = s.accept()
        threading.Thread(target=serve_client, args=(c,), daemon=True).start()


class H(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def reply(self, body, code=200):
        data = json.dumps(body, default=str).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(data)))
        self.end_headers()
        self.wfile.write(data)

    def denied(self):
        if "Origin" in self.headers:
            return "request comes from a browser (Origin header)"
        if self.headers.get("Host") not in HOSTS:
            return "Host is not localhost"
        if not hmac.compare_digest(self.headers.get("X-Token", "").encode(), TOKENS["control"].encode()):
            return "missing or wrong X-Token"
        return None

    def do_GET(self):
        why = self.denied()
        if why:
            return self.reply({"ok": False, "error": "FORBIDDEN: " + why}, 403)
        now = time.time()
        self.reply({"ok": True, "pid": os.getpid(), "sites": {k: round(now - c.since) for k, c in list(clients.items())},
                    "in_flight": len(pending)})

    def do_POST(self):
        size = int(self.headers.get("Content-Length") or 0)
        if not 0 <= size <= MAX_BODY:
            self.close_connection = True
            return self.reply({"ok": False, "error": "FORBIDDEN: body too large"}, 403)
        body = self.rfile.read(size)
        why = self.denied()
        if why:
            return self.reply({"ok": False, "error": "FORBIDDEN: " + why}, 403)
        q = json.loads(body.decode("utf-8"))
        site = q.get("site")
        if not site:
            names = list(clients)
            if len(names) != 1:
                return self.reply({"ok": False, "down": True, "err": f"NAME A SITE: connected Designers: {names}"})
            site = names[0]
        client = clients.get(site)
        if not client:
            return self.reply({"ok": False, "down": True, "err": f"DESIGNER NOT CONNECTED: {site}; connected: {list(clients)}"})
        timeout = max(0.1, min(float(q.get("timeout", 30)), MAX_TIMEOUT))
        i, t0 = next(ids), time.time()
        slot = {"done": threading.Event(), "reply": None, "site": site}
        pending[i] = slot
        try:
            client.send(json.dumps({"id": i, "code": q["code"], "arg": q.get("arg")}))
            finished = slot["done"].wait(timeout)
        except OSError:
            finished, slot["reply"] = True, {"ok": False, "down": True, "err": "DESIGNER DISCONNECTED"}
        finally:
            pending.pop(i, None)
        out = slot["reply"] if finished else {"ok": False, "timeout": True, "err": f"TIMEOUT after {timeout}s"}
        self.reply(dict(out, site=site, rt=round((time.time() - t0) * 1000, 1)))


if __name__ == "__main__":
    server = ThreadingHTTPServer(("127.0.0.1", HTTP_PORT), H)  # first, so a second copy fails before touching anything
    threading.Thread(target=accept_loop, daemon=True).start()
    print(f"relay ready: http {HTTP_PORT}, ws {WS_PORT}", flush=True)
    server.serve_forever()
