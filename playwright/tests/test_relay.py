"""Scores relay.py and wb.py against a fake Designer, without Chrome or the driver. Uses its own ports (RELAY_PORT 9770),
so the real relay is untouched. Rerun after any change to relay.py or wb.py; it must print 0 failures.

    python tests/test_relay.py"""
import base64, http.client, json, os, socket, struct, subprocess, sys, threading, time

PORT = 9770
os.environ["RELAY_PORT"] = str(PORT)
HERE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, HERE)
import wb  # noqa: E402

TOKEN_FILE = os.path.join(HERE, f".relay-token-{PORT}")
fails, count = [], 0


def check(name, ok, detail=""):
    global count
    count += 1
    print(("ok   " if ok else "FAIL ") + name + ("" if ok else f"  {str(detail)[:200]}"), flush=True)
    if not ok:
        fails.append(name)


def recvn(s, n):
    b = bytearray()
    while len(b) < n:
        x = s.recv(min(n - len(b), 65536))
        if not x:
            raise EOFError
        b += x
    return bytes(b)


def handshake(origin, token):
    s = socket.create_connection(("127.0.0.1", PORT + 1), timeout=5)
    key = base64.b64encode(os.urandom(16)).decode()
    o = f"Origin: {origin}\r\n" if origin else ""
    s.sendall((f"GET /?t={token} HTTP/1.1\r\nHost: 127.0.0.1\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n"
               f"Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n{o}\r\n").encode())
    r = b""
    while b"\r\n\r\n" not in r:
        x = s.recv(4096)
        if not x:
            break
        r += x
    return s, r.split(b"\r\n")[0].decode()


