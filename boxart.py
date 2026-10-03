#!/usr/bin/env python3
"""Fetch missing box art for an archive from libretro-thumbnails (https://github.com/libretro-thumbnails).

Usage: RETRO=/srv/games/retro python3 boxart.py [system ...]

Covers land in media/<system>/boxart/<rom name>.png. Only missing covers are downloaded.
Matching: exact libretro name first, then the same title ignoring region/revision tags
(preferring USA, World, Europe, Japan). Arcade short names (sf2.zip) are turned into full
titles with the libretro-database DAT files, which also fills media/<system>/names.tsv.

Set GITHUB_TOKEN to lift GitHub's 60 requests/hour limit for anonymous API calls
(two calls per system; listings are cached for a week in media/.cache/).
"""
import json
import os
import re
import sys
import time
import unicodedata
import urllib.parse
import urllib.request
from concurrent.futures import ThreadPoolExecutor

THUMBS = "https://raw.githubusercontent.com/libretro-thumbnails/{repo}/master/Named_Boxarts/{name}.png"
TREES = "https://api.github.com/repos/libretro-thumbnails/{repo}/git/trees/{sha}"
DATS = "https://raw.githubusercontent.com/libretro/libretro-database/master/metadat/{path}"
CACHE_DAYS = 7

# Folder names used by ROCKNIX, Batocera, Knulli and ES-DE -> libretro-thumbnails repos (first match wins)
CONSOLES = {
    "gb": ["Nintendo_-_Game_Boy"],
    "gbc": ["Nintendo_-_Game_Boy_Color"],
    "gba": ["Nintendo_-_Game_Boy_Advance"],
    "nds": ["Nintendo_-_Nintendo_DS"],
    "nes": ["Nintendo_-_Nintendo_Entertainment_System"],
    "famicom": ["Nintendo_-_Nintendo_Entertainment_System"],
    "fds": ["Nintendo_-_Family_Computer_Disk_System"],
    "snes": ["Nintendo_-_Super_Nintendo_Entertainment_System"],
    "sfc": ["Nintendo_-_Super_Nintendo_Entertainment_System"],
    "n64": ["Nintendo_-_Nintendo_64"],
    "virtualboy": ["Nintendo_-_Virtual_Boy"],
    "pokemini": ["Nintendo_-_Pokemon_Mini"],
    "gamecube": ["Nintendo_-_GameCube"],
    "gc": ["Nintendo_-_GameCube"],
    "wii": ["Nintendo_-_Wii"],
    "3ds": ["Nintendo_-_Nintendo_3DS"],
    "megadrive": ["Sega_-_Mega_Drive_-_Genesis"],
    "genesis": ["Sega_-_Mega_Drive_-_Genesis"],
    "mastersystem": ["Sega_-_Master_System_-_Mark_III"],
    "gamegear": ["Sega_-_Game_Gear"],
    "segacd": ["Sega_-_Mega-CD_-_Sega_CD"],
    "megacd": ["Sega_-_Mega-CD_-_Sega_CD"],
    "sega32x": ["Sega_-_32X"],
    "saturn": ["Sega_-_Saturn"],
    "dreamcast": ["Sega_-_Dreamcast"],
    "sg-1000": ["Sega_-_SG-1000"],
    "psx": ["Sony_-_PlayStation"],
    "psp": ["Sony_-_PlayStation_Portable"],
    "ps2": ["Sony_-_PlayStation_2"],
    "pcengine": ["NEC_-_PC_Engine_-_TurboGrafx_16"],
    "tg16": ["NEC_-_PC_Engine_-_TurboGrafx_16"],
    "pcenginecd": ["NEC_-_PC_Engine_CD_-_TurboGrafx-CD"],
    "tg16cd": ["NEC_-_PC_Engine_CD_-_TurboGrafx-CD"],
    "supergrafx": ["NEC_-_PC_Engine_SuperGrafx"],
    "wonderswan": ["Bandai_-_WonderSwan", "Bandai_-_WonderSwan_Color"],
    "wswan": ["Bandai_-_WonderSwan", "Bandai_-_WonderSwan_Color"],
    "wonderswancolor": ["Bandai_-_WonderSwan_Color", "Bandai_-_WonderSwan"],
    "wswanc": ["Bandai_-_WonderSwan_Color", "Bandai_-_WonderSwan"],
    "ngp": ["SNK_-_Neo_Geo_Pocket"],
    "ngpc": ["SNK_-_Neo_Geo_Pocket_Color"],
    "neocd": ["SNK_-_Neo_Geo_CD"],
    "atari2600": ["Atari_-_2600"],
    "atari5200": ["Atari_-_5200"],
    "atari7800": ["Atari_-_7800"],
    "atarilynx": ["Atari_-_Lynx"],
    "lynx": ["Atari_-_Lynx"],
    "atarijaguar": ["Atari_-_Jaguar"],
    "jaguar": ["Atari_-_Jaguar"],
    "colecovision": ["Coleco_-_ColecoVision"],
    "intellivision": ["Mattel_-_Intellivision"],
    "vectrex": ["GCE_-_Vectrex"],
    "msx": ["Microsoft_-_MSX"],
    "msx2": ["Microsoft_-_MSX2"],
    "c64": ["Commodore_-_64"],
    "amiga": ["Commodore_-_Amiga"],
    "3do": ["The_3DO_Company_-_3DO"],
}
# Arcade folders: which DATs turn short names into titles (first hit wins), then MAME/FBNeo covers
FBNEO = "fbneo-split/FBNeo - Arcade Games.dat"
M2003P, M2003, M2010, MAME = ("mame-nonmerged/MAME 2003-Plus.dat", "mame-nonmerged/MAME 2003.dat",
                              "mame-nonmerged/MAME 2010.dat", "mame/MAME.dat")
