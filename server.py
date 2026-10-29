#!/usr/bin/env python3
"""Olympus server.

Serves the Olympus home screen, reports on the PC and the apps running on it,
and passes requests through to Navidrome (/nav/) and Jellyfin (/jf/).

Settings (environment variables):
  OLYMPUS_PORT    port Olympus listens on            (default 8097)
  NAVIDROME_URL   default http://127.0.0.1:4533
  JELLYFIN_URL    default http://127.0.0.1:8096
  ODYSSEUS_URL    default http://127.0.0.1:7011
  APOLLO_URL      default http://127.0.0.1:8090
  THEIA_URL       default http://127.0.0.1:8095
  MEDIA_DIR       default /srv/media
  NTFY_URL        where ntfy runs (Odysseus's)      (default http://127.0.0.1:8091)
  NTFY_TOPIC      the topic your phone subscribes to (default olympus)
  NTFY_TOKEN      access token, only if your ntfy needs one
"""
import glob
import http.server
import json
import os
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.request

PORT = int(os.environ.get("OLYMPUS_PORT", "8097"))
WEB_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "web")
MEDIA = os.environ.get("MEDIA_DIR", "/srv/media")
BACKENDS = {
    "/nav/": os.environ.get("NAVIDROME_URL", "http://127.0.0.1:4533").rstrip("/"),
    "/jf/": os.environ.get("JELLYFIN_URL", "http://127.0.0.1:8096").rstrip("/"),
}
SERVICES = {
    "odysseus": os.environ.get("ODYSSEUS_URL", "http://127.0.0.1:7011"),
    "apollo": os.environ.get("APOLLO_URL", "http://127.0.0.1:8090"),
    "theia": os.environ.get("THEIA_URL", "http://127.0.0.1:8095"),
    "cerberus": os.environ.get("CERBERUS_URL", "http://127.0.0.1:8222").rstrip("/") + "/alive",
    "budget": os.environ.get("BUDGET_URL", "http://127.0.0.1:5006").rstrip("/") + "/",
    "investments": os.environ.get("INVESTMENTS_URL", "http://127.0.0.1:3333").rstrip("/") + "/api/v1/health",
    "navidrome": BACKENDS["/nav/"] + "/ping",
    "jellyfin": BACKENDS["/jf/"] + "/System/Info/Public",
}
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8091").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "olympus")
CERBERUS_TS_PORT = os.environ.get("CERBERUS_TS_PORT", "7443")
_ts_host = {"name": None, "at": 0}


def ts_host():
    """This PC's Tailscale name, e.g. my-pc.tailnet.ts.net (cached for 10 minutes)."""
    if time.time() - _ts_host["at"] > 600:
        try:
            out = subprocess.run(["tailscale", "status", "--json"], capture_output=True, text=True, timeout=5).stdout
            _ts_host["name"] = json.loads(out)["Self"]["DNSName"].rstrip(".") or None
        except Exception:
            pass
        _ts_host["at"] = time.time()
    return _ts_host["name"]


BACKUP_STATUS = os.environ.get("BACKUP_STATUS", "/var/lib/homelab-backup/status.json")
NTFY_TOKEN = os.environ.get("NTFY_TOKEN", "")
NICE = {"odysseus": "Odysseus", "apollo": "Apollo", "theia": "Theia", "cerberus": "Cerberus (passwords)", "budget": "Plutus budget", "investments": "Plutus investments", "navidrome": "Navidrome (music library)", "jellyfin": "Jellyfin (film library)"}
HOP_BY_HOP = {"connection", "keep-alive", "proxy-authenticate", "proxy-authorization",
              "te", "trailers", "transfer-encoding", "upgrade", "host"}


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, *a, **k):
        return None


OPENER = urllib.request.build_opener(NoRedirect)


# ---------- PC facts ----------
def check(url):
    t = time.time()
    try:
        OPENER.open(urllib.request.Request(url, method="GET"), timeout=2.5).close()
    except urllib.error.HTTPError:
        pass                       # it answered, so it's up
    except Exception:
        return {"up": False}
    return {"up": True, "ms": round((time.time() - t) * 1000)}


