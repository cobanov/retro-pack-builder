#!/usr/bin/env python3
"""Retro Pack Builder: a tiny web UI for curating a handheld game pack from a ROM archive.

Standard library only. Environment:
  RETRO  archive root (default /retro), laid out as roms/<system>, saves/<system>, media/<system>/boxart
  PACKS  folder of pack lists (default $RETRO/packs); each pack is <name>.txt, one "<system>/<file>" per line
  PACK   pack shown first (default "top")
  DB     SQLite library for play status (default $RETRO/library.db)
  PORT   port to listen on (default 8790)
  BUILDER  pack builder script (default: build-pack.py next to this file)
  CORS_ORIGIN  set (e.g. "*") to let browser apps on other origins call the API; off by default

The UI talks to a small JSON API that scripts and agents can use too: GET /api describes it.
"""
import json
import os
import re
import shutil
import sqlite3
import subprocess
import sys
import threading
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlparse

RETRO = os.environ.get("RETRO", "/retro")
PACKS = os.environ.get("PACKS", os.path.join(RETRO, "packs"))
DEFAULT_PACK = os.environ.get("PACK", "top")
MARKER = ".generated-pack"  # written by build-pack.py into every built pack folder
PORT = int(os.environ.get("PORT", "8790"))
ROMS, SAVES, MEDIA = (os.path.join(RETRO, d) for d in ("roms", "saves", "media"))
HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
BUILDER = os.environ.get("BUILDER", os.path.join(HERE, "build-pack.py"))
SAVE_EXT = (".srm", ".sav", ".dsv")
SKIP = {"neogeo.zip"}
DB = os.environ.get("DB", os.path.join(RETRO, "library.db"))
STATUSES = ["playing", "completed", "on_hold", "dropped", "plan"]
CORS_ORIGIN = os.environ.get("CORS_ORIGIN", "")

SYSTEMS = [
    ("gb", "Game Boy"), ("gbc", "Game Boy Color"), ("gba", "Game Boy Advance"), ("nds", "Nintendo DS"),
    ("nes", "NES"), ("snes", "SNES"), ("n64", "Nintendo 64"),
    ("megadrive", "Mega Drive / Genesis"), ("mastersystem", "Master System"), ("gamegear", "Game Gear"),
    ("segacd", "Sega CD"), ("dreamcast", "Dreamcast"),
    ("psx", "PlayStation"), ("psp", "PSP"),
    ("pcengine", "PC Engine"), ("wonderswancolor", "WonderSwan Color"),
    ("atari2600", "Atari 2600"), ("atari5200", "Atari 5200"), ("atari7800", "Atari 7800"),
    ("cps1", "CPS-1"), ("cps2", "CPS-2"), ("cps3", "CPS-3"), ("neogeo", "Neo Geo"),
    ("mame", "MAME"), ("mame2003", "MAME 2003"), ("mame2010", "MAME 2010"),
]
KNOWN = dict(SYSTEMS)


def systems():
    """Known systems in a friendly order, then any other folder under roms/ (labelled by its name)."""
    found = {d for d in os.listdir(ROMS) if os.path.isdir(os.path.join(ROMS, d)) and not d.startswith((".", "@"))} if os.path.isdir(ROMS) else set()
    return [(k, v) for k, v in SYSTEMS if k in found] + [(d, d) for d in sorted(found - set(KNOWN))]


def label(sysn):
    return KNOWN.get(sysn, sysn)


def is_system(sysn):
    return sysn and "/" not in sysn and not sysn.startswith((".", "@")) and os.path.isdir(os.path.join(ROMS, sysn))


lock = threading.Lock()
build_lock = threading.Lock()


def clean_name(base):
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", base).strip()
    m = re.match(r"^(.*?), (The|A|An)( - .*)?$", n)
    return f"{m.group(2)} {m.group(1)}{m.group(3) or ''}" if m else n