ARCADE = {
    "fbneo": [FBNEO, MAME], "fbn": [FBNEO, MAME], "cps1": [FBNEO, M2003P, MAME], "cps2": [FBNEO, M2003P, MAME],
    "cps3": [FBNEO, MAME], "neogeo": [FBNEO, M2003P, MAME], "arcade": [FBNEO, MAME, M2003P],
    "mame": [MAME, FBNEO, M2010], "mame2003": [M2003P, M2003, FBNEO, MAME], "mame2003plus": [M2003P, M2003, FBNEO, MAME],
    "mame2010": [M2010, MAME, FBNEO],
}
ARCADE_REPOS = ["MAME", "FBNeo_-_Arcade_Games"]
REGIONS = ["USA", "World", "Europe", "Japan"]
BAD = re.compile(r"\((Demo|Kiosk|Beta|Proto|Sample|Pirate)[^)]*\)", re.I)


def http(url, api=False):
    req = urllib.request.Request(url, headers={"User-Agent": "retro-pack-builder"})
    if api and os.environ.get("GITHUB_TOKEN"):
        req.add_header("Authorization", "Bearer " + os.environ["GITHUB_TOKEN"])
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read()


def cached(cache_dir, key, fetch):
    """Return text for key, refetching when older than CACHE_DAYS."""
    p = os.path.join(cache_dir, re.sub(r"[^\w.-]", "_", key))
    if os.path.exists(p) and time.time() - os.path.getmtime(p) < CACHE_DAYS * 86400:
        return open(p, encoding="utf-8").read()
    text = fetch()
    os.makedirs(cache_dir, exist_ok=True)
    with open(p + ".tmp", "w", encoding="utf-8") as fh:
        fh.write(text)
    os.replace(p + ".tmp", p)
    return text


def boxart_names(repo, cache_dir):
    """All cover names in a libretro-thumbnails repo (Named_Boxarts, without .png)."""
    def fetch():
        root = json.loads(http(TREES.format(repo=repo, sha="master"), api=True))
        sha = next(t["sha"] for t in root["tree"] if t["path"] == "Named_Boxarts")
        tree = json.loads(http(TREES.format(repo=repo, sha=sha), api=True))
        return "\n".join(t["path"][:-4] for t in tree["tree"] if t["path"].endswith(".png"))
    return [n for n in cached(cache_dir, f"boxarts-{repo}.txt", fetch).split("\n") if n]


def dat_titles(path, cache_dir):
    """Short name (sf2) -> title from a libretro-database DAT."""
    text = cached(cache_dir, "dat-" + path, lambda: http(DATS.format(path=urllib.parse.quote(path))).decode("utf-8", "replace"))
    out = {}
    for g in re.finditer(r'game \(\s*name "((?:[^"\\]|\\.)*)"(.*?)\n\)', text, re.S):
        r = re.search(r'rom \( name "?([^\s"]+?)\.zip"? ', g.group(2))
        if r:
            out.setdefault(r.group(1).lower(), g.group(1))
    return out


def san(n):
    """libretro replaces these characters in thumbnail file names."""
    return re.sub(r'[&*/:`<>?\\|"]', "_", n)


def norm(n):
    n = re.sub(r"\s*[\(\[][^\)\]]*[\)\]]", "", n)
    n = unicodedata.normalize("NFKD", n).encode("ascii", "ignore").decode().lower()
    n = re.sub(r"^the\s+", "", n)
    n = re.sub(r",\s*the\b", "", n)
    return re.sub(r"[^a-z0-9]+", "", n)


