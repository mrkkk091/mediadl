#!/usr/bin/env python3
import datetime
import importlib.metadata as md
import json
import os
import re
import shutil
import subprocess
import sys
import time
import urllib.parse
import urllib.request
from collections import deque
from importlib import invalidate_caches
from importlib.util import find_spec

__version__ = "1.0.2"
UPDATE_REPO = "https://github.com/mrkkk091/mediadl"

CONFIG_PATH = os.path.expanduser("~/.mediadl.json")
DEFAULTS = {
    "music_dir": "/storage/emulated/0/Music",
    "video_dir": "/storage/emulated/0/Movies",
    "audio_format": "mp3",
    "cookies": "",
    "last_update": 0,
    "ignore_until": 0,
    "last_update_check": 0,
}
DESKTOP_UA = ("Mozilla/5.0 (X11; Linux x86_64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0 Safari/537.36")
UA = ("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
YT_DLP = [sys.executable, "-m", "yt_dlp"]
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".3gp", ".flv")

TIKTOK_FMT = ("bv*[format_note!*=?watermarked]+ba/b[format_note!*=?watermarked]"
              "/bv*+ba/b")


def load_cfg():
    cfg = dict(DEFAULTS)
    try:
        with open(CONFIG_PATH, encoding="utf-8") as f:
            cfg.update(json.load(f))
    except (OSError, ValueError):
        pass
    return cfg


def save_cfg():
    try:
        with open(CONFIG_PATH, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=2)
    except OSError as e:
        say(f"Could not save settings: {e}", "r")


cfg = load_cfg()

COL = {"g": "\033[92m", "r": "\033[91m", "y": "\033[93m",
       "c": "\033[96m", "b": "\033[1m", "0": "\033[0m"}


def say(msg, col="c"):
    print(f"{COL[col]}{msg}{COL['0']}")


def ask(prompt, default=""):
    try:
        v = input(f"{COL['y']}{prompt}{COL['0']} ").strip()
    except EOFError:
        return default
    return v or default


def ask_url():
    url = ask("Paste link:").strip("'\" ")
    if not url.lower().startswith("http"):
        say("That doesn't look like a link.", "r")
        return None
    return url


def pause():
    try:
        input("\nPress Enter to continue...")
    except EOFError:
        pass


def clean(text):
    text = re.sub(r'[\\/:*?"<>|$%]', "_", text or "").strip().strip(".")
    return text or "Untitled"


def clean_path(p):
    return os.path.expanduser(p.strip().strip("'\""))


def run(cmd):
    print()
    try:
        return subprocess.run(cmd).returncode == 0
    except FileNotFoundError:
        say(f"Command not found: {cmd[0]}", "r")
        return False


NEXT_TAG = "@@NEXT@@"
DONE_TAG = "@@DONE@@"


def clean_print_args(playlist):
    if playlist:
        item = "[%(playlist_index)s/%(n_entries)s] %(title)s"
    else:
        item = "%(title)s"
    return ["--quiet", "--no-warnings", "--no-simulate",
            "--print", f"before_dl:{NEXT_TAG}{item}",
            "--print", f"after_move:{DONE_TAG}{item}"]


def run_clean(cmd, seen):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             text=True, errors="replace", bufsize=1, env=env)
    except FileNotFoundError:
        say(f"Command not found: {cmd[0]}", "r")
        return False, False
    retryable = False
    try:
        for raw in p.stdout:
            line = raw.strip()
            if line.startswith(NEXT_TAG):
                item = line[len(NEXT_TAG):]
                if item not in seen:
                    say(f"  → Next: {item}", "c")
            elif line.startswith(DONE_TAG):
                item = line[len(DONE_TAG):]
                if item not in seen:
                    seen.add(item)
                    say(f"  ✔ Done: {item}", "g")
            elif line.startswith("ERROR"):
                msg = re.sub(r"^ERROR:\s*", "", line)
                say(f"  ✖ {msg[:110]}", "r")
                if "403" in msg or "Sign in" in msg or "bot" in msg.lower():
                    retryable = True
        p.wait()
    except KeyboardInterrupt:
        p.terminate()
        raise
    return p.returncode == 0, retryable


CLIENT_SETS = [
    None,
    "tv,web_safari,mweb",
    "web,mweb,android,tv",
    "ios,android_vr",
]


def run_yt(cmd, url, clean=False):
    tries = CLIENT_SETS if "youtu" in url.lower() else [None]
    seen = set()
    for i, clients in enumerate(tries):
        c = list(cmd)
        if clients:
            c += ["--extractor-args", f"youtube:player_client={clients}"]
        if clean:
            ok, retryable = run_clean(c + [url], seen)
        else:
            ok, retryable = run(c + [url]), True
        if ok:
            return True
        if not retryable:
            break
        if i < len(tries) - 1:
            say("\n  Some downloads failed - retrying with another method "
                f"({i + 1}/{len(tries) - 1})...", "y")
    return False


def snapshot(folder):
    try:
        return set(os.listdir(folder))
    except OSError:
        return set()


