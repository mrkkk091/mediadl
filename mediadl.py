#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
MediaDL - all-in-one downloader for Termux

Just run it - on start it checks everything it needs (ffmpeg, Node.js,
yt-dlp, storage permission...) and offers to install / update what is missing:
    python mediadl.py
"""
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
from importlib import invalidate_caches
from importlib.util import find_spec

__version__ = "1.0.0"     # bump this when you publish a new version to your repo

# --------------------------------------------------------------------------
# Config
# --------------------------------------------------------------------------
CONFIG_PATH = os.path.expanduser("~/.mediadl.json")
DEFAULTS = {
    "music_dir": "/storage/emulated/0/Music",
    "video_dir": "/storage/emulated/0/Movies",
    "audio_format": "mp3",   # mp3 / m4a / flac / opus
    "cookies": "",           # path to cookies.txt (Netscape format), optional
    "last_update": 0,        # when tools were last updated (epoch seconds)
    "ignore_until": 0,       # requirement prompt snoozed until (epoch seconds)
    "spotdl_declined": False,
    "update_url": "",        # your GitHub repo / raw link of mediadl.py
    "last_update_check": 0,  # last time the script checked its repo
}
UA = ("Mozilla/5.0 (Linux; Android 13; Mobile) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0 Mobile Safari/537.36")
YT_DLP = [sys.executable, "-m", "yt_dlp"]
SPOTDL = [sys.executable, "-m", "spotdl"]
VIDEO_EXT = (".mp4", ".mkv", ".webm", ".mov", ".avi", ".3gp", ".flv")

# TikTok: skip the "watermarked" format, prefer the clean one
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

# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
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
    """Make a string safe for a folder/file name."""
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
    """yt-dlp flags: silence everything except our own Next/Done lines."""
    if playlist:
        item = "[%(playlist_index)s/%(n_entries)s] %(title)s"
    else:
        item = "%(title)s"
    return ["--quiet", "--no-warnings", "--no-simulate",
            "--print", f"before_dl:{NEXT_TAG}{item}",
            "--print", f"after_move:{DONE_TAG}{item}"]


def run_clean(cmd, seen):
    """Run yt-dlp quietly and show only 'next' / 'done' lines (+ short errors)."""
    print()
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


# YouTube sometimes returns "HTTP 403" for one player client but not another.
# We try the default first, then fall back to other clients automatically.
CLIENT_SETS = [
    None,                      # yt-dlp default
    "tv,web_safari,mweb",
    "web,mweb,android,tv",
    "ios,android_vr",
]


def run_yt(cmd, url, clean=False):
    """Run a yt-dlp command; on failure retry with different YouTube clients."""
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
        if not retryable:      # e.g. video removed - another method won't help
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
    """Remove thumbnails / partial files left by failed downloads.
    Only touches files that were created during this download."""
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
    """Make new files show up in the gallery / music app right away."""
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

# --------------------------------------------------------------------------
# Setup checks
# --------------------------------------------------------------------------
IS_TERMUX = bool(shutil.which("pkg")) and os.path.isdir("/data/data/com.termux")
STALE_DAYS = 30          # yt-dlp older than this is flagged (YouTube changes often)
ICONS = {"ok": ("✔", "g"), "bad": ("✖", "r"), "warn": ("⚠", "y"), "opt": ("○", "c")}


def _pip_version(name):
    try:
        return md.version(name)
    except md.PackageNotFoundError:
        return None


def _works(cmd):
    """True if the command runs successfully (catches broken installs)."""
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


def scan_requirements(show_optional=False):
    """Check everything (offline, fast). Returns (report, plan)."""
    report = []
    plan = {"storage": False, "pkg_upgrade": False,
            "pkg": [], "pip_pre": [], "pip": []}
    ok_pip = []

    # pip itself
    if find_spec("pip") is None:
        report.append(("bad", "pip - missing"))
        plan["pkg"].append("python-pip")

    # yt-dlp (and how old it is)
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

    # ffmpeg (test that it really runs - a bad upgrade can break it)
    if not shutil.which("ffmpeg"):
        report.append(("bad", "ffmpeg - missing (needed for MP3 / merging video)"))
        plan["pkg"].append("ffmpeg")
    elif not _works(["ffmpeg", "-version"]):
        report.append(("bad", "ffmpeg - installed but broken (needs package upgrade)"))
        plan["pkg_upgrade"] = True
        plan["pkg"].append("ffmpeg")
    else:
        report.append(("ok", "ffmpeg"))

    # Node.js (YouTube needs a JS runtime)
    if not shutil.which("node") or not _works(["node", "--version"]):
        report.append(("bad", "Node.js - missing (needed for YouTube)"))
        plan["pkg"].append("nodejs")
    else:
        report.append(("ok", "Node.js"))

    if IS_TERMUX:
        if not shutil.which("termux-media-scan"):
            report.append(("warn", "termux-tools - missing (files won't show in gallery at once)"))
            plan["pkg"].append("termux-tools")
        if os.access("/storage/emulated/0", os.W_OK):
            report.append(("ok", "Storage permission"))
        else:
            report.append(("bad", "Storage permission - not granted"))
            plan["storage"] = True

    # Spotify support is optional (heavy to install)
    if find_spec("spotdl") is not None:
        report.append(("ok", "spotdl (Spotify)"))
    elif show_optional or not cfg.get("spotdl_declined"):
        report.append(("opt", "spotdl - not installed (optional, for Spotify)"))
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


def install_spotdl():
    env = dict(os.environ, DEBIAN_FRONTEND="noninteractive")
    if IS_TERMUX:
        subprocess.call(["pkg", "install", "-y", "rust", "binutils", "clang",
                         "cmake", "ninja"], env=env)
        try:
            api = subprocess.run(["getprop", "ro.build.version.sdk"],
                                 capture_output=True, text=True).stdout.strip()
        except OSError:
            api = ""
        env["ANDROID_API_LEVEL"] = api or "24"
    say("\nInstalling spotdl - this can take 10-20 minutes, please wait...", "y")
    subprocess.call([sys.executable, "-m", "pip", "install", "-U", "spotdl"], env=env)
    invalidate_caches()
    if find_spec("spotdl"):
        say("✔ spotdl installed.", "g")
    else:
        say("✖ spotdl could not be installed. Everything else still works.", "r")


def offer_spotdl(force=False):
    if find_spec("spotdl") is not None:
        return
    if cfg.get("spotdl_declined") and not force:
        return
    ans = ask("\nInstall Spotify support (spotdl)? Takes 10-20 min, may fail. "
              "[y/N]:", "n").lower()
    if ans.startswith("y"):
        cfg["spotdl_declined"] = False
        install_spotdl()
    else:
        cfg["spotdl_declined"] = True
    save_cfg()


def check_requirements(force=False):
    """Startup check. Returns True if anything was shown to the user."""
    say("Checking requirements...", "y")
    report, plan = scan_requirements(show_optional=force)
    problems = any(k in ("bad", "warn") for k, _ in report)

    if not problems:
        say("✔ All requirements are installed and up to date.", "g")
        if force:
            print_report(report)
        offer_spotdl(force)
        return force

    say("\nSome requirements are missing or outdated:", "b")
    print_report(report)

    if not force and time.time() < cfg.get("ignore_until", 0):
        say("\nSkipped for now - use menu option 11 to install them.", "y")
        return True

    ans = ask("\nInstall all requirements? (Press ENTER = yes, n = no):").lower()
    if ans in ("", "y", "yes"):
        install_requirements(plan)
        say("\nRe-checking...", "y")
        report, plan = scan_requirements(show_optional=force)
        print_report(report)
        if any(k in ("bad", "warn") for k, _ in report):
            say("\nSome items still have problems - see above. "
                "You can continue, but those features may not work.", "r")
        else:
            say("\n✔ Everything is ready!", "g")
    else:
        cfg["ignore_until"] = time.time() + 3 * 86400
        save_cfg()
        say("\nSkipped. Some features may not work until you install them.", "y")
        return True
    offer_spotdl(force)
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
    # YouTube needs a JS runtime for some formats; use Node if available
    if shutil.which("node") and ytdlp_has("--js-runtimes"):
        a += ["--js-runtimes", "node"]
    return a


def dl_args():
    return base_args() + ["--no-mtime", "--concurrent-fragments", "4",
                          "--ignore-errors", "--embed-metadata",
                          "--trim-filenames", "120"]


def probe(url, extra=None, flat=True):
    """Ask yt-dlp about a link (no download). Returns dict or None."""
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

# --------------------------------------------------------------------------
# 1. MUSIC
# --------------------------------------------------------------------------
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


def music_spotify(url):
    if find_spec("spotdl") is None:
        say("Spotify needs 'spotdl'. Install it with:\n"
            "  pkg install rust binutils\n  pip install spotdl", "r")
        return False
    m = re.search(r"spotify\.com/(?:intl-[a-z-]+/)?(track|album|playlist)/", url)
    if not m:
        say("Only Spotify track, album and playlist links are supported.", "r")
        return False
    kind = m.group(1)
    url = url.split("?")[0]
    base = cfg["music_dir"]

    if kind == "album":        # folder name = exact album name
        tmpl = f"{base}/{{album}}/{{track-number}} - {{title}}.{{output-ext}}"
        outdir = base
    elif kind == "playlist":
        tmpl = f"{base}/{{list-name}}/{{artists}} - {{title}}.{{output-ext}}"
        outdir = base
    else:
        tmpl = f"{base}/{{artists}} - {{title}}.{{output-ext}}"
        outdir = base

    fmt = cfg["audio_format"]
    cmd = SPOTDL + ["download", url, "--output", tmpl, "--format", fmt]
    if fmt != "flac":
        cmd += ["--bitrate", "auto"]
    ok = run(cmd)
    media_scan(outdir)
    if ok:
        say(f"Done -> {base}", "g")
    return ok


def music_link(url):
    if "spotify.com" in url.lower():
        return music_spotify(url)
    return music_ytdlp(url)


def menu_music():
    say("\n== Music download ==  (YouTube, YouTube Music, Spotify, SoundCloud...)")
    say("Albums/playlists are saved in a folder named after the album.", "y")
    url = ask_url()
    if url:
        music_link(url)

# --------------------------------------------------------------------------
# 2. VIDEO
# --------------------------------------------------------------------------
def tiktok_backup(url, outdir):
    """Fallback: public tikwm API returns a watermark-free (HD) file."""
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
    except Exception as e:  # noqa: BLE001
        say(f"Backup failed: {e}", "r")
        return False


def download_video(url, height=None):
    plat = platform_of(url)
    outdir = os.path.join(cfg["video_dir"], PLATFORM_DIR[plat])
    cmd = YT_DLP + dl_args() + ["--merge-output-format", "mp4"]

    # format selection
    if plat == "tiktok":
        cmd += ["-f", TIKTOK_FMT]
    else:
        if height:
            cmd += ["-f", f"bv*[height<=?{height}]+ba/b[height<=?{height}]"]
        else:
            cmd += ["-f", "bv*+ba/b"]
        cmd += ["-S", "res,vcodec:h264,acodec:m4a"]

    # playlist handling (YouTube & others)
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

# --------------------------------------------------------------------------
# 3. BATCH
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# 4-7. EXTRAS
# --------------------------------------------------------------------------
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

# --------------------------------------------------------------------------
# 8-9. SETTINGS & UPDATE
# --------------------------------------------------------------------------
# --------------------------------------------------------------------------
# SCRIPT SELF-UPDATER (checks your repository for a newer mediadl.py)
# --------------------------------------------------------------------------
CHECK_EVERY = 24 * 3600      # automatic check at most once a day


def normalize_update_url(text):
    """Turn a GitHub repo/file link (or raw link) into raw-file URL(s) to try."""
    t = (text or "").strip().rstrip("/")
    if not t.lower().startswith("https://"):
        return []
    m = re.match(r"https://github\.com/([^/]+)/([^/]+?)(?:\.git)?"
                 r"(?:/(?:blob|raw|tree)/([^/]+)(?:/(.+))?)?$", t)
    if m:
        user, repo, branch, path = m.groups()
        if not path or not path.endswith(".py"):
            path = "mediadl.py"
        branches = [branch] if branch else ["main", "master"]
        return [f"https://raw.githubusercontent.com/{user}/{repo}/{b}/{path}"
                for b in branches]
    return [t]


def _ver_tuple(v):
    return tuple(int(x) for x in re.findall(r"\d+", v or "")) or (0,)


def fetch_remote_script(url_text):
    """Download the script from the repo. Returns (code, error)."""
    urls = normalize_update_url(url_text)
    if not urls:
        return None, "link must start with https://"
    err = "unknown error"
    for u in urls:
        try:
            req = urllib.request.Request(
                u, headers={"User-Agent": UA, "Cache-Control": "no-cache"})
            with urllib.request.urlopen(req, timeout=8) as r:
                data = r.read(2_000_001)
            if len(data) > 2_000_000:
                err = "file is too large"
                continue
            return data.decode("utf-8"), None
        except Exception as e:  # noqa: BLE001
            err = str(e)
    return None, err


def apply_script_update(code, new_ver):
    """Verify the downloaded code, back up the current file, replace it, restart."""
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
    """Compare our version with the repo. auto=True = quiet startup check."""
    url = cfg.get("update_url", "")
    if not url:
        if not auto:
            say("No update repository set. Set it in Settings (option 8 -> 5).", "y")
        return
    if auto and time.time() - cfg.get("last_update_check", 0) < CHECK_EVERY:
        return
    if not auto:
        say("Checking for a new version of mediadl.py...", "y")
    code, err = fetch_remote_script(url)
    if code is None:
        if not auto:
            say(f"Could not reach the repository: {err}", "r")
        return
    cfg["last_update_check"] = time.time()
    save_cfg()
    m = re.search(r"^__version__\s*=\s*[\"']([\d.]+)[\"']", code, re.M)
    if not m or "def main" not in code:
        if not auto:
            say("That file doesn't look like mediadl.py (no __version__ found).", "r")
        return
    remote = m.group(1)
    if _ver_tuple(remote) <= _ver_tuple(__version__):
        if not auto:
            say(f"✔ You have the latest version (v{__version__}).", "g")
        return
    try:                       # never offer a broken update
        compile(code, "mediadl.py", "exec")
    except SyntaxError:
        if not auto:
            say(f"v{remote} exists but its code is broken - skipped.", "r")
        return
    say(f"\n🔔 New version available: v{remote}  (you have v{__version__})", "b")
    ans = ask("Update now? (Press ENTER = yes, n = later):").lower()
    if ans in ("", "y", "yes"):
        apply_script_update(code, remote)
    else:
        say("OK - you can update any time from menu option 10.", "y")


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
        print(f"5) Update repo  : {cfg['update_url'] or '(not set)'}")
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
        elif c == "5":
            say("Use a repository you own or trust - the script will run "
                "whatever code is there.", "y")
            say("Example: https://github.com/yourname/mediadl", "c")
            url = ask("Repo or raw-file link (blank = keep, 'off' = disable):").strip()
            if url.lower() in ("off", "none", "-"):
                cfg["update_url"] = ""
            elif url:
                cfg["update_url"] = url
                cfg["last_update_check"] = 0
                save_cfg()
                check_script_update(auto=False)
                pause()
        elif c == "0":
            break
        save_cfg()


def menu_update():
    say("\n== Updating tools ==")
    say("Installing yt-dlp NIGHTLY (newest YouTube fixes)...", "y")
    subprocess.call([sys.executable, "-m", "pip", "install", "-U", "--pre",
                     "yt-dlp", "yt-dlp-ejs"])
    if find_spec("spotdl"):
        subprocess.call([sys.executable, "-m", "pip", "install", "-U", "spotdl"])
    cfg["last_update"] = time.time()
    save_cfg()
    _flag_cache.clear()
    say("Update finished.", "g")

# --------------------------------------------------------------------------
# Main
# --------------------------------------------------------------------------
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
]


def main():
    if check_requirements():
        pause()
    check_script_update(auto=True)
    while True:
        os.system("clear")
        say("=" * 44, "c")
        say(f"     MediaDL  -  Termux Downloader  v{__version__}", "b")
        say("=" * 44, "c")
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
