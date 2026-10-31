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
  THEIA_DIR       where Theia is installed (for the inbox)  (default ~/theia)
  APOLLO_DIR      where Apollo is installed (for the inbox) (default ~/apollo)
"""
import glob
import http.server
import json
import os
import re
import shutil
import subprocess
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

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
    "mnemosyne": os.environ.get("MNEMOSYNE_URL", "http://127.0.0.1:2283").rstrip("/") + "/api/server/ping",
    "clio": os.environ.get("CLIO_URL", "http://127.0.0.1:13378").rstrip("/") + "/ping",
    "aegis": os.environ.get("AEGIS_URL", "http://127.0.0.1:3080").rstrip("/") + "/",
    "athena": os.environ.get("ATHENA_URL", "http://127.0.0.1:8010").rstrip("/") + "/",
    "hermes": os.environ.get("HERMES_URL", "http://127.0.0.1:8081").rstrip("/") + "/status.php",
    "hestia": os.environ.get("HESTIA_URL", "http://127.0.0.1:8123").rstrip("/") + "/manifest.json",
}
NTFY_URL = os.environ.get("NTFY_URL", "http://127.0.0.1:8091").rstrip("/")
NTFY_TOPIC = os.environ.get("NTFY_TOPIC", "olympus")
CERBERUS_TS_PORT = os.environ.get("CERBERUS_TS_PORT", "7443")
AEGIS_CONF = os.path.expanduser(os.environ.get("AEGIS_CONF", "~/.config/olympus/aegis.json"))
HESTIA_CONF = os.path.expanduser(os.environ.get("HESTIA_CONF", "~/.config/olympus/hestia.json"))


def installed_services():
    """SERVICES, minus apps that aren't installed on this PC (Mnemosyne is optional)."""
    optional = {"mnemosyne": os.environ.get("MNEMOSYNE_DIR", "/opt/mnemosyne"), "clio": os.environ.get("CLIO_DIR", "/opt/clio"),
                "athena": os.environ.get("ATHENA_DIR", "/opt/athena"), "hermes": os.environ.get("HERMES_DIR", "/opt/hermes"),
                "hestia": os.environ.get("HESTIA_DIR", "/opt/hestia")}
    have = {k: os.path.exists(d + "/docker-compose.yml") for k, d in optional.items()}
    have["aegis"] = os.path.exists(AEGIS_CONF)
    return {k: u for k, u in SERVICES.items() if have.get(k, True)}
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
NICE = {"odysseus": "Odysseus", "apollo": "Apollo", "theia": "Theia", "cerberus": "Cerberus (passwords)", "budget": "Plutus budget", "investments": "Plutus investments", "navidrome": "Navidrome (music library)", "jellyfin": "Jellyfin (film library)", "mnemosyne": "Mnemosyne (photos)", "clio": "Clio (books)", "aegis": "Aegis (ad blocking)", "athena": "Athena (documents)", "hermes": "Hermes (drive)", "hestia": "Hestia (smart home)"}
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
        compute_sizes()
        time.sleep(900)


def compute_sizes():
    sizes = {}
    purge_cleared()
    for sub in ("music", "movies", "shows", "photos", "books", "documents", "inbox", ".cleared"):
        total = 0
        for root, _, files in os.walk(os.path.join(MEDIA, sub)):
            for f in files:
                try:
                    total += os.lstat(os.path.join(root, f)).st_size
                except OSError:
                    pass
        sizes[sub] = total
    _sizes["v"], _sizes["at"] = sizes, time.time()


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
        for k, url in installed_services().items():
            up = check(url)["up"]
            fails[k] = 0 if up else fails[k] + 1
            if fails[k] == 2 and k not in down:          # two misses in a row = really down
                down.add(k)
                notify(f"{NICE[k]} is down", f"{NICE[k]} stopped answering. Try: sudo systemctl restart {k}" if k not in ("odysseus", "cerberus", "budget", "investments", "mnemosyne", "clio", "aegis", "athena", "hermes", "hestia") else f"{NICE[k]} stopped answering. Try: docker ps", "warning", "high")
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