def meminfo():
    vals = {}
    try:
        with open("/proc/meminfo") as f:
            for line in f:
                k, v = line.split(":", 1)
                vals[k] = int(v.split()[0]) * 1024
    except Exception:
        return None
    return {"total": vals.get("MemTotal", 0), "used": vals.get("MemTotal", 0) - vals.get("MemAvailable", 0)}


def cpu_temp():
    for hw in glob.glob("/sys/class/hwmon/hwmon*"):
        try:
            name = open(os.path.join(hw, "name")).read().strip()
        except Exception:
            continue
        if name in ("coretemp", "k10temp", "zenpower", "cpu_thermal"):
            try:
                return round(int(open(os.path.join(hw, "temp1_input")).read()) / 1000)
            except Exception:
                pass
    return None


_gpu = {"t": 0, "v": None}


def gpu():
    if time.time() - _gpu["t"] < 5:
        return _gpu["v"]
    _gpu["t"] = time.time()
    if not shutil.which("nvidia-smi"):
        return None
    try:
        out = subprocess.run(["nvidia-smi", "--query-gpu=name,utilization.gpu,temperature.gpu,memory.used,memory.total",
                              "--format=csv,noheader,nounits"], capture_output=True, text=True, timeout=3).stdout.strip().splitlines()[0]
        name, util, temp, mu, mt = [x.strip() for x in out.split(",")]
        _gpu["v"] = {"name": name, "util": int(util), "temp": int(temp), "memUsed": int(mu) * 1048576, "memTotal": int(mt) * 1048576}
    except Exception:
        _gpu["v"] = None
    return _gpu["v"]


_sizes = {"at": 0, "v": {}}


def folder_sizes_loop():
    while True:
        sizes = {}
        for sub in ("music", "movies", "shows"):
            total = 0
            for root, _, files in os.walk(os.path.join(MEDIA, sub)):
                for f in files:
                    try:
                        total += os.lstat(os.path.join(root, f)).st_size
                    except OSError:
                        pass
            sizes[sub] = total
        _sizes["v"], _sizes["at"] = sizes, time.time()
        time.sleep(900)


def system():
    du_path = MEDIA if os.path.exists(MEDIA) else "/"
    du = shutil.disk_usage(du_path)
    try:
        load = os.getloadavg()
    except OSError:
        load = (0, 0, 0)
    try:
        uptime = float(open("/proc/uptime").read().split()[0])
    except Exception:
        uptime = None
    return {
        "disk": {"path": du_path, "total": du.total, "used": du.used, "free": du.free},
        "folders": _sizes["v"],
        "load": load, "cpus": os.cpu_count(), "cpuTemp": cpu_temp(),
        "mem": meminfo(), "uptime": uptime, "gpu": gpu(),
    }


# ---------- phone alerts (through ntfy) ----------
_alerts = {"last": None, "ok": None}


def notify(title, message, tags="", priority="default", click=""):
    headers = {"Title": title.encode("utf-8"), "Priority": priority}
    if click:
        headers["Click"] = click
    if tags:
        headers["Tags"] = tags
    if NTFY_TOKEN:
        headers["Authorization"] = "Bearer " + NTFY_TOKEN
    try:
        req = urllib.request.Request(f"{NTFY_URL}/{NTFY_TOPIC}", data=message.encode("utf-8"), method="POST")
        for k, v in headers.items():
            req.add_header(k, v)
        urllib.request.urlopen(req, timeout=5).close()
        _alerts["ok"], _alerts["last"] = True, time.time()
        return True
    except Exception as e:
        _alerts["ok"] = False
        print("ntfy failed:", e, flush=True)
        return False


# ---------- Plutus: hand-updated balances (Moneybox LISA, Cash ISA) ----------
PLUTUS_CONF = os.environ.get("PLUTUS_CONF", os.path.join(os.path.dirname(os.path.abspath(__file__)), "plutus.json"))
_gf = {"jwt": None, "at": 0}