class Fake:
    """A Designer that answers with handler(message) and replies from its own thread."""

    def __init__(self, site, handler):
        self.sock, self.status = handshake(f"https://{site}.design.webflow.com", tokens["client"])
        self.sock.settimeout(None)
        self.handler, self.seen, self.lock = handler, [], threading.Lock()
        threading.Thread(target=self.loop, daemon=True).start()

    def send(self, text):
        p = text.encode()
        n, mask = len(p), os.urandom(4)
        head = b"\x81" + (bytes([0x80 | n]) if n < 126 else b"\xfe" + struct.pack(">H", n) if n < 65536
                          else b"\xff" + struct.pack(">Q", n))
        key = (mask * (n // 4 + 1))[:n]
        body = (int.from_bytes(p, "big") ^ int.from_bytes(key, "big")).to_bytes(n, "big")
        with self.lock:
            self.sock.sendall(head + mask + body)

    def loop(self):
        try:
            while True:
                a, b = recvn(self.sock, 2)
                n = b & 127
                if n == 126:
                    n = struct.unpack(">H", recvn(self.sock, 2))[0]
                elif n == 127:
                    n = struct.unpack(">Q", recvn(self.sock, 8))[0]
                m = json.loads(recvn(self.sock, n).decode())
                self.seen.append(m)
                threading.Thread(target=self.handler, args=(self, m), daemon=True).start()
        except (EOFError, OSError):
            pass

    def close(self):
        self.sock.close()


def echo(f, m):
    f.send(json.dumps({"id": m["id"], "ok": True, "v": m["arg"], "ms": 0}))


def raw_http(headers):
    c = http.client.HTTPConnection("127.0.0.1", PORT, timeout=5)
    c.putrequest("GET", "/", skip_host=True, skip_accept_encoding=True)
    for k, v in headers.items():
        c.putheader(k, v)
    c.endheaders()
    return c.getresponse().status


if os.path.exists(TOKEN_FILE):
    os.remove(TOKEN_FILE)
proc = subprocess.Popen([sys.executable, "-I", "-u", os.path.join(HERE, "relay.py")], cwd=HERE,
                        env=dict(os.environ, RELAY_PORT=str(PORT)), stdout=subprocess.DEVNULL)
try:
    for _ in range(50):
        try:
            wb.status()
            break
        except Exception:
            time.sleep(0.1)
    tokens = json.load(open(TOKEN_FILE))
    good = {"Host": f"127.0.0.1:{PORT}", "X-Token": tokens["control"]}

    # who may talk to it
    check("http: control token accepted", raw_http(good) == 200)
    check("http: no token refused", raw_http({"Host": f"127.0.0.1:{PORT}"}) == 403)
    check("http: client token refused", raw_http(dict(good, **{"X-Token": tokens["client"]})) == 403)
    check("http: browser Origin refused", raw_http(dict(good, Origin="https://evil.example")) == 403)
    check("http: rebinding Host refused", raw_http(dict(good, Host="evil.example")) == 403)
    for name, origin, tok in (("wrong token", "https://a.design.webflow.com", "wrong"),
                              ("control token as client", "https://a.design.webflow.com", tokens["control"]),
                              ("other origin", "https://evil.example", tokens["client"]),
                              ("lookalike origin", "https://a.design.webflow.com.evil.example", tokens["client"]),
                              ("plain http origin", "http://a.design.webflow.com", tokens["client"]),
                              ("nested subdomain is one site", "https://a.b.design.webflow.com", tokens["client"]),
                              ("no origin", None, tokens["client"])):
        s, st = handshake(origin, tok)
        s.close()
        if name == "nested subdomain is one site":
            continue
        check(f"socket refused: {name}", "403" in st, st)

    # no Designer yet
    r = wb.run("x", site="a", timeout=1)
    check("unknown site: down, not a timeout", r.get("down") and not r.get("timeout"), r)

    # one Designer
    a = Fake("site-a", echo)
    time.sleep(0.2)
    check("socket accepted for a Designer origin", "101" in a.status, a.status)
    check("no site named, one connected: uses it", wb.run("x", 1).get("v") == 1)
    check("URL and short name both resolve", wb.run("x", 2, "https://site-a.design.webflow.com/").get("v") == 2)
    for size in (10, 125, 126, 65535, 65536, 200000, 3000000):
        r = wb.run("x", "y" * size, "site-a", 30)
        check(f"round trip {size} bytes", r.get("ok") and len(r.get("v", "")) == size, str(r)[:120])
    check("control token never sent to the page", all(tokens["control"] not in json.dumps(m) for m in a.seen))

    # many at once, answered out of order
    def slow(f, m):
        time.sleep(0.3 * (1 - m["arg"] / 40))
        echo(f, m)
    a.handler = slow
    res, ts = {}, []
    for i in range(40):
        t = threading.Thread(target=lambda i=i: res.__setitem__(i, wb.run("x", i, "site-a", 10)))
        t.start()
        ts.append(t)
    [t.join() for t in ts]
    check("40 concurrent calls each get their own answer", all(res[i].get("v") == i for i in range(40)))

    # timeout blocks nothing else
    a.handler = lambda f, m: echo(f, m) if m["arg"] != "hang" else None
    t0 = time.time()
    box = {}
    th = threading.Thread(target=lambda: box.update(r=wb.run("x", "hang", "site-a", 1)))
    th.start()
    r = wb.run("x", "fast", "site-a", 5)
    check("other calls finish during a hung call", r.get("v") == "fast" and time.time() - t0 < 0.8, r)
    th.join()
    check("hung call times out as timeout", box["r"].get("timeout") and 0.9 < time.time() - t0 < 2, box["r"])
    check("timed-out work clears", wb.status()["in_flight"] == 0)

    # two Designers: never guess
    b = Fake("site-b", lambda f, m: f.send(json.dumps({"id": m["id"], "ok": True, "v": "from b", "ms": 0})))
    time.sleep(0.2)
    r = wb.run("x", 1)
    check("no site named, two connected: refused", r.get("down") and "NAME A SITE" in r.get("err", ""), r)
    check("calls reach the named site", wb.run("x", 1, "site-b").get("v") == "from b" and wb.run("x", 5, "site-a").get("v") == 5)

    # Designer goes away
    a.handler = lambda f, m: None
    box = {}
    th = threading.Thread(target=lambda: box.update(r=wb.run("x", 1, "site-a", 20)))
    t0 = time.time()
    th.start()
    time.sleep(0.3)
    a.close()
    th.join()
    check("disconnect fails the call at once", "DISCONNECTED" in box["r"].get("err", "") and time.time() - t0 < 2, box["r"])
    check("disconnect removes the site", "site-a" not in wb.status()["sites"] and "site-b" in wb.status()["sites"])

    # a reloaded Designer replaces the old socket
    c = Fake("site-b", lambda f, m: f.send(json.dumps({"id": m["id"], "ok": True, "v": "from new b", "ms": 0})))
    time.sleep(0.3)
    check("reconnect replaces the old socket", wb.run("x", 1, "site-b").get("v") == "from new b")
    check("old socket's close keeps the new one", "site-b" in wb.status()["sites"])

    # a script error and a dead relay
    c.handler = lambda f, m: f.send(json.dumps({"id": m["id"], "ok": False, "err": "boom", "ms": 0}))
    r = wb.run("x", 1, "site-b")
    check("script error comes back", r.get("ok") is False and r.get("err") == "boom" and not r.get("down"), r)
    old = json.load(open(TOKEN_FILE))
    proc.kill()
    proc.wait()
    r = wb.run("x", 1, "site-b", 2)
    check("relay gone: down, no exception", r.get("down"), r)
    proc = subprocess.Popen([sys.executable, "-I", "-u", os.path.join(HERE, "relay.py")], cwd=HERE,
                            env=dict(os.environ, RELAY_PORT=str(PORT)), stdout=subprocess.DEVNULL)
    time.sleep(0.6)
    check("tokens survive a relay restart", json.load(open(TOKEN_FILE)) == old)
finally:
    proc.kill()
    proc.wait()
    if os.path.exists(TOKEN_FILE):
        os.remove(TOKEN_FILE)

print(f"\n{count - len(fails)} of {count} passed, {len(fails)} failures", *fails, sep="\n  " if fails else " ")
sys.exit(1 if fails else 0)