# ---------- drop box: upload films and music into the inbox, then file them ----------
INBOX = os.path.join(MEDIA, "inbox")
UPLOADING = os.path.join(INBOX, ".uploading")
TIDY_SCRIPTS = [
    os.path.join(os.path.expanduser(os.environ.get("THEIA_DIR", "~/theia")), "tidy.py"),
    os.path.join(os.path.expanduser(os.environ.get("APOLLO_DIR", "~/apollo")), "tidy-music.py"),
    os.path.join(os.path.expanduser(os.environ.get("CLIO_DIR_HOME", "~/clio")), "tidy-books.py"),
]
DROP_TYPES = {".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".mpg", ".mpeg", ".flv",
              ".mp3", ".flac", ".m4a", ".aac", ".ogg", ".oga", ".opus", ".wav", ".wma", ".aiff", ".aif", ".ape", ".wv",
              ".srt", ".ass", ".ssa", ".vtt", ".sub", ".idx", ".jpg", ".jpeg", ".png", ".webp",
              ".m4b", ".epub", ".pdf", ".mobi", ".azw3", ".azw", ".cbz", ".cbr", ".fb2", ".tif", ".tiff", ".gif"}
_admins = {}   # (u, t, s) -> time it was checked


def is_admin(u, t, s):
    """True if these Navidrome login details belong to an admin (checked with Navidrome itself)."""
    if not (u and t and s):
        return False
    key = (u, t, s)
    if time.time() - _admins.get(key, 0) < 600:
        return True
    q = urllib.parse.urlencode({"u": u, "t": t, "s": s, "v": "1.16.1", "c": "olympus", "f": "json", "username": u})
    try:
        with urllib.request.urlopen(f"{BACKENDS['/nav/']}/rest/getUser.view?{q}", timeout=8) as r:
            resp = json.load(r).get("subsonic-response", {})
        ok = resp.get("status") == "ok" and resp.get("user", {}).get("adminRole") is True
    except Exception:
        ok = False
    if ok:
        _admins[key] = time.time()
    return ok


def safe_relpath(raw):
    """Turn 'Album/CD1/01 Song.flac' from the browser into a safe path inside the inbox, or None."""
    parts = []
    for part in urllib.parse.unquote(raw or "").replace("\\", "/").split("/"):
        part = part.strip()
        if part in ("", "."):
            continue
        if part.startswith("."):                                # "..", hidden files and folders: refuse
            return None
        part = "".join(c for c in part if c >= " " and c not in '<>:"|?*').strip().rstrip(".")
        if not part:
            return None
        parts.append(part[:150])
    if not parts or len(parts) > 5 or os.path.splitext(parts[-1])[1].lower() not in DROP_TYPES:
        return None
    return os.path.join(*parts)


def clear_stale_uploads():
    try:
        for f in os.listdir(UPLOADING):
            fp = os.path.join(UPLOADING, f)
            if time.time() - os.path.getmtime(fp) > 86400:
                os.remove(fp)
    except OSError:
        pass


def run_tidy():
    """File what's in the inbox now (films with Theia's script, music with Apollo's). Returns their output lines."""
    lines = []
    env = {**os.environ, "NTFY_URL": NTFY_URL, "NTFY_TOPIC": NTFY_TOPIC, "NTFY_TOKEN": NTFY_TOKEN}
    for script in TIDY_SCRIPTS:
        if not os.path.exists(script):
            continue
        try:
            out = subprocess.run(["python3", script, "--media", MEDIA, "--quiet"], capture_output=True,
                                 text=True, timeout=900, env=env)
            lines += [l for l in (out.stdout + out.stderr).splitlines() if l.strip()]
        except Exception as e:
            lines.append(f"· {os.path.basename(script)} had a problem: {str(e)[:120]}")
    return lines


# ---------- storage helper: "clear out" watched films, with 30 days to change your mind ----------
CLEARED = os.path.join(MEDIA, ".cleared")
CLEARED_INDEX = os.path.join(CLEARED, "index.json")
KEEP_CLEARED_DAYS = 30
_clear_lock = threading.Lock()


def _cleared():
    try:
        with open(CLEARED_INDEX) as f:
            return json.load(f)
    except (OSError, ValueError):
        return []


def _save_cleared(items):
    os.makedirs(CLEARED, exist_ok=True)
    tmp = CLEARED_INDEX + ".tmp"
    with open(tmp, "w") as f:
        json.dump(items, f, indent=1)
    os.replace(tmp, CLEARED_INDEX)


def _size(path):
    if os.path.isfile(path):
        return os.path.getsize(path)
    total = 0
    for root, _, files in os.walk(path):
        for f in files:
            try: total += os.lstat(os.path.join(root, f)).st_size
            except OSError: pass
    return total