def names_for(sysn):
    p = os.path.join(MEDIA, sysn, "names.tsv")
    if not os.path.exists(p):
        return {}
    return dict(l.rstrip("\n").split("\t", 1) for l in open(p, encoding="utf-8") if "\t" in l)


def entries(sysn):
    d = os.path.join(ROMS, sysn)
    if not os.path.isdir(d):
        return []
    return sorted(f for f in os.listdir(d) if not f.startswith((".", "@")) and f not in SKIP)


def size_of(p):
    if os.path.isdir(p):
        return sum(os.path.getsize(os.path.join(r, f)) for r, _, fs in os.walk(p) for f in fs)
    return os.path.getsize(p)


def base_of(sysn, entry):
    return entry if os.path.isdir(os.path.join(ROMS, sysn, entry)) else os.path.splitext(entry)[0]


LIST_HEADER = [
    "# Retro Pack Builder pack list: one game per line as <system>/<file>, optionally \"| Display name\".",
    "# Lines starting with # are comments. Edit by hand or from the web UI, then build the pack.",
]


def slug(name):
    s = re.sub(r"[^a-z0-9]+", "-", str(name).lower()).strip("-")[:40]
    if not s:
        raise ValueError("a pack name needs letters or digits")
    return s


def list_path(pack):
    return os.path.join(PACKS, slug(pack) + ".txt")


def packs():
    """Saved packs: every <name>.txt in the packs folder."""
    out = []
    if os.path.isdir(PACKS):
        for f in sorted(os.listdir(PACKS)):
            if f.endswith(".txt") and not f.startswith("."):
                name = f[:-4]
                out.append({"name": name, "games": len(read_list(name)[1]),
                            "built": os.path.exists(os.path.join(PACKS, name, MARKER))})
    return out


def pack_action(action, name, to=None):
    """create, duplicate (to = new name), rename (to = new name) or delete a pack. Built folders follow along."""
    src, built = list_path(name), os.path.join(PACKS, slug(name))
    with lock:
        if action == "create":
            if os.path.exists(src):
                raise ValueError("a pack with that name already exists")
            write_list(LIST_HEADER[:], name)
            return slug(name)
        if not os.path.exists(src):
            raise ValueError("no such pack")
        if action in ("duplicate", "rename"):
            dst = list_path(to)
            if os.path.exists(dst):
                raise ValueError("a pack with that name already exists")
            if action == "duplicate":
                shutil.copyfile(src, dst)
            else:
                os.replace(src, dst)
                if os.path.exists(src + ".bak"):
                    os.replace(src + ".bak", dst + ".bak")
                if os.path.exists(os.path.join(built, MARKER)):
                    os.replace(built, os.path.join(PACKS, slug(to)))
            return slug(to)
        if action == "delete":
            for p in (src, src + ".bak"):
                if os.path.exists(p):
                    os.remove(p)
            if os.path.exists(os.path.join(built, MARKER)):  # only folders build-pack.py made
                shutil.rmtree(built)
            return None
    raise ValueError("unknown pack action")


def read_list(pack):
    path = list_path(pack)
    if not os.path.exists(path):
        return LIST_HEADER[:], {}
    lines = open(path, encoding="utf-8").read().split("\n")
    sel = {}
    for l in lines:
        s = l.strip()
        if s and not s.startswith("#"):
            rel, _, name = (p.strip() for p in s.partition("|"))
            sel[rel] = name or None
    return lines, sel


def db():
    """SQLite library: per-game status (MyAnimeList style). Room for rating/notes later."""
    c = sqlite3.connect(DB, timeout=10)
    c.execute(
        "CREATE TABLE IF NOT EXISTS game_status ("
        " rel TEXT PRIMARY KEY,"  # <system>/<file> under roms/
        " status TEXT NOT NULL CHECK (status IN ('playing','completed','on_hold','dropped','plan')),"
        " updated_at TEXT NOT NULL DEFAULT (datetime('now')))"
    )
    return c