def cleanup_orphans(folder, before):
    if not os.path.isdir(folder):
        return
    audio = (".mp3", ".m4a", ".flac", ".opus", ".mp4", ".mkv", ".webm")
    names = os.listdir(folder)
    stems = {os.path.splitext(n)[0] for n in names if n.lower().endswith(audio)}
    for n in names:
        if n in before:
            continue
        stem, ext = os.path.splitext(n)
        if ext.lower() in (".jpg", ".webp", ".part", ".ytdl") and stem not in stems:
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def media_scan(path):
    tool = shutil.which("termux-media-scan")
    if tool and os.path.exists(path):
        subprocess.run([tool, "-r", path],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)


def platform_of(url):
    u = url.lower()
    if "tiktok.com" in u:
        return "tiktok"
    if "instagram.com" in u:
        return "instagram"
    if "youtu" in u:
        return "youtube"
    return "other"


PLATFORM_DIR = {"tiktok": "TikTok", "instagram": "Instagram",
                "youtube": "YouTube", "other": "Other"}

IS_TERMUX = bool(shutil.which("pkg")) and os.path.isdir("/data/data/com.termux")
STALE_DAYS = 30
ICONS = {"ok": ("✔", "g"), "bad": ("✖", "r"), "warn": ("⚠", "y")}