def clear_target(item_path):
    """Which folder to move for a film file or episode file Jellyfin reported. None if it's not safely ours."""
    real = os.path.realpath(item_path or "")
    movies, shows = os.path.realpath(os.path.join(MEDIA, "movies")), os.path.realpath(os.path.join(MEDIA, "shows"))
    if not os.path.exists(real):
        return None
    VIDEO = (".mkv", ".mp4", ".m4v", ".avi", ".mov", ".webm", ".wmv", ".ts", ".mpg", ".mpeg")
    folder = os.path.dirname(real)
    if real.startswith(movies + os.sep):
        if folder == movies:                       # a film sitting loose in movies/: just that file
            return real
        videos = [f for f in os.listdir(folder) if f.lower().endswith(VIDEO)]
        return folder if len(videos) == 1 else real   # its own folder (with subtitles, posters) if it's alone in it
    if real.startswith(shows + os.sep) and len(os.path.relpath(real, shows).split(os.sep)) == 3:
        return folder                              # shows/<Show>/<Season xx>/<episode> -> that season's folder
    return None


def clear_out(item_path, title):
    target = clear_target(item_path)
    if not target:
        raise ValueError("That isn't in your films or shows folders")
    with _clear_lock:
        items = _cleared()
        cid = uuid.uuid4().hex[:12]
        os.makedirs(CLEARED, exist_ok=True)
        size = _size(target)
        shutil.move(target, os.path.join(CLEARED, cid))
        parent = os.path.dirname(target)                 # a show with no seasons left: remove its empty folder
        if parent.startswith(os.path.realpath(os.path.join(MEDIA, "shows")) + os.sep) and not os.listdir(parent):
            os.rmdir(parent)
        items.append({"id": cid, "title": title or os.path.basename(target), "from": target, "size": size, "at": time.time()})
        _save_cleared(items)
    return {"id": cid, "size": size}


def put_back(cid):
    with _clear_lock:
        items = _cleared()
        it = next((i for i in items if i["id"] == cid), None)
        if not it:
            raise ValueError("Not found")
        if os.path.exists(it["from"]):
            raise ValueError("Something with that name is already back in its place")
        os.makedirs(os.path.dirname(it["from"]), exist_ok=True)
        shutil.move(os.path.join(CLEARED, cid), it["from"])
        _save_cleared([i for i in items if i["id"] != cid])
    return it


def _remove(path):
    if os.path.isdir(path) and not os.path.islink(path):
        shutil.rmtree(path, ignore_errors=True)
    elif os.path.lexists(path):
        os.remove(path)


def delete_now(cid):
    with _clear_lock:
        items = _cleared()
        if not any(i["id"] == cid for i in items) or not re.fullmatch(r"[0-9a-f]{12}", cid):
            raise ValueError("Not found")
        _remove(os.path.join(CLEARED, cid))
        _save_cleared([i for i in items if i["id"] != cid])


def purge_cleared():
    """Things cleared out more than 30 days ago are deleted for good."""
    try:
        with _clear_lock:
            items = _cleared()
            keep = [i for i in items if time.time() - i["at"] < KEEP_CLEARED_DAYS * 86400]
            for i in items:
                if i not in keep and re.fullmatch(r"[0-9a-f]{12}", i["id"]):
                    _remove(os.path.join(CLEARED, i["id"]))
            if len(keep) != len(items):
                _save_cleared(keep)
    except OSError:
        pass


# ---------- your stats: monthly snapshots of song play counts (Navidrome only keeps all-time totals) ----------
STATS_DIR = os.path.expanduser("~/.config/olympus/stats")


def stats_bases():
    """The first play-count snapshot of this month and of this year (or the earliest we have)."""
    month, year = time.strftime("%Y-%m"), time.strftime("%Y")
    try:
        files = sorted(f for f in os.listdir(STATS_DIR) if re.fullmatch(r"\d{4}-\d{2}\.json", f))
    except OSError:
        files = []

    def load(name):
        try:
            with open(os.path.join(STATS_DIR, name)) as f:
                return json.load(f)
        except (OSError, ValueError):
            return None
    this_year = [f for f in files if f.startswith(year)]
    return {"month": load(f"{month}.json"), "year": load(this_year[0]) if this_year else None,
            "needSnapshot": f"{month}.json" not in files}