def read_status():
    with closing(db()) as c:
        return {rel: {"status": st, "updated": up} for rel, st, up in c.execute("SELECT rel, status, updated_at FROM game_status")}


def set_status(rel, status):
    """Tag a game: playing, completed, on_hold, dropped, plan. None clears the tag."""
    sysn, _, e = rel.partition("/")
    if not is_system(sysn) or e not in entries(sysn):
        raise ValueError("no such game in the archive")
    if status is not None and status not in STATUSES:
        raise ValueError("unknown status")
    with closing(db()) as c, c:
        if status:
            c.execute(
                "INSERT INTO game_status (rel, status, updated_at) VALUES (?, ?, datetime('now'))"
                " ON CONFLICT(rel) DO UPDATE SET status = excluded.status, updated_at = excluded.updated_at",
                (rel, status),
            )
        else:
            c.execute("DELETE FROM game_status WHERE rel = ?", (rel,))


def write_list(lines, pack):
    path = list_path(pack)
    os.makedirs(os.path.dirname(path), exist_ok=True)
    with open(path + ".tmp", "w", encoding="utf-8") as fh:
        fh.write("\n".join(lines).rstrip("\n") + "\n")
    if os.path.exists(path):
        os.replace(path, path + ".bak")
    os.replace(path + ".tmp", path)


def saved():
    """Games that have a save file in saves/ (shown with a disk icon; their saves travel with the pack)."""
    out = set()
    if not os.path.isdir(SAVES):
        return out
    for sysn in os.listdir(SAVES):
        sd = os.path.join(SAVES, sysn)
        if not os.path.isdir(sd):
            continue
        bases = {os.path.splitext(f)[0] for f in os.listdir(sd) if f.endswith(SAVE_EXT)}
        for e in entries(sysn):
            if base_of(sysn, e) in bases:
                out.add(f"{sysn}/{e}")
    return out


def game(sysn, e, sel, sv, names, arts, with_size=False, st=None):
    rel = f"{sysn}/{e}"
    b = base_of(sysn, e)
    g = {"rel": rel, "sys": sysn, "sysLabel": label(sysn),
         "name": sel.get(rel) or names.get(e) or clean_name(b), "file": e,
         "art": arts.get(b + ".png", (0, None))[0], "ar": arts.get(b + ".png", (0, None))[1],
         "save": rel in sv, "listed": rel in sel, "status": (st or {}).get(rel, {}).get("status"),
         "selected": rel in sel}
    if with_size:
        g["size"] = size_of(os.path.join(ROMS, sysn, e))
    return g


_art_cache = {}


def png_ratio(path):
    """Width / height from the PNG header, without decoding the image."""
    with open(path, "rb") as fh:
        b = fh.read(24)
    if b[:8] != b"\x89PNG\r\n\x1a\n":
        return None
    w, h = int.from_bytes(b[16:20], "big"), int.from_bytes(b[20:24], "big")
    return round(w / h, 3) if h else None


def arts_for(sysn):
    """Cover file name -> (mtime, aspect ratio). mtime busts the browser cache when a cover changes."""
    d = os.path.join(MEDIA, sysn, "boxart")
    if not os.path.isdir(d):
        return {}
    stamp = os.stat(d).st_mtime
    hit = _art_cache.get(sysn)
    if hit and hit[0] == stamp:
        return hit[1]
    arts = {e.name: (int(e.stat().st_mtime), png_ratio(e.path)) for e in os.scandir(d) if e.name.endswith(".png")}
    _art_cache[sysn] = (stamp, arts)
    return arts