def loose(n):
    """Second chance: also ignore filler words, so "Pokemon Red" finds "Pokemon - Red Version"."""
    return re.sub(r"(version|edition)", "", norm(n))


def rank(name):
    region = next((i for i, r in enumerate(REGIONS) if re.search(r"\([^)]*\b" + r + r"\b", name)), 9)
    return (1 if BAD.search(name) else 0, region, len(name))


class Index:
    def __init__(self, repos, cache_dir):
        self.exact, self.fuzzy, self.loose = {}, {}, {}
        for repo in repos:
            for n in boxart_names(repo, cache_dir):
                self.exact.setdefault(n, (repo, n))
                self.fuzzy.setdefault(norm(n), []).append((repo, n))
                self.loose.setdefault(loose(n), []).append((repo, n))

    def find(self, title):
        hit = self.exact.get(san(title))
        if hit:
            return hit
        cands = self.fuzzy.get(norm(title)) or self.loose.get(loose(title))
        return sorted(cands, key=lambda c: rank(c[1]))[0] if cands else None


def download(repo, name):
    """PNG bytes. Some repo entries are git symlinks: raw.githubusercontent then returns the target's file name."""
    for _ in range(3):
        data = http(THUMBS.format(repo=repo, name=urllib.parse.quote(name)))
        if data[:8] == b"\x89PNG\r\n\x1a\n":
            return data
        if len(data) > 512:
            return None
        name = os.path.splitext(os.path.basename(data.decode("utf-8", "replace").strip()))[0]
    return None


def fetch_system(retro, sysn, progress=lambda **k: None):
    roms = os.path.join(retro, "roms", sysn)
    art = os.path.join(retro, "media", sysn, "boxart")
    cache = os.path.join(retro, "media", ".cache")
    entries = sorted(f for f in os.listdir(roms) if not f.startswith((".", "@")) and not f.endswith((".m3u", ".txt", ".xml")))
    base = {e: (e if os.path.isdir(os.path.join(roms, e)) else os.path.splitext(e)[0]) for e in entries}
    have = set(os.listdir(art)) if os.path.isdir(art) else set()
    missing = [e for e in entries if base[e] + ".png" not in have]
    stats = {"system": sysn, "total": len(missing), "done": 0, "found": 0, "skipped": None}
    if sysn in ARCADE:
        titles = {}
        for d in ARCADE[sysn]:
            for k, v in dat_titles(d, cache).items():
                titles.setdefault(k, v)
        names_tsv = os.path.join(retro, "media", sysn, "names.tsv")
        known = dict(l.rstrip("\n").split("\t", 1) for l in open(names_tsv, encoding="utf-8") if "\t" in l) if os.path.exists(names_tsv) else {}
        for e in entries:
            t = titles.get(os.path.splitext(e)[0].lower())
            if t and e not in known:
                known[e] = re.sub(r"\s+/\s+.*$", "", re.sub(r"\s*\([^)]*\)\s*$", "", t)).strip()
        os.makedirs(os.path.dirname(names_tsv), exist_ok=True)
        with open(names_tsv, "w", encoding="utf-8") as fh:
            fh.write("".join(f"{k}\t{v}\n" for k, v in sorted(known.items())))
        lookup = {e: titles.get(os.path.splitext(e)[0].lower()) for e in missing}
        repos = ARCADE_REPOS
    elif sysn in CONSOLES:
        lookup = {e: re.sub(r"^\d+\.\s+", "", base[e]) for e in missing}
        repos = CONSOLES[sysn]
    else:
        stats["skipped"] = "no libretro mapping for this folder name"
        progress(**stats)
        return stats
    progress(**stats)
    if not missing:
        return stats
    index = Index(repos, cache)
    os.makedirs(art, exist_ok=True)

    def one(e):
        hit = index.find(lookup[e]) if lookup[e] else None
        data = download(*hit) if hit else None
        if data:
            tmp = os.path.join(art, ".tmp-" + base[e] + ".png")
            with open(tmp, "wb") as fh:
                fh.write(data)
            os.replace(tmp, os.path.join(art, base[e] + ".png"))
        return bool(data)

    with ThreadPoolExecutor(8) as pool:
        for ok in pool.map(one, missing):
            stats["done"] += 1
            stats["found"] += ok
            if stats["done"] % 10 == 0 or stats["done"] == stats["total"]:
                progress(**stats)
    return stats


def main():
    retro = os.environ.get("RETRO", ".")
    systems = sys.argv[1:] or sorted(d for d in os.listdir(os.path.join(retro, "roms")) if not d.startswith((".", "@")))
    for s in systems:
        st = fetch_system(retro, s)
        print(f"{s:<16} " + (st["skipped"] or f"{st['found']} / {st['total']} missing covers found"))


if __name__ == "__main__":
    main()