def save_snapshot(counts):
    os.makedirs(STATS_DIR, exist_ok=True)
    name = os.path.join(STATS_DIR, time.strftime("%Y-%m") + ".json")
    if os.path.exists(name):
        return False
    clean = {str(k)[:64]: int(v) for k, v in list(counts.items())[:200000] if isinstance(v, (int, float))}
    with open(name, "w") as f:
        json.dump({"taken": time.time(), "counts": clean}, f)
    return True


# ---------- addresses: every app, on this PC and over Tailscale (read from Tailscale itself) ----------
ADDRESS_APPS = [  # key, name, what it is, local URL
    ("olympus", "Olympus", "Home screen", f"http://127.0.0.1:{PORT}"),
    ("apollo", "Apollo", "Music", SERVICES["apollo"]),
    ("theia", "Theia", "Films & shows", SERVICES["theia"]),
    ("odysseus", "Odysseus", "AI workspace", SERVICES["odysseus"]),
    ("cerberus", "Cerberus", "Passwords", os.environ.get("CERBERUS_URL", "http://127.0.0.1:8222")),
    ("mnemosyne", "Mnemosyne", "Photos", os.environ.get("MNEMOSYNE_URL", "http://127.0.0.1:2283")),
    ("clio", "Clio", "Books & podcasts", os.environ.get("CLIO_URL", "http://127.0.0.1:13378")),
    ("athena", "Athena", "Documents", os.environ.get("ATHENA_URL", "http://127.0.0.1:8010")),
    ("hermes", "Hermes", "Your Drive", os.environ.get("HERMES_URL", "http://127.0.0.1:8081")),
    ("hestia", "Hestia", "Smart home", os.environ.get("HESTIA_URL", "http://127.0.0.1:8123")),
    ("aegis", "Aegis", "Ad blocking (admin page)", os.environ.get("AEGIS_URL", "http://127.0.0.1:3080")),
    ("budget", "Plutus budget", "Budget", os.environ.get("BUDGET_URL", "http://127.0.0.1:5006")),
    ("investments", "Plutus investments", "Investments", os.environ.get("INVESTMENTS_URL", "http://127.0.0.1:3333")),
    ("navidrome", "Navidrome", "Music library admin (this PC)", BACKENDS["/nav/"]),
    ("jellyfin", "Jellyfin", "Film library admin (this PC)", BACKENDS["/jf/"]),
    ("ntfy", "ntfy", "Phone alerts server", NTFY_URL),
]
TS_DEFAULTS = {"hestia": 9123, "hermes": 7843, "athena": 8843, "aegis": 4553, "clio": 4443, "mnemosyne": 6443, "olympus": 9443, "apollo": 443, "theia": 8443, "odysseus": 10000, "cerberus": int(CERBERUS_TS_PORT),
               "budget": 5443, "investments": 3443}
_addr = {"at": 0, "v": None}


def _port(url):
    m = re.search(r":(\d+)", url.split("//", 1)[-1])
    return int(m.group(1)) if m else 80


def served_ports():
    """{local port: tailscale https port} from `tailscale serve status`, or None if it can't be read."""
    try:
        out = subprocess.run(["tailscale", "serve", "status", "--json"], capture_output=True, text=True, timeout=5)
        data = json.loads(out.stdout or "null")
    except Exception:
        return None
    if not isinstance(data, dict):
        return None
    found = {}
    for hostport, conf in (data.get("Web") or {}).items():
        ts_port = int(hostport.rsplit(":", 1)[-1])
        for h in (conf.get("Handlers") or {}).values():
            proxy = h.get("Proxy") or ""
            m = re.search(r":(\d+)/?$", proxy) or re.fullmatch(r"(\d+)", proxy)
            if m:
                found[int(m.group(1))] = ts_port
    return found


def addresses():
    if _addr["v"] and time.time() - _addr["at"] < 60:
        return _addr["v"]
    host, served = ts_host(), served_ports()
    rows = []
    for key, name, what, url in ADDRESS_APPS:
        if key in ("mnemosyne", "clio", "aegis", "athena", "hermes", "hestia") and key not in installed_services():
            continue
        local_port = _port(url)
        ts_port = served.get(local_port) if served is not None else TS_DEFAULTS.get(key)
        rows.append({"key": key, "name": name, "what": what, "local": f"http://localhost:{local_port}",
                     "remote": (f"https://{host}" + ("" if ts_port == 443 else f":{ts_port}")) if host and ts_port else None})
    _addr["v"], _addr["at"] = {"host": host, "fromTailscale": served is not None, "apps": rows}, time.time()
    return _addr["v"]