def plutus_conf():
    try:
        with open(PLUTUS_CONF) as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def gf_call(method, path, body=None):
    c = plutus_conf()
    if not c:
        raise RuntimeError("Plutus balances aren't set up")
    base = c.get("ghostfolio_url", "http://127.0.0.1:3333").rstrip("/")

    def raw(m, p, b=None, auth=True):
        req = urllib.request.Request(base + p, method=m, data=json.dumps(b).encode() if b is not None else None,
                                     headers={"Content-Type": "application/json"})
        if auth:
            req.add_header("Authorization", "Bearer " + _gf["jwt"])
        with urllib.request.urlopen(req, timeout=20) as r:
            data = r.read()
            return json.loads(data) if data else None

    if not _gf["jwt"] or time.time() - _gf["at"] > 3600:
        _gf["jwt"] = raw("POST", "/api/v1/auth/anonymous", {"accessToken": c["ghostfolio_token"]}, auth=False)["authToken"]
        _gf["at"] = time.time()
    return raw(method, path, body)


def plutus_balances():
    c = plutus_conf()
    if not c:
        return {"setup": False}
    ids = c.get("manual_accounts", [])
    accs = {a["id"]: a for a in gf_call("GET", "/api/v1/accounts")["accounts"]}
    out = []
    for i in ids:
        a = accs.get(i)
        if not a:
            continue
        last = None
        try:
            bal = gf_call("GET", f"/api/v1/accounts/{i}/balances").get("balances", [])
            last = max((b["date"] for b in bal), default=None)
        except Exception:
            pass
        out.append({"id": i, "name": a["name"], "currency": a.get("currency", "GBP"),
                    "balance": a.get("balance", 0), "updated": last})
    return {"setup": True, "accounts": out}


def plutus_reminder(state):
    """1st of each month, after 10am: one nudge to update the hand-kept balances."""
    now = time.localtime()
    key = time.strftime("%Y-%m", now)
    if now.tm_mday != 1 or now.tm_hour < 10 or state.get("month") == key or not plutus_conf():
        return
    state["month"] = key
    h = ts_host()
    notify("Plutus: monthly balance update", "Time to update your Moneybox LISA and Cash ISA balances. Tap to open.",
           "moneybag", click=f"https://{h}:9443/#balances" if h else "")


def watcher():
    time.sleep(90)                       # let everything finish starting after boot
    notify("PC is on", "Olympus is up. Everything is being checked.", "computer")
    fails = {k: 0 for k in SERVICES}
    down = set()
    disk_warned = 0
    reminded = {}
    while True:
        try:
            plutus_reminder(reminded)
        except Exception:
            pass
        for k, url in SERVICES.items():
            up = check(url)["up"]
            fails[k] = 0 if up else fails[k] + 1
            if fails[k] == 2 and k not in down:          # two misses in a row = really down
                down.add(k)
                notify(f"{NICE[k]} is down", f"{NICE[k]} stopped answering. Try: sudo systemctl restart {k}" if k not in ("odysseus", "cerberus", "budget", "investments") else f"{NICE[k]} stopped answering. Try: docker ps", "warning", "high")
            elif up and k in down:
                down.discard(k)
                notify(f"{NICE[k]} is back", f"{NICE[k]} is running again.", "white_check_mark")
        try:
            du = shutil.disk_usage(MEDIA if os.path.exists(MEDIA) else "/")
            if du.free / du.total < 0.08 and time.time() - disk_warned > 86400:
                disk_warned = time.time()
                notify("Storage nearly full", f"Only {du.free / 1e9:.0f} GB left for your media.", "floppy_disk", "high")
        except Exception:
            pass
        time.sleep(60)