def _pip_version(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def _works(cmd):
    try:
        return subprocess.run(cmd, stdout=subprocess.DEVNULL,
                              stderr=subprocess.DEVNULL, timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def _days_old(version):
    m = re.match(r"(\d{4})\.(\d{1,2})\.(\d{1,2})", version or "")
    if not m:
        return None
    try:
        return (datetime.date.today() - datetime.date(*map(int, m.groups()))).days
    except ValueError:
        return None


def scan_requirements():
    report = []
    plan = {"storage": False, "pkg_upgrade": False,
            "pkg": [], "pip_pre": [], "pip": []}
    ok_pip = []

    if find_spec("pip") is None:
        report.append(("bad", "pip - missing"))
        plan["pkg"].append("python-pip")

    v = _pip_version("yt-dlp")
    if v is None:
        report.append(("bad", "yt-dlp - not installed"))
        plan["pip_pre"].append("yt-dlp")
    else:
        age = _days_old(v)
        snoozed = time.time() - cfg.get("last_update", 0) < 7 * 86400
        if age is not None and age > STALE_DAYS and not snoozed:
            report.append(("warn", f"yt-dlp {v} - {age} days old, update needed"))
            plan["pip_pre"].append("yt-dlp")
        else:
            ok_pip.append(f"yt-dlp {v}")

    for name, bucket in (("yt-dlp-ejs", "pip_pre"), ("mutagen", "pip")):
        if _pip_version(name) is None:
            report.append(("bad", f"{name} - not installed"))
            plan[bucket].append(name)
        else:
            ok_pip.append(name)
    if ok_pip:
        report.insert(0, ("ok", "Python packages: " + ", ".join(ok_pip)))

    if not shutil.which("ffmpeg"):
        report.append(("bad", "ffmpeg - missing (needed for MP3 / merging video)"))
        plan["pkg"].append("ffmpeg")
    elif not _works(["ffmpeg", "-version"]):
        report.append(("bad", "ffmpeg - installed but broken (needs package upgrade)"))
        plan["pkg_upgrade"] = True
        plan["pkg"].append("ffmpeg")
    else:
        report.append(("ok", "ffmpeg"))

    if not shutil.which("node") or not _works(["node", "--version"]):
        report.append(("bad", "Node.js - missing (needed for YouTube)"))
        plan["pkg"].append("nodejs")
    else:
        report.append(("ok", "Node.js"))

    if IS_TERMUX:
        if os.access("/storage/emulated/0", os.W_OK):
            report.append(("ok", "Storage permission"))
        else:
            report.append(("bad", "Storage permission - not granted"))
            plan["storage"] = True

    return report, plan


def print_report(report):
    for kind, text in report:
        icon, col = ICONS[kind]
        say(f"  {icon} {text}", col)


def install_requirements(plan):
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    pip = [sys.executable, "-m", "pip", "install", "-U"]
    if IS_TERMUX:
        if plan["storage"]:
            say("\nStorage permission - tap ALLOW on the popup.", "b")
            if shutil.which("termux-setup-storage"):
                subprocess.call(["termux-setup-storage"])
            ask("Press Enter after allowing...")
        if plan["pkg_upgrade"]:
            say("\nUpgrading Termux packages (fixes broken ffmpeg)...", "b")
            subprocess.call(["pkg", "upgrade", "-y"], env=env)
            subprocess.call(["dpkg", "--configure", "-a"], env=env)
        if plan["pkg"]:
            say(f"\nInstalling: {' '.join(plan['pkg'])}", "b")
            subprocess.call(["pkg", "install", "-y", *plan["pkg"]], env=env)
    elif plan["pkg"] or plan["pkg_upgrade"]:
        say("\nNot running in Termux - install ffmpeg / Node.js with your "
            "system package manager.", "y")
    if plan["pip_pre"]:
        say("\nInstalling / updating yt-dlp (newest build)...", "b")
        subprocess.call(pip + ["--pre"] + plan["pip_pre"])
        cfg["last_update"] = time.time()
        save_cfg()
    if plan["pip"]:
        say(f"\nInstalling: {' '.join(plan['pip'])}", "b")
        subprocess.call(pip + plan["pip"])
    invalidate_caches()


def check_requirements(force=False):
    say("Checking requirements...", "y")
    report, plan = scan_requirements()
    problems = any(k in ("bad", "warn") for k, _ in report)

    if not problems:
        say("✔ All requirements are installed and up to date.", "g")
        if force:
            print_report(report)
        return force

    say("\nSome requirements are missing or outdated:", "b")
    print_report(report)

    if not force and time.time() < cfg.get("ignore_until", 0):
        say("\nSkipped for now - use menu option 11 to install them.", "y")
        return True

    ans = ask("\nInstall all requirements? (Press ENTER = yes, n = no):").lower()
    if ans not in ("", "y", "yes"):
        cfg["ignore_until"] = time.time() + 3 * 86400
        save_cfg()
        say("\nSkipped. Some features may not work until you install them.", "y")
        return True

    install_requirements(plan)
    say("\nRe-checking...", "y")
    report, plan = scan_requirements()
    print_report(report)
    if any(k in ("bad", "warn") for k, _ in report):
        cfg["ignore_until"] = time.time() + 3 * 86400
        save_cfg()
        say("\nSome items could not be fixed automatically - see above. "
            "You can continue; this won't be asked again for 3 days "
            "(menu option 11 to retry).", "r")
    else:
        say("\n✔ Everything is ready!", "g")
    return True


def menu_requirements():
    say("\n== Check / install requirements ==")
    cfg["ignore_until"] = 0
    check_requirements(force=True)


_flag_cache = {}


def ytdlp_has(flag):
    if flag not in _flag_cache:
        r = subprocess.run(YT_DLP + ["--help"], capture_output=True, text=True)
        _flag_cache[flag] = flag in r.stdout
    return _flag_cache[flag]


def base_args():
    a = []
    ck = cfg.get("cookies")
    if ck and os.path.isfile(ck):
        a += ["--cookies", ck]
    if shutil.which("node") and ytdlp_has("--js-runtimes"):
        a += ["--js-runtimes", "node"]
    return a


def dl_args():
    return base_args() + ["--no-mtime", "--concurrent-fragments", "4",
                          "--ignore-errors", "--embed-metadata",
                          "--trim-filenames", "120"]


def probe(url, extra=None, flat=True):
    cmd = YT_DLP + base_args() + ["--dump-single-json", "--no-warnings"]
    if flat:
        cmd.append("--flat-playlist")
    cmd += (extra or []) + [url]
    r = subprocess.run(cmd, capture_output=True, text=True)
    try:
        return json.loads(r.stdout)
    except ValueError:
        err = (r.stderr.strip().splitlines() or ["unknown error"])[-1]
        say(err, "r")
        return None

ALBUM_PREFIX = re.compile(r"^(Album|Single|EP)\s*-\s*", re.I)


def music_ytdlp(url):
    single = False
    if "list=" in url and re.search(r"[?&]v=", url):
        single = ask("Song inside a playlist. 1 = this song only, "
                     "2 = whole playlist [1]:", "1") == "1"
    say("Reading link...", "y")
    info = probe(url, ["--no-playlist"] if single else None)
    if info is None:
        say("Could not read that link.", "r")
        return False

    fmt = cfg["audio_format"]
    cmd = YT_DLP + dl_args() + ["-x", "--audio-format", fmt,
                                "--audio-quality", "0"]
    if fmt in ("mp3", "m4a"):
        cmd += ["--embed-thumbnail", "--convert-thumbnails", "jpg"]

    if info.get("_type") == "playlist" and not single:
        title = info.get("title") or "Playlist"
        is_album = "list=OLAK5uy" in url or bool(ALBUM_PREFIX.match(title))
        album = ALBUM_PREFIX.sub("", title)
        outdir = os.path.join(cfg["music_dir"], clean(album))
        count = info.get("playlist_count") or len(info.get("entries") or [])
        say(f"\nAlbum/playlist: {album}  ({count} tracks)", "b")
        say(f"Saving to: {outdir}", "y")
        cmd += ["--yes-playlist", "-P", outdir,
                "-o", "%(playlist_index)02d - %(title)s.%(ext)s",
                "--parse-metadata", "%(playlist_index)s:%(track_number)s"]
        if not is_album:
            cmd += ["--parse-metadata", "%(playlist_title)s:%(album)s"]
        cmd += clean_print_args(playlist=True)
    else:
        outdir = cfg["music_dir"]
        say(f"\nSaving to: {outdir}", "y")
        cmd += ["--no-playlist", "-P", outdir, "-o", "%(title)s.%(ext)s"]
        cmd += clean_print_args(playlist=False)

    before = snapshot(outdir)
    ok = run_yt(cmd, url, clean=True)
    if not ok:
        cleanup_orphans(outdir, before)
        say("Some or all tracks failed. Try menu 9 (Update tools), then retry "
            "- finished tracks are skipped automatically.", "r")
    media_scan(outdir)
    if ok:
        say(f"Done -> {outdir}", "g")
    return ok


SPOTIFY_RE = re.compile(
    r"(?:open\.spotify\.com/(?:intl-[a-z-]+/)?(?:embed/)?|spotify:)"
    r"(track|album|playlist)[/:]([A-Za-z0-9]{22})")
BAD_WORDS = ("live", "cover", "remix", "karaoke", "instrumental",
             "reaction", "slowed", "sped up", "nightcore", "8d audio")


def http_get(url, limit=3_000_000):
    req = urllib.request.Request(
        url, headers={"User-Agent": DESKTOP_UA, "Accept-Language": "en-US,en;q=0.9"})
    with urllib.request.urlopen(req, timeout=15) as r:
        data = r.read(limit + 1)
        final = r.geturl()
    if len(data) > limit:
        raise ValueError("response too large")
    return data, final


def parse_spotify_link(url):
    m = SPOTIFY_RE.search(url)
    if not m and re.search(r"spotify\.link|spoti\.fi", url):
        try:
            _, final = http_get(url)
            m = SPOTIFY_RE.search(final)
        except Exception:
            m = None
    return (m.group(1), m.group(2)) if m else None


def _walk(obj):
    queue = deque([obj])
    while queue:
        cur = queue.popleft()
        if isinstance(cur, dict):
            yield cur
            queue.extend(cur.values())
        elif isinstance(cur, list):
            queue.extend(cur)


def _txt(v):
    return re.sub(r"\s+", " ", str(v or "")).strip()


def _names(d):
    artists = d.get("artists")
    if isinstance(artists, list):
        names = [_txt(a.get("name")) for a in artists
                 if isinstance(a, dict) and a.get("name")]
        if names:
            return ", ".join(names)
    return _txt(d.get("subtitle"))


def _ms(d):
    for key in ("duration", "durationMs", "duration_ms"):
        v = d.get(key)
        if isinstance(v, dict):
            v = v.get("totalMilliseconds")
        if isinstance(v, (int, float)) and v > 0:
            return int(v if v >= 1000 else v * 1000)
    return None


def _cover_url(ent):
    best, size = None, -1
    for d in _walk(ent):
        for key in ("sources", "image", "images"):
            images = d.get(key)
            if not isinstance(images, list):
                continue
            for im in images:
                if not isinstance(im, dict):
                    continue
                url = im.get("url")
                if not (isinstance(url, str) and url.startswith("https://")):
                    continue
                w = im.get("width") or im.get("maxWidth") or 0
                w = w if isinstance(w, (int, float)) else 0
                if w > size:
                    best, size = url, w
    return best


def _image_bytes(url):
    try:
        data, _ = http_get(url, 5_000_000)
    except Exception:
        return None
    return data if data[:2] == b"\xff\xd8" or data[:4] == b"\x89PNG" else None


def spotify_meta(kind, sid):
    data, _ = http_get(f"https://open.spotify.com/embed/{kind}/{sid}")
    html = data.decode("utf-8", "replace")
    m = re.search(r'<script id="__NEXT_DATA__"[^>]*>(.*?)</script>', html, re.S)
    if m:
        blob = m.group(1)
    else:
        m = re.search(r'<script id="resource"[^>]*>(.*?)</script>', html, re.S)
        if not m:
            raise ValueError("no data found in page")
        blob = urllib.parse.unquote(m.group(1))
    tree = json.loads(blob)

    wanted = f"spotify:{kind}:{sid}"
    ent = next((d for d in _walk(tree)
                if d.get("uri") == wanted and (d.get("name") or d.get("title"))), None)
    if ent is None:
        ent = next((d for d in _walk(tree)
                    if isinstance(d.get("trackList"), list)), None)
    if ent is None:
        raise ValueError("item not found in page")

    name = _txt(ent.get("name") or ent.get("title"))
    artist = _names(ent)
    tracks = []
    items = ent.get("trackList")
    if isinstance(items, list):
        for it in items:
            if not isinstance(it, dict):
                continue
            title = _txt(it.get("title") or it.get("name"))
            if title:
                tracks.append({"title": title, "artist": _names(it) or artist,
                               "ms": _ms(it)})
    elif kind == "track" and name:
        tracks.append({"title": name, "artist": artist, "ms": _ms(ent)})
    for i, t in enumerate(tracks, 1):
        t["n"] = i
    return {"kind": kind, "name": name, "artist": artist,
            "tracks": tracks, "cover": _cover_url(ent)}


def search_youtube(query, count=6):
    info = probe(f"ytsearch{count}:{query}")
    entries = (info or {}).get("entries") or []
    return [e for e in entries if isinstance(e, dict) and e.get("id")]


def pick_match(entries, title, artist, ms):
    target = ms / 1000 if ms else None
    title_l = title.lower()
    artist_l = artist.split(",")[0].strip().lower()
    best, best_score = None, None
    for e in entries:
        t = (e.get("title") or "").lower()
        ch = (e.get("channel") or e.get("uploader") or "").lower()
        score = 0
        dur = e.get("duration")
        if target and isinstance(dur, (int, float)):
            diff = abs(dur - target)
            score += 6 if diff <= 3 else 3 if diff <= 8 else -6 if diff > 20 else 0
        if ch.endswith("topic"):
            score += 4
        if artist_l and artist_l in f"{t} {ch}":
            score += 2
        if title_l in t:
            score += 2
        for w in BAD_WORDS:
            if w in t and w not in title_l:
                score -= 4
        if "audio" in t:
            score += 1
        if best_score is None or score > best_score:
            best, best_score = e, score
    return best if best_score is not None and best_score >= 0 else None


def tag_audio(path, title, artist, album, album_artist, track, total, cover):
    ext = os.path.splitext(path)[1].lower()
    mime = "image/png" if cover and cover[:4] == b"\x89PNG" else "image/jpeg"
    try:
        if ext == ".mp3":
            from mutagen.id3 import ID3, TALB, TIT2, TPE1, TPE2, TRCK, APIC
            tags = ID3()
            tags.add(TIT2(encoding=3, text=title))
            if artist:
                tags.add(TPE1(encoding=3, text=artist))
            if album:
                tags.add(TALB(encoding=3, text=album))
            if album_artist:
                tags.add(TPE2(encoding=3, text=album_artist))
            if track:
                tags.add(TRCK(encoding=3, text=f"{track}/{total}"))
            if cover:
                tags.add(APIC(encoding=3, mime=mime, type=3, desc="Cover", data=cover))
            tags.save(path, v2_version=3)
        elif ext in (".m4a", ".mp4"):
            from mutagen.mp4 import MP4, MP4Cover
            mf = MP4(path)
            mf["\xa9nam"] = [title]
            if artist:
                mf["\xa9ART"] = [artist]
            if album:
                mf["\xa9alb"] = [album]
            if album_artist:
                mf["aART"] = [album_artist]
            if track:
                mf["trkn"] = [(track, total)]
            if cover:
                fmt = MP4Cover.FORMAT_PNG if mime == "image/png" else MP4Cover.FORMAT_JPEG
                mf["covr"] = [MP4Cover(cover, imageformat=fmt)]
            mf.save()
        elif ext == ".flac":
            from mutagen.flac import FLAC, Picture
            mf = FLAC(path)
            mf["title"] = [title]
            if artist:
                mf["artist"] = [artist]
            if album:
                mf["album"] = [album]
            if album_artist:
                mf["albumartist"] = [album_artist]
            if track:
                mf["tracknumber"] = [str(track)]
            if cover:
                pic = Picture()
                pic.type = 3
                pic.mime = mime
                pic.data = cover
                mf.clear_pictures()
                mf.add_picture(pic)
            mf.save()
        else:
            import mutagen
            mf = mutagen.File(path)
            if mf is None:
                return
            mf["title"] = [title]
            if artist:
                mf["artist"] = [artist]
            if album:
                mf["album"] = [album]
            if track:
                mf["tracknumber"] = [str(track)]
            mf.save()
    except Exception:
        pass


def download_matched(tr, outdir, stem):
    entries = search_youtube(f"{tr['artist']} {tr['title']}".strip())
    match = pick_match(entries, tr["title"], tr["artist"], tr["ms"])
    if not match:
        return False
    cmd = YT_DLP + base_args() + [
        "-x", "--audio-format", cfg["audio_format"], "--audio-quality", "0",
        "--no-mtime", "--no-playlist", "--quiet", "--no-warnings",
        "-P", outdir, "-o", f"{stem}.%(ext)s"]
    return run_yt(cmd, f"https://www.youtube.com/watch?v={match['id']}", clean=True)


def search_and_download(query):
    say("Searching...", "y")
    entries = search_youtube(query, 5)
    if not entries:
        say("No results found.", "r")
        return False
    for i, e in enumerate(entries, 1):
        who = e.get("channel") or e.get("uploader") or ""
        print(f" {i}) {e.get('title')}  [{fmt_duration(e.get('duration'))}]  {who}")
    pick = ask("Choose number [1] (n = cancel):", "1")
    if pick.lower() == "n":
        return False
    idx = int(pick) - 1 if pick.isdigit() and 1 <= int(pick) <= len(entries) else 0
    return music_ytdlp(f"https://www.youtube.com/watch?v={entries[idx]['id']}")


def spotify_fallback(kind, sid):
    if kind != "track":
        say("Tip: paste this album's YouTube Music link in the music option, "
            "or search songs by name (option 12).", "y")
        return False
    title = ""
    try:
        api = ("https://open.spotify.com/oembed?url="
               + urllib.parse.quote(f"https://open.spotify.com/track/{sid}", safe=""))
        data, _ = http_get(api, 200_000)
        title = _txt(json.loads(data.decode("utf-8")).get("title"))
    except Exception:
        pass
    q = ask(f"Song name to search [{title or 'type it'}] (n = cancel):", title)
    if not q or q.lower() == "n":
        return False
    return search_and_download(q)


def music_spotify(url):
    ref = parse_spotify_link(url)
    if not ref:
        say("Only Spotify track, album and playlist links are supported.", "r")
        return False
    kind, sid = ref
    say("Reading Spotify link...", "y")
    try:
        meta = spotify_meta(kind, sid)
    except Exception as e:
        say(f"Could not read this Spotify link ({e}).", "r")
        return spotify_fallback(kind, sid)
    tracks = meta["tracks"]
    if not tracks:
        say("No tracks found in that link.", "r")
        return spotify_fallback(kind, sid)

    fmt = cfg["audio_format"]
    single = kind == "track"
    outdir = cfg["music_dir"] if single else os.path.join(cfg["music_dir"], clean(meta["name"]))
    total = len(tracks)
    if single:
        say(f"\nTrack: {meta['name']}", "b")
    else:
        say(f"\n{kind.title()}: {meta['name']}  ({total} tracks)", "b")
        if kind == "playlist" and total >= 50:
            say("Note: Spotify only shares the first ~50 tracks of a playlist.", "y")
    say(f"Saving to: {outdir}", "y")

    cover = _image_bytes(meta["cover"]) if meta["cover"] else None
    done = 0
    for tr in tracks:
        label = f"{tr['artist']} - {tr['title']}" if tr["artist"] else tr["title"]
        prefix = "" if single else f"[{tr['n']:02d}/{total}] "
        stem = f"{tr['n']:02d} - {clean(tr['title'])}" if kind == "album" else clean(label)
        dest = os.path.join(outdir, f"{stem}.{fmt}")
        if os.path.exists(dest):
            say(f"  ✔ Already saved: {prefix}{label}", "g")
            done += 1
            continue
        say(f"  → Next: {prefix}{label}", "c")
        if download_matched(tr, outdir, stem) and os.path.exists(dest):
            tag_audio(dest, tr["title"], tr["artist"],
                      "" if single else meta["name"],
                      meta["artist"] if kind == "album" else "",
                      0 if single else tr["n"], total, cover)
            say(f"  ✔ Done: {prefix}{label}", "g")
            done += 1
        else:
            say(f"  ✖ Could not download: {prefix}{label}", "r")
    media_scan(outdir)
    say(f"\nFinished: {done}/{total} tracks -> {outdir}", "g" if done == total else "y")
    return done == total


def music_link(url):
    if "spotify.com" in url.lower():
        return music_spotify(url)
    return music_ytdlp(url)


def menu_search():
    say("\n== Search & download song by name ==")
    q = ask("Song name (e.g. Rick Astley Never Gonna Give You Up):")
    if q:
        search_and_download(q)


def menu_music():
    say("\n== Music download ==  (YouTube, YouTube Music, Spotify, SoundCloud...)")
    say("Albums/playlists are saved in a folder named after the album.", "y")
    url = ask_url()
    if url:
        music_link(url)


def tiktok_backup(url, outdir):
    try:
        api = "https://www.tikwm.com/api/?hd=1&url=" + urllib.parse.quote(url, safe="")
        req = urllib.request.Request(api, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=30) as r:
            data = json.load(r)
        if data.get("code") != 0:
            raise RuntimeError(data.get("msg", "unknown error"))
        d = data["data"]
        link = d.get("hdplay") or d.get("play")
        if not link:
            raise RuntimeError("no video found (photo post?)")
        if link.startswith("/"):
            link = "https://www.tikwm.com" + link
        user = clean((d.get("author") or {}).get("unique_id") or "tiktok")
        dest = os.path.join(outdir, f"{user}_{d.get('id', 'video')}.mp4")
        os.makedirs(outdir, exist_ok=True)
        req = urllib.request.Request(link, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=60) as r, open(dest, "wb") as f:
            total = int(r.headers.get("Content-Length") or 0)
            done = 0
            while True:
                chunk = r.read(65536)
                if not chunk:
                    break
                f.write(chunk)
                done += len(chunk)
                if total:
                    print(f"\r  {done * 100 // total}%  ", end="", flush=True)
        print()
        say(f"Saved: {dest}", "g")
        return True
    except Exception as e:
        say(f"Backup failed: {e}", "r")
        return False


def download_video(url, height=None):
    plat = platform_of(url)
    outdir = os.path.join(cfg["video_dir"], PLATFORM_DIR[plat])
    cmd = YT_DLP + dl_args() + ["--merge-output-format", "mp4"]

    if plat == "tiktok":
        cmd += ["-f", TIKTOK_FMT]
    else:
        if height:
            cmd += ["-f", f"bv*[height<=?{height}]+ba/b[height<=?{height}]"]
        else:
            cmd += ["-f", "bv*+ba/b"]
        cmd += ["-S", "res,vcodec:h264,acodec:m4a"]

    playlist = False
    if plat in ("youtube", "other") and "list=" in url:
        if re.search(r"[?&]v=", url):
            playlist = ask("Video inside a playlist. 1 = this video only, "
                           "2 = whole playlist [1]:", "1") == "2"
        else:
            playlist = True

    if playlist:
        info = probe(url)
        title = clean(info.get("title")) if info else "Playlist"
        outdir = os.path.join(outdir, title)
        cmd += ["--yes-playlist", "-P", outdir,
                "-o", "%(playlist_index)02d - %(title)s.%(ext)s"]
    elif plat in ("tiktok", "instagram"):
        cmd += ["--no-playlist", "-P", outdir,
                "-o", "%(uploader|user)s_%(id)s.%(ext)s"]
    else:
        cmd += ["--no-playlist", "-P", outdir, "-o", "%(title)s.%(ext)s"]

    before = snapshot(outdir)
    ok = run_yt(cmd, url)
    if not ok:
        cleanup_orphans(outdir, before)
    if not ok and plat == "tiktok":
        say("Trying backup method...", "y")
        ok = tiktok_backup(url, outdir)
    if not ok and plat == "instagram":
        say("Instagram may need login. Export cookies.txt from your browser "
            "and set it in Settings.", "y")
    media_scan(outdir)
    if ok:
        say(f"Done -> {outdir}", "g")
    return ok


def menu_video():
    say("\n== Video download ==  (YouTube, TikTok, Instagram, others)")
    say("TikTok = no watermark. Instagram = original quality.", "y")
    url = ask_url()
    if not url:
        return
    height = None
    if platform_of(url) in ("youtube", "other"):
        print("Quality: 1) Best  2) 1080p  3) 720p  4) 480p")
        height = {"2": 1080, "3": 720, "4": 480}.get(ask("Choose [1]:", "1"))
    download_video(url, height)


def menu_batch():
    say("\n== Batch download ==")
    path = clean_path(ask("Path to .txt file (one link per line):"))
    try:
        with open(path, encoding="utf-8") as f:
            links = [l.strip() for l in f
                     if l.strip() and not l.startswith("#")]
    except OSError as e:
        say(f"Cannot open file: {e}", "r")
        return
    if not links:
        say("No links found.", "r")
        return
    mode = ask("1 = music, 2 = video (best quality) [1]:", "1")
    ok_count = 0
    for i, link in enumerate(links, 1):
        say(f"\n[{i}/{len(links)}] {link}", "b")
        ok = music_link(link) if mode == "1" else download_video(link)
        ok_count += bool(ok)
    say(f"\nFinished: {ok_count}/{len(links)} succeeded.", "g")


def menu_subs():
    say("\n== Download subtitles ==")
    url = ask_url()
    if not url:
        return
    lang = ask("Language code (en, id, es, ...) [en]:", "en")
    outdir = os.path.join(cfg["video_dir"], "Subtitles")
    run(YT_DLP + base_args() + ["--skip-download", "--write-subs",
                                "--write-auto-subs", "--sub-langs", lang,
                                "--convert-subs", "srt", "--no-playlist",
                                "-P", outdir, "-o", "%(title)s.%(ext)s", url])
    say(f"Subtitles folder: {outdir}", "g")


def menu_thumb():
    say("\n== Download thumbnail ==")
    url = ask_url()
    if not url:
        return
    outdir = os.path.join(cfg["video_dir"], "Thumbnails")
    run(YT_DLP + base_args() + ["--skip-download", "--write-thumbnail",
                                "--convert-thumbnails", "jpg", "--no-playlist",
                                "-P", outdir, "-o", "%(title)s.%(ext)s", url])
    media_scan(outdir)
    say(f"Thumbnails folder: {outdir}", "g")


def menu_convert():
    say("\n== Convert local video(s) to MP3 ==")
    if not shutil.which("ffmpeg"):
        say("ffmpeg missing. Run: pkg install ffmpeg", "r")
        return
    src = clean_path(ask("Video file or folder path:"))
    if os.path.isdir(src):
        files = [os.path.join(src, f) for f in sorted(os.listdir(src))
                 if f.lower().endswith(VIDEO_EXT)]
    elif os.path.isfile(src):
        files = [src]
    else:
        say("Path not found.", "r")
        return
    if not files:
        say("No video files found there.", "r")
        return
    os.makedirs(cfg["music_dir"], exist_ok=True)
    for f in files:
        name = os.path.splitext(os.path.basename(f))[0] + ".mp3"
        out = os.path.join(cfg["music_dir"], name)
        say(f"-> {name}", "b")
        run(["ffmpeg", "-y", "-loglevel", "error", "-i", f, "-vn",
             "-codec:a", "libmp3lame", "-q:a", "0", out])
    media_scan(cfg["music_dir"])
    say(f"Saved in {cfg['music_dir']}", "g")


def fmt_duration(sec):
    if not sec:
        return "?"
    sec = int(sec)
    h, rem = divmod(sec, 3600)
    m, s = divmod(rem, 60)
    return f"{h}:{m:02d}:{s:02d}" if h else f"{m}:{s:02d}"


def menu_info():
    say("\n== Video info ==")
    url = ask_url()
    if not url:
        return
    info = probe(url, ["--no-playlist"], flat=False)
    if not info:
        return
    heights = sorted({f["height"] for f in info.get("formats", [])
                      if f.get("height")})
    print(f"\nTitle    : {info.get('title')}")
    print(f"Uploader : {info.get('uploader') or info.get('channel')}")
    print(f"Duration : {fmt_duration(info.get('duration'))}")
    print(f"Views    : {info.get('view_count')}")
    print(f"Qualities: {', '.join(f'{h}p' for h in heights) or 'n/a'}")

CHECK_EVERY = 24 * 3600
RAW_BASE = UPDATE_REPO.replace("https://github.com/", "https://raw.githubusercontent.com/")
BRANCHES = ("main", "master")
VERSION_RE = re.compile(r"^\d+(\.\d+){0,3}$")


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def fetch_repo_file(name, limit=2_000_000):
    err = "unknown error"
    for branch in BRANCHES:
        try:
            req = urllib.request.Request(
                f"{RAW_BASE}/{branch}/{name}",
                headers={"User-Agent": UA, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(limit + 1)
            if len(data) > limit:
                err = "file is too large"
                continue
            return data.decode("utf-8"), None
        except Exception as e:
            err = str(e)
    return None, err


def read_version_info():
    text, err = fetch_repo_file("version.json")
    if text is None:
        return None, err
    try:
        info = json.loads(text)
        version = str(info["version"]).strip()
        if not VERSION_RE.match(version):
            raise ValueError
    except (ValueError, KeyError, TypeError):
        return None, "version.json is invalid"
    notes = re.sub(r"[^\x20-\x7e\u00a0-\uffff]", "", str(info.get("changelog", "")))
    return {"version": version, "changelog": notes[:300]}, None


def apply_script_update(code, new_ver):
    m = re.search(r"^__version__\s*=\s*[\"']([\d.]+)[\"']", code, re.M)
    if not m or "def main" not in code:
        say("Downloaded file is not a valid mediadl.py - update cancelled.", "r")
        return False
    if _ver_tuple(m.group(1)) < _ver_tuple(new_ver):
        say("The new file is not ready on GitHub yet. Try again in a few minutes.", "y")
        return False
    try:
        compile(code, "mediadl.py", "exec")
    except SyntaxError as e:
        say(f"Downloaded file is broken ({e.msg}) - update cancelled.", "r")
        return False
    path = os.path.abspath(__file__)
    try:
        shutil.copy2(path, path + ".bak")
        tmp = path + ".new"
        with open(tmp, "w", encoding="utf-8", newline="\n") as f:
            f.write(code)
        os.replace(tmp, path)
    except OSError as e:
        say(f"Could not replace the script file: {e}", "r")
        say("Tip: keep mediadl.py in your Termux home (~) and run "
            "it from there.", "y")
        return False
    say(f"✔ Updated to v{new_ver}  (old version saved as mediadl.py.bak)", "g")
    say("Restarting...", "y")
    os.execv(sys.executable, [sys.executable, path] + sys.argv[1:])


def check_script_update(auto=False):
    if auto and time.time() - cfg.get("last_update_check", 0) < CHECK_EVERY:
        return
    if not auto:
        say("Checking for a new version...", "y")
    info, err = read_version_info()
    if info is None:
        if not auto:
            say(f"Could not check for updates: {err}", "r")
        return
    cfg["last_update_check"] = time.time()
    save_cfg()
    remote = info["version"]
    if _ver_tuple(remote) <= _ver_tuple(__version__):
        if not auto:
            say(f"✔ You have the latest version (v{__version__}).", "g")
        return
    say(f"\n🔔 New version available: v{remote}  (you have v{__version__})", "b")
    if info["changelog"]:
        say(f"What's new: {info['changelog']}", "c")
    ans = ask("Update now? (Press ENTER = yes, n = later):").lower()
    if ans not in ("", "y", "yes"):
        say("OK - you can update any time from menu option 10.", "y")
        return
    say("Downloading...", "y")
    code, err = fetch_repo_file("mediadl.py")
    if code is None:
        say(f"Download failed: {err}", "r")
        return
    apply_script_update(code, remote)


def menu_script_update():
    say("\n== Check for script update ==")
    check_script_update(auto=False)


def menu_settings():
    while True:
        say("\n== Settings ==")
        print(f"1) Music folder : {cfg['music_dir']}")
        print(f"2) Video folder : {cfg['video_dir']}")
        print(f"3) Audio format : {cfg['audio_format']}")
        print(f"4) Cookies file : {cfg['cookies'] or '(none)'}")
        print("0) Back")
        c = ask("Choose:")
        if c == "1":
            cfg["music_dir"] = clean_path(ask("New music folder:", cfg["music_dir"]))
        elif c == "2":
            cfg["video_dir"] = clean_path(ask("New video folder:", cfg["video_dir"]))
        elif c == "3":
            f = ask("Format (mp3 / m4a / flac / opus):", cfg["audio_format"]).lower()
            if f in ("mp3", "m4a", "flac", "opus"):
                cfg["audio_format"] = f
            else:
                say("Unsupported format.", "r")
        elif c == "4":
            cfg["cookies"] = clean_path(ask("Path to cookies.txt (blank = none):"))
        elif c == "0":
            break
        save_cfg()


def menu_update():
    say("\n== Updating tools ==")
    say("Installing yt-dlp NIGHTLY (newest YouTube fixes)...", "y")
    subprocess.call([sys.executable, "-m", "pip", "install", "-U", "--pre",
                     "yt-dlp", "yt-dlp-ejs"])
    cfg["last_update"] = time.time()
    save_cfg()
    _flag_cache.clear()
    say("Update finished.", "g")

MENU = [
    ("1", "Music download (YouTube / Spotify / SoundCloud)", menu_music),
    ("2", "Video download (YouTube / TikTok / Instagram)", menu_video),
    ("3", "Batch download from a .txt file", menu_batch),
    ("4", "Download subtitles", menu_subs),
    ("5", "Download thumbnail", menu_thumb),
    ("6", "Convert local video(s) to MP3", menu_convert),
    ("7", "Video info & available qualities", menu_info),
    ("8", "Settings", menu_settings),
    ("9", "Update tools", menu_update),
    ("10", "Check for script update", menu_script_update),
    ("11", "Check / install requirements", menu_requirements),
    ("12", "Search & download song by name", menu_search),
]


def main():
    if check_requirements():
        pause()
    check_script_update(auto=True)
    while True:
        os.system("clear")
        say("=" * 46, "c")
        say(f"  MediaDL by mrk  -  Termux Downloader v{__version__}", "b")
        say("=" * 46, "c")
        for key, label, _ in MENU:
            print(f" {key}) {label}")
        print(" 0) Exit")
        choice = ask("\nChoose:")
        if choice == "0":
            say("Bye!", "g")
            break
        action = next((fn for k, _, fn in MENU if k == choice), None)
        if not action:
            continue
        try:
            action()
        except KeyboardInterrupt:
            say("\nCancelled.", "y")
        if choice != "8":
            pause()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print()