# ---------- restore from backups (through the backup command's own browse/restore, never overwriting) ----------
RUNNER = os.environ.get("BACKUP_RUNNER", "/usr/local/bin/homelab-backup")


class RestoreNotSetUp(Exception):
    pass


def runner(*args, timeout=120):
    cmd = [RUNNER, *args] if os.environ.get("BACKUP_RUNNER_NO_SUDO") else ["sudo", "-n", RUNNER, *args]
    r = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    if r.returncode != 0 and ("password is required" in r.stderr or "not allowed to execute" in r.stderr or "sudoers" in r.stderr):
        raise RestoreNotSetUp()
    return r


def restore_places():
    out = json.loads(runner("snapshots-json", timeout=180).stdout or "[]")
    for place in out:
        place["snapshots"] = sorted(({"id": x["short_id"], "time": x["time"]} for x in place.get("snapshots") or []),
                                    key=lambda x: x["time"], reverse=True)
    return {"places": out, "roots": [{"name": "Media", "path": "/srv/media"},
                                     {"name": "Home folder", "path": os.path.expanduser("~")}]}


def restore_ls(place, snap, path):
    r = runner("ls-json", place, snap, path, timeout=180)
    want = os.path.normpath(path)
    items = []
    for line in r.stdout.splitlines():
        try:
            j = json.loads(line)
        except ValueError:
            continue
        if j.get("error"):
            raise ValueError("That folder isn't one Olympus can restore from")
        if j.get("struct_type") == "node" and os.path.dirname(j.get("path", "")) == want:
            items.append({"name": j.get("name"), "type": j.get("type"), "size": j.get("size") or 0,
                          "mtime": j.get("mtime"), "path": j.get("path")})
    items.sort(key=lambda i: (i["type"] != "dir", (i["name"] or "").lower()))
    return {"path": want, "items": items}


def restore_run(place, snap, path):
    r = runner("restore-to", place, snap, path, timeout=7200)
    try:
        j = json.loads(r.stdout.strip().splitlines()[-1])
    except (ValueError, IndexError):
        raise ValueError("The restore didn't finish")
    if j.get("error"):
        raise ValueError("The restore didn't work" if j["error"] != "not allowed" else "That isn't something Olympus can restore")
    return {"to": j["to"].replace(os.path.expanduser("~"), "~", 1)}


# ---------- Aegis: what's been blocked, and pausing it ----------
_aegis = {"at": 0, "v": None}