# ---------- web server ----------
class Handler(http.server.SimpleHTTPRequestHandler):
    extensions_map = {**http.server.SimpleHTTPRequestHandler.extensions_map,
                      ".webmanifest": "application/manifest+json", ".js": "text/javascript"}

    def __init__(self, *a, **k):
        super().__init__(*a, directory=WEB_DIR, **k)

    def log_message(self, *a):
        pass

    def _backend(self):
        for prefix, url in BACKENDS.items():
            if self.path.startswith(prefix):
                return url + self.path[len(prefix) - 1:]
        return None

    def _json(self, obj):
        body = json.dumps(obj).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Content-Length", str(len(body)))
        super().end_headers()
        self.wfile.write(body)

    def end_headers(self):
        if not self._backend():
            self.send_header("Cache-Control", "no-cache")
        super().end_headers()

    def do_GET(self):
        if self.path == "/api/status":
            results = {}
            threads = [threading.Thread(target=lambda k=k, u=u: results.__setitem__(k, check(u))) for k, u in SERVICES.items()]
            [t.start() for t in threads]
            [t.join() for t in threads]
            return self._json(results)
        if self.path == "/api/system":
            return self._json(system())
        if self.path == "/api/plutus/balances":
            try:
                return self._json(plutus_balances())
            except Exception as e:
                return self._json({"setup": True, "error": str(e)[:200]})
        if self.path == "/api/links":
            h = ts_host()
            # Bitwarden only works over https, so Cerberus always opens on its Tailscale address.
            return self._json({
                "cerberus": f"https://{h}:{CERBERUS_TS_PORT}/" if h else None,
                "budget": f"https://{h}:5443/" if h else None,
                "investments": f"https://{h}:3443/" if h else None,
            })
        if self.path == "/api/backup":
            try:
                with open(BACKUP_STATUS) as f:
                    return self._json(json.load(f))
            except (OSError, ValueError):
                return self._json({"missing": True})
        if self.path == "/api/alerts":
            return self._json({"topic": NTFY_TOPIC, "ok": _alerts["ok"], "last": _alerts["last"]})
        if self._backend():
            return self._proxy()
        return super().do_GET()

    def do_POST(self):
        if self.path == "/api/plutus/balance":
            # Only from this PC itself or via Tailscale (which also arrives from 127.0.0.1), never straight from the LAN.
            if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
                self.send_error(403)
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                c = plutus_conf() or {}
                if body.get("id") not in c.get("manual_accounts", []):
                    self.send_error(403)
                    return
                bal = round(float(body["balance"]), 2)
                if bal < 0 or bal > 10_000_000:
                    raise ValueError("balance out of range")
                gf_call("POST", f"/api/v1/accounts/{body['id']}/balances",
                        {"balance": bal, "date": time.strftime("%Y-%m-%d")})
                return self._json({"ok": True, "balance": bal})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path == "/api/alerts/test":
            ok = notify("Test from Olympus", "Phone alerts are working.", "tada")
            return self._json({"ok": ok, "topic": NTFY_TOPIC})
        if self._backend():
            return self._proxy()
        self.send_error(405)

    def _proxy(self):
        length = int(self.headers.get("Content-Length") or 0)
        body = self.rfile.read(length) if length else None
        req = urllib.request.Request(self._backend(), data=body, method=self.command)
        for k, v in self.headers.items():
            if k.lower() not in HOP_BY_HOP:
                req.add_header(k, v)
        try:
            resp = OPENER.open(req, timeout=60)
        except urllib.error.HTTPError as err:
            resp = err
        except Exception:
            self.send_error(502, "Backend unreachable")
            return
        try:
            self.send_response(resp.status if hasattr(resp, "status") else resp.code)
            for k, v in resp.headers.items():
                if k.lower() not in HOP_BY_HOP and k.lower() not in ("server", "date"):
                    self.send_header(k, v)
            self.send_header("Connection", "close")
            super().end_headers()
            while True:
                chunk = resp.read(64 * 1024)
                if not chunk:
                    break
                self.wfile.write(chunk)
        except (BrokenPipeError, ConnectionResetError):
            pass
        finally:
            resp.close()


def main():
    threading.Thread(target=folder_sizes_loop, daemon=True).start()
    threading.Thread(target=watcher, daemon=True).start()
    server = http.server.ThreadingHTTPServer(("0.0.0.0", PORT), Handler)
    server.daemon_threads = True
    print(f"Olympus is up on http://localhost:{PORT}", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