def system_ratio(sysn):
    """Typical cover shape of a system: the median aspect ratio of its covers (PSP ~0.58, SNES ~1.41)."""
    rs = sorted(r for _, r in arts_for(sysn).values() if r)
    return rs[len(rs) // 2] if rs else 1.0


def summary(pack):
    _, sel = read_list(pack)
    sv = saved()
    chosen = set(sel)
    rows, size = [], 0
    for sysn, lbl in systems():
        es = entries(sysn)
        if not es:
            continue
        n = sum(1 for e in es if f"{sysn}/{e}" in chosen)
        rows.append({"id": sysn, "label": lbl, "total": len(es), "selected": n, "ar": system_ratio(sysn)})
    for rel in chosen:
        p = os.path.join(ROMS, rel)
        if os.path.exists(p):
            size += size_of(p)
    counts = {k: 0 for k in STATUSES}
    for v in read_status().values():
        counts[v["status"]] = counts.get(v["status"], 0) + 1
    return {"pack": slug(pack), "packs": packs(), "statusCounts": counts, "systems": rows, "selectedTotal": len([r for r in chosen if os.path.exists(os.path.join(ROMS, r))]),
            "missing": sorted(r for r in sel if not os.path.exists(os.path.join(ROMS, r))), "sizeBytes": size}


def matches(q, *texts):
    """Every word of the query appears in the name or file name (case-insensitive)."""
    hay = " ".join(texts).lower()
    return all(w in hay for w in q.lower().split())


def games(pack, sysn=None, status=None, q=None, limit=200):
    """One system's games, games with a given status, a search across every system (q),
    or (default) everything in the pack."""
    _, sel = read_list(pack)
    sv = saved()
    st = read_status()
    if sysn or (q and not status):
        out = []
        for s in [sysn] if sysn else [k for k, _ in systems()]:
            names, arts = names_for(s), arts_for(s)
            for e in entries(s):
                g = game(s, e, sel, sv, names, arts, with_size=True, st=st)
                if not q or matches(q, g["name"], e):
                    out.append(g)
                    if q and not sysn and len(out) >= limit:
                        return out
        return out
    rels = [r for r, v in st.items() if v["status"] == status] if status else set(sel)
    out = []
    for rel in sorted(rels):
        s, e = rel.split("/", 1)
        if os.path.exists(os.path.join(ROMS, rel)):
            out.append(game(s, e, sel, sv, names_for(s), arts_for(s), with_size=True, st=st))
    if q:
        out = [g for g in out if matches(q, g["name"], g["file"])]
    order = {k: i for i, (k, _) in enumerate(systems())}
    return sorted(out, key=lambda g: (order.get(g["sys"], 99), g["name"].lower()))


def toggle(rels, on, pack):
    """Add games to the pack list (next to others of the same system) or remove them. One write for many games.
    Removing does not require the game to exist, so stale lines can be cleaned up."""
    for rel in rels if on else []:
        sysn, _, e = rel.partition("/")
        if not is_system(sysn) or e not in entries(sysn):
            raise ValueError(f"no such game in the archive: {rel}")
    key = lambda l: l.strip().partition("|")[0].strip()
    with lock:
        lines, sel = read_list(pack)
        if on:
            for rel in rels:
                if rel in sel:
                    continue
                sysn = rel.partition("/")[0]
                idx = max((i for i, l in enumerate(lines) if not l.strip().startswith("#") and key(l).startswith(sysn + "/")), default=None)
                if idx is None:
                    while lines and not lines[-1].strip():
                        lines.pop()
                    lines += ["", f"## {label(sysn)}", rel]
                else:
                    lines.insert(idx + 1, rel)
                sel[rel] = None
        else:
            drop = set(rels)
            lines = prune([l for l in lines if l.strip().startswith("#") or key(l) not in drop])
        write_list(lines, pack)


def prune(lines):
    """Drop "## Section" headers left without any games, and runs of blank lines."""
    out = []
    for i, l in enumerate(lines):
        if l.strip().startswith("## "):
            rest = []
            for m in lines[i + 1:]:
                if m.strip().startswith("## "):
                    break
                rest.append(m.strip())
            if not any(t and not t.startswith("#") for t in rest):
                continue
        if not l.strip() and out and not out[-1].strip():
            continue
        out.append(l)
    while out and not out[-1].strip():
        out.pop()
    return out


API = {
    "name": "Retro Pack Builder API",
    "notes": [
        "JSON over HTTP on the local network, no authentication.",
        "Games are identified by rel = '<system>/<file name>' exactly as under roms/ (find them with /api/games?q=...).",
        "Calls act on one pack: ?pack=<name> for GET, \"pack\" in the JSON body for POST. Default: the PACK setting.",
    ],
    "endpoints": [
        {"method": "GET", "path": "/api", "does": "this description"},
        {"method": "GET", "path": "/api/summary?pack=top", "does": "systems with game counts and cover shape, pack size, saved packs, status counts"},
        {"method": "GET", "path": "/api/games?q=chrono trigger", "does": "search every system by name or file name (max 200 results)"},
        {"method": "GET", "path": "/api/games?sys=snes&pack=top", "does": "all games of a system; 'selected' tells whether each is in the pack (q filters further)"},
        {"method": "GET", "path": "/api/games?status=playing", "does": "games with a play status: playing, completed, on_hold, dropped, plan"},
        {"method": "GET", "path": "/api/games?pack=top", "does": "the games in a pack"},
        {"method": "GET", "path": "/api/packs", "does": "saved packs with game counts"},
        {"method": "POST", "path": "/api/packs", "body": {"action": "create | duplicate | rename | delete", "name": "rpg", "to": "new name (duplicate, rename)"}},
        {"method": "POST", "path": "/api/toggle", "body": {"pack": "top", "rels": ["snes/Chrono Trigger (USA).zip"], "on": True},
         "does": "add (on: true) or remove (on: false) games; returns the pack summary"},
        {"method": "POST", "path": "/api/status", "body": {"rel": "gba/Metroid Fusion (USA).zip", "status": "completed (or null to clear)"}},
        {"method": "POST", "path": "/api/build", "body": {"pack": "top"}, "does": "build packs/<pack>/ for the SD card; returns the builder output"},
        {"method": "POST", "path": "/api/boxart", "body": {"sys": "gba (omit for every system)"}, "does": "start fetching missing covers in the background"},
        {"method": "GET", "path": "/api/boxart", "does": "progress and results of the cover fetch"},
        {"method": "GET", "path": "/art?rel=gba/Metroid Fusion (USA).zip", "does": "a game's cover image (PNG)"},
    ],
}


# Box art download runs in the background; the UI polls /api/boxart for progress
cover_job = {"running": False, "system": None, "done": 0, "total": 0, "found": 0, "results": [], "error": None}


def fetch_covers(targets):
    import boxart
    cover_job.update(running=True, results=[], error=None)
    try:
        for sysn in targets:
            cover_job.update(system=sysn, done=0, total=0, found=0)
            st = boxart.fetch_system(RETRO, sysn, progress=lambda **k: cover_job.update(done=k["done"], total=k["total"], found=k["found"]))
            cover_job["results"].append(st)
    except Exception as ex:  # noqa: BLE001
        cover_job["error"] = str(ex)
    finally:
        cover_job.update(running=False, system=None)


def build(pack):
    if not build_lock.acquire(blocking=False):
        return False, "A build is already running, please wait."
    try:
        r = subprocess.run([sys.executable, BUILDER, list_path(pack)], capture_output=True, text=True, timeout=1800)
        return r.returncode == 0, (r.stdout + r.stderr).strip()
    finally:
        build_lock.release()


class H(BaseHTTPRequestHandler):
    def log_message(self, fmt, *a):
        pass

    def send(self, code, body, ctype="application/json; charset=utf-8", cache=None):
        if isinstance(body, (dict, list)):
            body = json.dumps(body, ensure_ascii=False)
        if isinstance(body, str):
            body = body.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        if cache:
            self.send_header("Cache-Control", cache)
        if CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
        self.end_headers()
        self.wfile.write(body)

    def do_OPTIONS(self):
        """CORS preflight, only answered when CORS_ORIGIN is set."""
        self.send_response(204 if CORS_ORIGIN else 405)
        if CORS_ORIGIN:
            self.send_header("Access-Control-Allow-Origin", CORS_ORIGIN)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    def do_GET(self):
        u = urlparse(self.path)
        q = {k: v[0] for k, v in parse_qs(u.query).items()}
        pack = q.get("pack") or DEFAULT_PACK
        try:
            if u.path == "/":
                return self.send(200, open(PAGE_FILE, "rb").read(), "text/html; charset=utf-8")
            if u.path in ("/api", "/api/"):
                return self.send(200, API)
            if u.path == "/api/summary":
                return self.send(200, summary(pack))
            if u.path == "/api/packs":
                return self.send(200, {"packs": packs(), "default": slug(DEFAULT_PACK)})
            if u.path == "/api/boxart":
                return self.send(200, cover_job)
            if u.path == "/api/games":
                s, st, text = q.get("sys"), q.get("status"), (q.get("q") or "").strip()
                if s and not is_system(s):
                    return self.send(404, {"error": "unknown system"})
                if st and st not in STATUSES:
                    return self.send(404, {"error": "unknown status"})
                return self.send(200, {"games": games(pack, s, st, text or None)})
            if u.path == "/art":
                sysn, _, e = q.get("rel", "").partition("/")
                if not is_system(sysn) or e not in entries(sysn):
                    return self.send(404, b"", "text/plain")
                p = os.path.join(MEDIA, sysn, "boxart", base_of(sysn, e) + ".png")
                if not os.path.exists(p):
                    return self.send(404, b"", "text/plain")
                return self.send(200, open(p, "rb").read(), "image/png", "public, max-age=86400")
            return self.send(404, {"error": "not found"})
        except Exception as ex:  # noqa: BLE001
            return self.send(500, {"error": str(ex)})

    def do_POST(self):
        u = urlparse(self.path)
        n = int(self.headers.get("Content-Length") or 0)
        data = json.loads(self.rfile.read(n) or b"{}")
        pack = (data.get("pack") or DEFAULT_PACK) if isinstance(data, dict) else DEFAULT_PACK
        try:
            if not isinstance(data, dict):
                raise ValueError("expected a JSON object")
            if u.path == "/api/toggle":
                toggle(data.get("rels") or [data["rel"]], bool(data["on"]), pack)
                return self.send(200, summary(pack))
            if u.path == "/api/packs":
                name = pack_action(data["action"], data["name"], data.get("to"))
                return self.send(200, {"pack": name, "packs": packs()})
            if u.path == "/api/boxart":
                if cover_job["running"]:
                    return self.send(409, {"error": "already fetching covers"})
                targets = [data["sys"]] if data.get("sys") else [k for k, _ in systems()]
                if not all(is_system(t) for t in targets):
                    return self.send(404, {"error": "unknown system"})
                cover_job.update(running=True, system=None, done=0, total=0, found=0, results=[], error=None)
                threading.Thread(target=fetch_covers, args=(targets,), daemon=True).start()
                return self.send(202, cover_job)
            if u.path == "/api/status":
                set_status(data["rel"], data.get("status") or None)
                return self.send(200, summary(pack))
            if u.path == "/api/build":
                ok, out = build(pack)
                return self.send(200, {"ok": ok, "output": out})
            return self.send(404, {"error": "not found"})
        except ValueError as ex:
            return self.send(400, {"error": str(ex)})
        except Exception as ex:  # noqa: BLE001
            return self.send(500, {"error": str(ex)})


PAGE_FILE = os.path.join(HERE, "index.html")

if __name__ == "__main__":
    print(f"Retro Pack Builder: http://0.0.0.0:{PORT}  (archive {RETRO}, packs {PACKS})", flush=True)
    ThreadingHTTPServer(("0.0.0.0", PORT), H).serve_forever()