def aegis_call(method, path, body=None):
    with open(AEGIS_CONF) as f:
        c = json.load(f)
    import base64
    req = urllib.request.Request(c["url"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Basic " + base64.b64encode(f'{c["user"]}:{c["password"]}'.encode()).decode(),
                                          "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=5) as r:
        t = r.read()
    try:
        return json.loads(t)
    except ValueError:            # some AdGuard answers are just "OK"
        return {}


def aegis_summary(fresh=False):
    if not os.path.exists(AEGIS_CONF):
        return {"installed": False}
    if not fresh and _aegis["v"] and time.time() - _aegis["at"] < 20:
        return _aegis["v"]
    try:
        st, stats = aegis_call("GET", "/control/status"), aegis_call("GET", "/control/stats")
        q, b = stats.get("num_dns_queries", 0), stats.get("num_blocked_filtering", 0)
        v = {"installed": True, "up": bool(st.get("running")), "on": bool(st.get("protection_enabled")),
             "pausedMins": round((st.get("protection_disabled_duration") or 0) / 60000),
             "queries": q, "blocked": b, "pct": round(b * 100 / q) if q else 0,
             "top": [list(d.items())[0] for d in (stats.get("top_blocked_domains") or [])[:5]]}
    except Exception:
        v = {"installed": True, "up": False}
    _aegis["v"], _aegis["at"] = v, time.time()
    return v


# ---------- getting started: what's been set up so far (for the welcome tour) ----------
SETUP_FILE = os.path.expanduser("~/.config/olympus/setup.json")


def setup_seen(key):
    try:
        with open(SETUP_FILE) as f:
            d = json.load(f)
    except (OSError, ValueError):
        d = {}
    if not d.get(key):
        d[key] = time.time()
        os.makedirs(os.path.dirname(SETUP_FILE), exist_ok=True)
        with open(SETUP_FILE, "w") as f:
            json.dump(d, f)


def setup_status():
    try:
        with open(SETUP_FILE) as f:
            seen = json.load(f)
    except (OSError, ValueError):
        seen = {}
    try:
        with open(BACKUP_STATUS) as f:
            b = json.load(f)
        backups = bool(b.get("ok"))
    except (OSError, ValueError):
        backups = False
    logs = [os.path.expanduser(x) for x in ("~/.theia-tidy.log", "~/.apollo-tidy.log", "~/.clio-tidy.log")]
    inbox = any(os.path.exists(x) and os.path.getsize(x) > 0 for x in logs)
    return {"phone": bool(seen.get("phone")), "alerts": bool(_alerts.get("last") or seen.get("alerts")),
            "backups": backups, "inbox": inbox}


# ---------- Hestia: lights and plugs on the home screen ----------
HESTIA_KINDS = ("light", "switch", "fan", "input_boolean")


def hestia_call(method, path, body=None):
    with open(HESTIA_CONF) as f:
        c = json.load(f)
    req = urllib.request.Request(c["url"].rstrip("/") + path, method=method,
                                 data=json.dumps(body).encode() if body is not None else None,
                                 headers={"Authorization": "Bearer " + c["token"], "Content-Type": "application/json"})
    with urllib.request.urlopen(req, timeout=6) as r:
        t = r.read()
    try:
        return json.loads(t)
    except ValueError:
        return {}


def hestia_devices():
    if not os.path.exists(HESTIA_CONF):
        return {"connected": False}
    try:
        states = hestia_call("GET", "/api/states")
    except Exception:
        return {"connected": True, "up": False, "devices": []}
    devices = []
    for st in states:
        eid = st.get("entity_id", ""); kind = eid.split(".")[0]
        a = st.get("attributes") or {}
        if kind not in HESTIA_KINDS or st.get("state") in ("unavailable", "unknown") or a.get("hidden"):
            continue
        devices.append({"id": eid, "kind": kind, "name": a.get("friendly_name") or eid, "on": st.get("state") == "on"})
    devices.sort(key=lambda d: (d["kind"] != "light", d["name"].lower()))
    return {"connected": True, "up": True, "devices": devices[:16]}


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
        # Opened through Tailscale (a phone or another device): the "phone" step of getting started is done
        if self.headers.get("Host", "").split(":")[0].endswith(".ts.net") and self.path in ("/", "/index.html"):
            try: setup_seen("phone")
            except OSError: pass
        if self.path == "/api/setup":
            return self._json(setup_status())
        if self.path == "/api/status":
            results = {}
            threads = [threading.Thread(target=lambda k=k, u=u: results.__setitem__(k, check(u))) for k, u in installed_services().items()]
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
                "hermes": f"https://{h}:7843/" if h else None,
                "budget": f"https://{h}:5443/" if h else None,
                "investments": f"https://{h}:3443/" if h else None,
            })
        if self.path == "/api/backup":
            try:
                with open(BACKUP_STATUS) as f:
                    return self._json(json.load(f))
            except (OSError, ValueError):
                return self._json({"missing": True})
        if self.path == "/api/storage/cleared":
            items = sorted(_cleared(), key=lambda i: -i["at"])
            return self._json({"items": [{**i, "from": os.path.relpath(i["from"], MEDIA),
                                          "deleteOn": i["at"] + KEEP_CLEARED_DAYS * 86400} for i in items]})
        if self.path == "/api/aegis":
            return self._json(aegis_summary())
        if self.path == "/api/hestia":
            return self._json(hestia_devices())
        if self.path == "/api/addresses":
            return self._json(addresses())
        if self.path == "/api/stats/bases":
            return self._json(stats_bases())
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
        if self.path == "/api/hestia/toggle":
            if not self._drop_allowed():
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                eid = str(body.get("id", ""))
                if not re.fullmatch(r"(light|switch|fan|input_boolean)\.[a-z0-9_]+", eid):
                    raise ValueError("That isn't a light or switch")
                hestia_call("POST", "/api/services/homeassistant/toggle", {"entity_id": eid})
                return self._json({"ok": True})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path == "/api/aegis/pause":
            if not self._drop_allowed():
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                mins = max(0, min(24 * 60, int(body.get("minutes", 10))))
                aegis_call("POST", "/control/protection", {"enabled": True} if mins == 0 else {"enabled": False, "duration": mins * 60000})
                return self._json({"ok": True, **aegis_summary(fresh=True)})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path in ("/api/restore/places", "/api/restore/ls", "/api/restore/run"):
            if not self._drop_allowed():
                return
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/restore/places":
                    return self._json({"ok": True, **restore_places()})
                args = (str(body.get("place", "")), str(body.get("snap", "")), str(body.get("path", "")))
                if self.path == "/api/restore/ls":
                    return self._json({"ok": True, **restore_ls(*args)})
                return self._json({"ok": True, **restore_run(*args)})
            except RestoreNotSetUp:
                return self._json({"ok": False, "setup": True, "error": "Restoring from Olympus isn't switched on yet"})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path in ("/api/storage/clear", "/api/storage/restore", "/api/storage/delete", "/api/stats/snapshot"):
            if not self._drop_allowed():
                return
            if self.path.startswith("/api/storage/"):
                threading.Timer(0.5, compute_sizes).start()      # so the storage bar is right straight away
            try:
                body = json.loads(self.rfile.read(int(self.headers.get("Content-Length") or 0)) or b"{}")
                if self.path == "/api/storage/clear":
                    return self._json({"ok": True, **clear_out(body.get("path"), str(body.get("title", ""))[:200])})
                if self.path == "/api/storage/restore":
                    it = put_back(str(body.get("id", "")))
                    return self._json({"ok": True, "title": it["title"]})
                if self.path == "/api/storage/delete":
                    delete_now(str(body.get("id", "")))
                    return self._json({"ok": True})
                return self._json({"ok": True, "saved": save_snapshot(body.get("counts") or {})})
            except Exception as e:
                return self._json({"ok": False, "error": str(e)[:200]})
        if self.path == "/api/inbox/file":
            if not self._drop_allowed():
                return
            self.rfile.read(int(self.headers.get("Content-Length") or 0))
            return self._json({"ok": True, "lines": run_tidy()})
        if self.path == "/api/alerts/test":
            ok = notify("Test from Olympus", "Phone alerts are working.", "tada")
            if ok:
                try: setup_seen("alerts")
                except OSError: pass
            return self._json({"ok": ok, "topic": NTFY_TOPIC})
        if self._backend():
            return self._proxy()
        self.send_error(405)

    def _drop_allowed(self):
        # Only from this PC itself or via Tailscale (which arrives from 127.0.0.1), and only the Navidrome admin.
        if self.client_address[0] not in ("127.0.0.1", "::1", "::ffff:127.0.0.1"):
            self.send_error(403, "Uploads only work on this PC or over Tailscale")
            return False
        h = self.headers
        if not is_admin(h.get("X-Nav-U", ""), h.get("X-Nav-T", ""), h.get("X-Nav-S", "")):
            self.send_error(401, "Sign in to Olympus with the Navidrome admin account to add files")
            return False
        return True

    def do_PUT(self):
        if not self.path.startswith("/api/inbox/upload?"):
            self.send_error(405)
            return
        if not self._drop_allowed():
            return
        rel = safe_relpath(urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get("path", [""])[0])
        length = int(self.headers.get("Content-Length") or -1)
        if rel is None:
            self.send_error(415, "Only films, music, subtitles and cover pictures can go in the inbox")
            return
        if length < 0:
            self.send_error(411)
            return
        dst = os.path.join(INBOX, rel)
        if os.path.exists(dst):
            self.send_error(409, "That file is already in the inbox")
            return
        os.makedirs(UPLOADING, exist_ok=True)
        clear_stale_uploads()
        if shutil.disk_usage(INBOX).free < length + 2 * 1024 ** 3:
            self.send_error(507, "Not enough space on the PC")
            return
        part = os.path.join(UPLOADING, uuid.uuid4().hex + ".part")
        try:
            with open(part, "wb") as f:
                left = length
                while left > 0:
                    chunk = self.rfile.read(min(1024 * 1024, left))
                    if not chunk:
                        raise ConnectionError("upload stopped part-way")
                    f.write(chunk)
                    left -= len(chunk)
            os.makedirs(os.path.dirname(dst), exist_ok=True)
            os.replace(part, dst)
            old = time.time() - 600          # it's complete, so the inbox scripts can file it straight away
            os.utime(dst, (old, old))
        except Exception as e:
            try: os.remove(part)
            except OSError: pass
            self.send_error(500, str(e)[:100])
            return
        return self._json({"ok": True, "path": rel})

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
