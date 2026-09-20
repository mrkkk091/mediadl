#!/usr/bin/env python3
import datetime
import hmac
import http.server
import importlib.metadata as md
import json
import os
import re
import secrets
import select
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
import urllib.request
from collections import deque
from importlib import invalidate_caches
from importlib.util import find_spec

try:
    import termios
except ImportError:
    termios = None

__version__ = "1.1.0"
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


_ctx = threading.local()
JOB_ANSI = re.compile(r"\x1b\[[0-9;]*m")


class JobCancelled(Exception):
    pass


def current_job():
    return getattr(_ctx, "job", None)


def check_cancel():
    job = current_job()
    if job is not None and job.cancel.is_set():
        raise JobCancelled()


def set_progress(value, force=False):
    job = current_job()
    if job is None or (job.opts.get("batch") and not force):
        return
    job.progress = None if value is None else max(0.0, min(100.0, float(value)))


def say(msg, col="c"):
    print(f"{COL[col]}{msg}{COL['0']}")


def ask(prompt, default=""):
    job = current_job()
    if job is not None:
        if "whole playlist" in prompt:
            return "2" if job.opts.get("playlist") else "1"
        return default
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
    if current_job() is not None:
        return run_captured(cmd)
    print()
    try:
        return subprocess.run(cmd).returncode == 0
    except FileNotFoundError:
        say(f"Command not found: {cmd[0]}", "r")
        return False


NEXT_TAG = "@@NEXT@@"
DONE_TAG = "@@DONE@@"


class SkipTrack(Exception):
    def __init__(self, item=""):
        super().__init__(item)
        self.item = item
        m = re.match(r"\[(\d+)/", item)
        self.index = int(m.group(1)) if m else None


def flush_stdin():
    if current_job() is None and termios and sys.stdin.isatty():
        try:
            termios.tcflush(sys.stdin.fileno(), termios.TCIFLUSH)
        except (OSError, ValueError, termios.error):
            pass


def show_skip_hint():
    if current_job() is not None:
        say("Use the Skip button to skip the current song.", "y")
    elif sys.stdin.isatty():
        say("Press ENTER at any time to skip the current song.", "y")


def remove_partials(folder, prefix, before):
    try:
        names = os.listdir(folder)
    except OSError:
        return
    for n in names:
        if n.startswith(prefix) and n not in before:
            try:
                os.remove(os.path.join(folder, n))
            except OSError:
                pass


def clean_print_args(playlist, total=None):
    if playlist:
        item = f"[%(playlist_index)s/{total or '%(n_entries)s'}] %(title)s"
    else:
        item = "%(title)s"
    return ["--quiet", "--no-warnings", "--no-simulate",
            "--print", f"before_dl:{NEXT_TAG}{item}",
            "--print", f"after_move:{DONE_TAG}{item}"]


def _kill(p):
    try:
        os.killpg(p.pid, signal.SIGTERM)
    except OSError:
        p.terminate()
    try:
        p.wait(timeout=5)
    except subprocess.TimeoutExpired:
        try:
            os.killpg(p.pid, signal.SIGKILL)
        except OSError:
            p.kill()
        p.wait()


_CANCEL = object()
PROGRESS_RE = re.compile(r"\[download\]\s+([\d.]+)%")
ITEM_RE = re.compile(r"\[(\d+)/(\d+)\]")


def _read_lines(p, poll):
    fd = p.stdout.fileno()
    buf = b""
    job = current_job()
    use_tty = poll and job is None
    while True:
        if job is not None:
            if job.cancel.is_set():
                yield _CANCEL
                return
            if poll and job.skip.is_set():
                job.skip.clear()
                yield None
        watch = [fd, sys.stdin] if use_tty else [fd]
        try:
            ready, _, _ = select.select(watch, [], [], 0.3)
        except (OSError, ValueError):
            ready = [fd]
        if use_tty and sys.stdin in ready:
            if sys.stdin.readline():
                yield None
            else:
                use_tty = False
        if fd in ready:
            chunk = os.read(fd, 4096)
            if not chunk:
                if buf:
                    yield buf.decode("utf-8", "replace")
                return
            buf += chunk.replace(b"\r\n", b"\n").replace(b"\r", b"\n")
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                yield line.decode("utf-8", "replace")


def run_captured(cmd):
    job = current_job()
    if cmd[:len(YT_DLP)] == YT_DLP:
        cmd = cmd[:len(YT_DLP)] + ["--newline"] + cmd[len(YT_DLP):]
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, start_new_session=True,
                             env=dict(os.environ, PYTHONUNBUFFERED="1"))
    except FileNotFoundError:
        say(f"Command not found: {cmd[0]}", "r")
        return False
    try:
        for raw in _read_lines(p, True):
            if raw is _CANCEL:
                _kill(p)
                raise JobCancelled()
            if raw is None:
                continue
            line = raw.strip()
            if not line:
                continue
            m = PROGRESS_RE.search(line)
            if m:
                set_progress(m.group(1))
                continue
            job.add_text(line + "\n")
        p.wait()
    except KeyboardInterrupt:
        _kill(p)
        raise
    return p.returncode == 0


def run_clean(cmd, seen, skippable=False):
    env = dict(os.environ, PYTHONUNBUFFERED="1")
    try:
        p = subprocess.Popen(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                             stdin=subprocess.DEVNULL, env=env, start_new_session=True)
    except FileNotFoundError:
        say(f"Command not found: {cmd[0]}", "r")
        return False, False
    poll = skippable and (current_job() is not None or sys.stdin.isatty())
    retryable = False
    current = ""
    try:
        for raw in _read_lines(p, poll):
            if raw is _CANCEL:
                _kill(p)
                raise JobCancelled()
            if raw is None:
                _kill(p)
                raise SkipTrack(current)
            line = raw.strip()
            if line.startswith(NEXT_TAG):
                current = line[len(NEXT_TAG):]
                m = ITEM_RE.match(current)
                if m:
                    set_progress((int(m.group(1)) - 1) / int(m.group(2)) * 100)
                if current not in seen:
                    say(f"  → Next: {current}", "c")
            elif line.startswith(DONE_TAG):
                item = line[len(DONE_TAG):]
                m = ITEM_RE.match(item)
                if m:
                    set_progress(int(m.group(1)) / int(m.group(2)) * 100)
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
        _kill(p)
        raise
    return p.returncode == 0, retryable


CLIENT_SETS = [
    None,
    "tv,web_safari,mweb",
    "web,mweb,android,tv",
    "ios,android_vr",
]


def run_yt(cmd, url, clean=False, skippable=False):
    tries = CLIENT_SETS if "youtu" in url.lower() else [None]
    seen = set()
    for i, clients in enumerate(tries):
        c = list(cmd)
        if clients:
            c += ["--extractor-args", f"youtube:player_client={clients}"]
        if clean:
            ok, retryable = run_clean(c + [url], seen, skippable)
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


_gallery_tip_shown = False
CONTENT_BIN = "/system/bin/content"


def _shell_env():
    env = dict(os.environ)
    env.pop("LD_PRELOAD", None)
    env.pop("LD_LIBRARY_PATH", None)
    return env


def _run_quiet(cmd, timeout=25):
    try:
        r = subprocess.run(cmd, capture_output=True, text=True,
                           timeout=timeout, env=_shell_env())
    except (OSError, subprocess.TimeoutExpired):
        return False, ""
    out = (r.stdout or "") + (r.stderr or "")
    failed = r.returncode != 0 or re.search(r"exception|error|denied", out, re.I)
    return not failed, out


def media_scan(path):
    global _gallery_tip_shown
    strong = False
    if os.path.exists(path):
        tool = shutil.which("termux-media-scan")
        if tool:
            strong, _ = _run_quiet([tool, "-r", path])
        if not strong and os.path.exists(CONTENT_BIN):
            strong, _ = _run_quiet([CONTENT_BIN, "call", "--uri", "content://media/external/file",
                                    "--method", "scan_file", "--arg", path])
            if not strong:
                strong, _ = _run_quiet([CONTENT_BIN, "call", "--uri", "content://media",
                                        "--method", "scan_volume", "--arg", "external_primary"])
        if not strong:
            am = shutil.which("am") or "/system/bin/am"
            _run_quiet([am, "broadcast", "-a", "android.intent.action.MEDIA_SCANNER_SCAN_FILE",
                        "-d", f"file://{path}"])
    if not strong and not _gallery_tip_shown:
        _gallery_tip_shown = True
        say("Tip: if files don't show in Gallery / Music app, use menu option 13.", "y")
    return strong


def is_indexed(path):
    if not os.path.exists(CONTENT_BIN):
        return None
    name = os.path.basename(path).replace("'", "''")
    ok, out = _run_quiet([CONTENT_BIN, "query", "--uri", "content://media/external/file",
                          "--projection", "_display_name",
                          "--where", f"_display_name='{name}'"])
    if not ok:
        return None
    return "Row:" in out


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
        cmd += clean_print_args(playlist=True, total=count)
        is_list = True
    else:
        is_list = False
        count = 0
        outdir = cfg["music_dir"]
        say(f"\nSaving to: {outdir}", "y")
        cmd += ["--no-playlist", "-P", outdir, "-o", "%(title)s.%(ext)s"]
        cmd += clean_print_args(playlist=False)

    before = snapshot(outdir)
    if is_list:
        show_skip_hint()
    flush_stdin()
    start, skipped, ok = 1, 0, False
    while True:
        run_cmd = cmd + (["--playlist-start", str(start)] if start > 1 else [])
        try:
            ok = run_yt(run_cmd, url, clean=True, skippable=is_list)
            break
        except SkipTrack as sk:
            skipped += 1
            say(f"  ↷ Skipped: {sk.item or 'this song'}", "y")
            idx = sk.index or start
            remove_partials(outdir, f"{idx:02d} - ", before)
            start = idx + 1
            if count and start > count:
                ok = True
                break
    flush_stdin()
    if not ok:
        cleanup_orphans(outdir, before)
        say("Some or all tracks failed. Try menu 9 (Update tools), then retry "
            "- finished tracks are skipped automatically.", "r")
    media_scan(outdir)
    if ok:
        note = f"  ({skipped} skipped)" if skipped else ""
        say(f"Done -> {outdir}{note}", "g")
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
    return run_yt(cmd, f"https://www.youtube.com/watch?v={match['id']}",
                  clean=True, skippable=True)


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
    if current_job() is not None:
        say("Could not read this Spotify link. Try the Search tab to find the "
            "song by name.", "y")
        return False
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
    before = snapshot(outdir)
    done = skipped = 0
    if total > 1:
        show_skip_hint()
    flush_stdin()
    for tr in tracks:
        check_cancel()
        set_progress((tr["n"] - 1) / total * 100)
        label = f"{tr['artist']} - {tr['title']}" if tr["artist"] else tr["title"]
        prefix = "" if single else f"[{tr['n']:02d}/{total}] "
        stem = f"{tr['n']:02d} - {clean(tr['title'])}" if kind == "album" else clean(label)
        dest = os.path.join(outdir, f"{stem}.{fmt}")
        if os.path.exists(dest):
            say(f"  ✔ Already saved: {prefix}{label}", "g")
            done += 1
            continue
        say(f"  → Next: {prefix}{label}", "c")
        try:
            got = download_matched(tr, outdir, stem)
        except SkipTrack:
            remove_partials(outdir, f"{stem}.", before)
            say(f"  ↷ Skipped: {prefix}{label}", "y")
            skipped += 1
            continue
        if got and os.path.exists(dest):
            tag_audio(dest, tr["title"], tr["artist"],
                      "" if single else meta["name"],
                      meta["artist"] if kind == "album" else "",
                      0 if single else tr["n"], total, cover)
            say(f"  ✔ Done: {prefix}{label}", "g")
            done += 1
        else:
            say(f"  ✖ Could not download: {prefix}{label}", "r")
    flush_stdin()
    media_scan(outdir)
    note = f", {skipped} skipped" if skipped else ""
    say(f"\nFinished: {done}/{total} tracks{note} -> {outdir}",
        "g" if done + skipped == total else "y")
    return done + skipped == total


def music_link(url):
    if "spotify.com" in url.lower():
        return music_spotify(url)
    return music_ytdlp(url)


def newest_media_file(folders):
    exts = (".mp4", ".mkv", ".webm", ".mov", ".mp3", ".m4a", ".flac", ".opus")
    best, best_time = None, 0
    for folder in folders:
        for root, _, files in os.walk(folder):
            for name in files:
                if not name.lower().endswith(exts):
                    continue
                full = os.path.join(root, name)
                try:
                    t = os.path.getmtime(full)
                except OSError:
                    continue
                if t > best_time:
                    best, best_time = full, t
    return best


def menu_gallery_fix():
    say("\n== Fix: show downloads in Gallery / Music app ==")
    gallery_fix()


def gallery_fix():
    folders = [d for d in (cfg["music_dir"], cfg["video_dir"]) if os.path.isdir(d)]
    if not folders:
        say("Your download folders don't exist yet.", "y")
        return
    for folder in folders:
        say(f"Scanning {folder} ...", "c")
        media_scan(folder)
    latest = newest_media_file(folders)
    if not latest:
        say("No downloaded media files found yet.", "y")
        return
    say(f"\nChecking your newest file: {os.path.basename(latest)}", "c")
    state = is_indexed(latest)
    if state:
        say("✔ Android knows this file now. Close and reopen your Gallery app.", "g")
    elif state is False:
        say("✖ Android has not indexed it yet. No app needed - just do this:", "r")
        print("  1) Restart your phone (Android rescans storage when it boots).")
        print("  2) Open your Gallery again after it has started.")
    else:
        say("Could not verify. Close and reopen your Gallery app. If files are "
            "still missing, restart your phone.", "y")


def menu_search():
    say("\n== Search & download song by name ==")
    q = ask("Song name (e.g. Rick Astley Never Gonna Give You Up):")
    if q:
        search_and_download(q)


def menu_music():
    say("\n== Music download ==  (YouTube, YouTube Music, Spotify, SoundCloud...)")
    say("Albums/playlists are saved in a folder named after the album.", "y")
    say("While downloading a playlist/album, press ENTER to skip a song.", "y")
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


def run_batch(links, mode="1", height=None):
    ok_count = 0
    for i, link in enumerate(links, 1):
        check_cancel()
        set_progress((i - 1) / len(links) * 100, force=True)
        say(f"\n[{i}/{len(links)}] {link}", "b")
        ok = music_link(link) if mode == "1" else download_video(link, height)
        ok_count += bool(ok)
    set_progress(100, force=True)
    say(f"\nFinished: {ok_count}/{len(links)} succeeded.", "g")
    return ok_count == len(links)


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
    run_batch(links, mode)


def download_subs(url, lang="en"):
    outdir = os.path.join(cfg["video_dir"], "Subtitles")
    ok = run(YT_DLP + base_args() + ["--skip-download", "--write-subs",
                                     "--write-auto-subs", "--sub-langs", lang,
                                     "--convert-subs", "srt", "--no-playlist",
                                     "-P", outdir, "-o", "%(title)s.%(ext)s", url])
    say(f"Subtitles folder: {outdir}", "g")
    return ok


def menu_subs():
    say("\n== Download subtitles ==")
    url = ask_url()
    if not url:
        return
    lang = ask("Language code (en, id, es, ...) [en]:", "en")
    download_subs(url, lang)


def download_thumb(url):
    outdir = os.path.join(cfg["video_dir"], "Thumbnails")
    ok = run(YT_DLP + base_args() + ["--skip-download", "--write-thumbnail",
                                     "--convert-thumbnails", "jpg", "--no-playlist",
                                     "-P", outdir, "-o", "%(title)s.%(ext)s", url])
    media_scan(outdir)
    say(f"Thumbnails folder: {outdir}", "g")
    return ok


def menu_thumb():
    say("\n== Download thumbnail ==")
    url = ask_url()
    if not url:
        return
    download_thumb(url)


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


def video_info(url):
    info = probe(url, ["--no-playlist"], flat=False)
    if not info:
        return None
    heights = sorted({f["height"] for f in info.get("formats", [])
                      if f.get("height")})
    return {"title": info.get("title"),
            "uploader": info.get("uploader") or info.get("channel"),
            "duration": fmt_duration(info.get("duration")),
            "views": info.get("view_count"),
            "heights": heights}


def menu_info():
    say("\n== Video info ==")
    url = ask_url()
    if not url:
        return
    info = video_info(url)
    if not info:
        return
    print(f"\nTitle    : {info['title']}")
    print(f"Uploader : {info['uploader']}")
    print(f"Duration : {info['duration']}")
    print(f"Views    : {info['views']}")
    print(f"Qualities: {', '.join(f'{h}p' for h in info['heights']) or 'n/a'}")


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


def update_tools():
    say("Installing yt-dlp NIGHTLY (newest YouTube fixes)...", "y")
    ok = run([sys.executable, "-m", "pip", "install", "-U", "--pre",
              "yt-dlp", "yt-dlp-ejs"])
    cfg["last_update"] = time.time()
    save_cfg()
    _flag_cache.clear()
    say("Update finished.", "g")
    return ok


def menu_update():
    say("\n== Updating tools ==")
    update_tools()


WEB_ROOTS = ["/storage/emulated/0", "/sdcard", os.path.expanduser("~")]
WEB_MAX_RUNNING = 3
WEB_KEEP_JOBS = 40
URL_RE = re.compile(r"^https?://[^\s\x00-\x1f\x7f]{4,2000}$")
VID_RE = re.compile(r"^[A-Za-z0-9_-]{6,20}$")
LANG_RE = re.compile(r"^[A-Za-z]{2,3}(-[A-Za-z0-9]{2,8})?$")
AUDIO_FORMATS = ("mp3", "m4a", "flac", "opus")
QUALITIES = {"best": None, "1080": 1080, "720": 720, "480": 480}
JOBS = {}
JOBS_LOCK = threading.Lock()
_job_seq = 0


class Job:
    def __init__(self, jid, kind, title, opts):
        self.id = jid
        self.kind = kind
        self.title = title
        self.opts = opts
        self.status = "queued"
        self.progress = None
        self.current = ""
        self.lines = []
        self.dropped = 0
        self.partial = ""
        self.skip = threading.Event()
        self.cancel = threading.Event()
        self.lock = threading.Lock()
        self.created = time.time()

    def add_text(self, text):
        with self.lock:
            self.partial += text
            parts = self.partial.split("\n")
            self.partial = parts.pop()
            for line in parts:
                self.lines.append(line)
                plain = JOB_ANSI.sub("", line)
                if "Next:" in plain:
                    self.current = plain.split("Next:", 1)[1].strip()
            overflow = len(self.lines) - 1500
            if overflow > 0:
                del self.lines[:overflow]
                self.dropped += overflow

    def read(self, start):
        with self.lock:
            offset = max(start - self.dropped, 0)
            return self.lines[offset:], self.dropped + len(self.lines)

    def summary(self):
        return {"id": self.id, "kind": self.kind, "title": self.title,
                "status": self.status, "progress": self.progress,
                "current": self.current, "skippable": self.kind in ("music", "batch")}


class _Router:
    def __init__(self, real):
        self.real = real

    def write(self, text):
        job = current_job()
        if job is not None:
            job.add_text(text)
            return len(text)
        return self.real.write(text)

    def flush(self):
        self.real.flush()

    def __getattr__(self, name):
        return getattr(self.real, name)


def valid_url(u):
    return isinstance(u, str) and bool(URL_RE.match(u.strip()))


def short_title(url):
    return url if len(url) <= 70 else url[:67] + "..."


def safe_dir(value):
    path = os.path.realpath(os.path.expanduser(str(value).strip()))
    if not path or "\0" in path or len(path) > 300:
        raise ValueError("Invalid folder.")
    roots = [os.path.realpath(r).rstrip(os.sep) for r in WEB_ROOTS]
    if not any(path == r or path.startswith(r + os.sep) for r in roots):
        raise ValueError("Folder must be inside your phone storage or Termux home.")
    return path


def build_job(body):
    kind = body.get("kind")
    if kind == "gallery":
        return kind, "Fix Gallery", gallery_fix, {}
    if kind == "update":
        return kind, "Update yt-dlp", update_tools, {}
    if kind == "batch":
        raw = body.get("urls")
        if not isinstance(raw, list):
            raise ValueError("No links given.")
        urls = [u.strip() for u in raw[:50] if valid_url(u)]
        if not urls:
            raise ValueError("No valid links found.")
        mode = "2" if body.get("mode") == "video" else "1"
        label = "video" if mode == "2" else "music"
        return ("batch", f"Batch: {len(urls)} links ({label})",
                lambda: run_batch(urls, mode), {"batch": True})
    url = body.get("url")
    if not valid_url(url):
        raise ValueError("Please enter a valid link starting with http:// or https://")
    url = url.strip()
    playlist = bool(body.get("playlist"))
    if kind == "music":
        return kind, short_title(url), lambda: music_link(url), {"playlist": playlist}
    if kind == "video":
        quality = str(body.get("quality", "best"))
        if quality not in QUALITIES:
            raise ValueError("Unknown quality.")
        height = QUALITIES[quality]
        return (kind, short_title(url), lambda: download_video(url, height),
                {"playlist": playlist})
    if kind == "subs":
        lang = str(body.get("lang") or "en").strip()
        if not LANG_RE.match(lang):
            raise ValueError("Invalid language code.")
        return kind, "Subtitles: " + short_title(url), lambda: download_subs(url, lang), {}
    if kind == "thumb":
        return kind, "Thumbnail: " + short_title(url), lambda: download_thumb(url), {}
    raise ValueError("Unknown job type.")


def _run_job(job, fn):
    _ctx.job = job
    job.status = "running"
    try:
        result = fn()
        if job.cancel.is_set():
            job.status = "cancelled"
        else:
            job.status = "failed" if result is False else "done"
    except JobCancelled:
        job.status = "cancelled"
    except Exception as e:
        job.add_text(f"{COL['r']}Error: {e}{COL['0']}\n")
        job.status = "failed"
    finally:
        if job.status == "done":
            job.progress = 100.0
        if job.partial:
            job.add_text("\n")
        _ctx.job = None


def start_job(spec):
    global _job_seq
    kind, title, fn, opts = spec
    with JOBS_LOCK:
        active = sum(1 for j in JOBS.values() if j.status in ("queued", "running"))
        if active >= WEB_MAX_RUNNING:
            raise ValueError(f"Too many running tasks (max {WEB_MAX_RUNNING}). "
                             "Wait for one to finish.")
        _job_seq += 1
        job = Job(str(_job_seq), kind, title, opts)
        JOBS[job.id] = job
        finished = sorted((j for j in JOBS.values()
                           if j.status not in ("queued", "running")),
                          key=lambda j: int(j.id))
        for old in finished[:max(0, len(JOBS) - WEB_KEEP_JOBS)]:
            JOBS.pop(old.id, None)
    threading.Thread(target=_run_job, args=(job, fn), daemon=True).start()
    return job


def job_list():
    with JOBS_LOCK:
        jobs = sorted(JOBS.values(), key=lambda j: int(j.id), reverse=True)
    return [j.summary() for j in jobs]


def web_settings():
    return {"music_dir": cfg["music_dir"], "video_dir": cfg["video_dir"],
            "audio_format": cfg["audio_format"]}


def web_search(query):
    out = []
    for e in search_youtube(query, 8):
        vid = str(e.get("id", ""))
        if not VID_RE.match(vid):
            continue
        out.append({"id": vid,
                    "title": str(e.get("title") or "")[:200],
                    "channel": str(e.get("channel") or e.get("uploader") or "")[:80],
                    "duration": fmt_duration(e.get("duration")),
                    "thumb": f"https://i.ytimg.com/vi/{vid}/mqdefault.jpg"})
    return out


def _int(value, default=0):
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


class WebHandler(http.server.BaseHTTPRequestHandler):
    server_version = "MediaDL"
    sys_version = ""

    def log_message(self, fmt, *args):
        pass

    def _send(self, code, data, ctype="application/json; charset=utf-8", extra=None):
        if isinstance(data, str):
            data = data.encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header("X-Frame-Options", "DENY")
        for key, value in (extra or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(data)

    def _json(self, obj, code=200):
        self._send(code, json.dumps(obj))

    def _guard(self, post=False):
        hosts = {f"127.0.0.1:{self.server.port}", f"localhost:{self.server.port}"}
        if (self.headers.get("Host") or "").lower() not in hosts:
            self._json({"error": "bad host"}, 403)
            return False
        origin = self.headers.get("Origin")
        if post and origin and origin.lower() not in {f"http://{h}" for h in hosts}:
            self._json({"error": "bad origin"}, 403)
            return False
        return True

    def _token_ok(self, value):
        return hmac.compare_digest(str(value).encode("utf-8"),
                                   self.server.token.encode("utf-8"))

    def do_GET(self):
        if not self._guard():
            return
        parts = urllib.parse.urlsplit(self.path)
        qs = urllib.parse.parse_qs(parts.query)
        if parts.path == "/":
            if not self._token_ok((qs.get("t") or [""])[0]):
                self._json({"error": "forbidden"}, 403)
                return
            nonce = secrets.token_urlsafe(12)
            page = WEB_PAGE.replace("__NONCE__", nonce).replace("__VERSION__", __version__)
            csp = (f"default-src 'none'; script-src 'nonce-{nonce}'; "
                   f"style-src 'nonce-{nonce}'; connect-src 'self'; "
                   "img-src https://i.ytimg.com; base-uri 'none'; "
                   "form-action 'none'; frame-ancestors 'none'")
            self._send(200, page, "text/html; charset=utf-8",
                       {"Content-Security-Policy": csp})
            return
        if not self._token_ok(self.headers.get("X-Token", "")):
            self._json({"error": "forbidden"}, 403)
            return
        try:
            self._route_get(parts.path, qs)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except KeyError:
            self._json({"error": "not found"}, 404)
        except Exception:
            self._json({"error": "server error"}, 500)

    def _route_get(self, path, qs):
        if path == "/api/state":
            self._json({"version": __version__, "settings": web_settings(),
                        "jobs": job_list()})
            return
        if path == "/api/jobs":
            self._json({"jobs": job_list()})
            return
        m = re.match(r"^/api/jobs/(\d{1,9})$", path)
        if m:
            job = JOBS[m.group(1)]
            lines, nxt = job.read(_int((qs.get("from") or ["0"])[0]))
            self._json({"lines": lines, "next": nxt, "status": job.status,
                        "progress": job.progress})
            return
        if path == "/api/search":
            query = re.sub(r"[\x00-\x1f\x7f]", " ", (qs.get("q") or [""])[0]).strip()[:120]
            if len(query) < 2:
                raise ValueError("Type at least 2 characters.")
            self._json({"results": web_search(query)})
            return
        if path == "/api/info":
            url = (qs.get("url") or [""])[0].strip()
            if not valid_url(url):
                raise ValueError("Please enter a valid link.")
            info = video_info(url)
            if not info:
                raise ValueError("Could not read that link.")
            self._json(info)
            return
        if path == "/api/versioncheck":
            info, err = read_version_info()
            if info is None:
                raise ValueError(f"Could not check for updates: {err}")
            self._json({"current": __version__, "latest": info["version"],
                        "changelog": info["changelog"],
                        "newer": _ver_tuple(info["version"]) > _ver_tuple(__version__)})
            return
        raise KeyError(path)

    def do_POST(self):
        if not self._guard(post=True):
            return
        if not self._token_ok(self.headers.get("X-Token", "")):
            self._json({"error": "forbidden"}, 403)
            return
        ctype = (self.headers.get("Content-Type") or "").split(";")[0].strip().lower()
        if ctype != "application/json":
            self._json({"error": "json required"}, 415)
            return
        length = _int(self.headers.get("Content-Length"), -1)
        if length < 0 or length > 65536:
            self._json({"error": "bad size"}, 413)
            return
        try:
            body = json.loads(self.rfile.read(length) or b"{}")
        except ValueError:
            self._json({"error": "bad json"}, 400)
            return
        if not isinstance(body, dict):
            self._json({"error": "bad json"}, 400)
            return
        try:
            self._route_post(urllib.parse.urlsplit(self.path).path, body)
        except ValueError as e:
            self._json({"error": str(e)}, 400)
        except KeyError:
            self._json({"error": "not found"}, 404)
        except Exception:
            self._json({"error": "server error"}, 500)

    def _route_post(self, path, body):
        if path == "/api/jobs":
            job = start_job(build_job(body))
            self._json({"job": job.summary()})
            return
        m = re.match(r"^/api/jobs/(\d{1,9})/(skip|cancel)$", path)
        if m:
            job = JOBS[m.group(1)]
            (job.skip if m.group(2) == "skip" else job.cancel).set()
            self._json({"ok": True})
            return
        if path == "/api/settings":
            music_dir = safe_dir(body.get("music_dir", cfg["music_dir"]))
            video_dir = safe_dir(body.get("video_dir", cfg["video_dir"]))
            fmt = str(body.get("audio_format", cfg["audio_format"])).lower()
            if fmt not in AUDIO_FORMATS:
                raise ValueError("Unsupported audio format.")
            cfg.update(music_dir=music_dir, video_dir=video_dir, audio_format=fmt)
            save_cfg()
            self._json({"settings": web_settings()})
            return
        raise KeyError(path)


def run_web(port=8765):
    server = None
    for candidate in range(port, port + 20):
        try:
            server = http.server.ThreadingHTTPServer(("127.0.0.1", candidate), WebHandler)
            break
        except OSError:
            continue
    if server is None:
        say("Could not find a free port.", "r")
        return
    server.daemon_threads = True
    server.port = server.server_address[1]
    server.token = secrets.token_urlsafe(18)
    url = f"http://127.0.0.1:{server.port}/?t={server.token}"

    say("\n== MediaDL Web ==", "b")
    say("Open this link in your phone's browser (it contains a secret key):", "y")
    print(f"\n  {url}\n")
    say("Only this phone can open it. Keep Termux open while you use it.", "y")
    say("Press Ctrl+C here to stop the web server.\n", "y")

    wake = shutil.which("termux-wake-lock")
    if wake:
        subprocess.run([wake], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
    opener = shutil.which("termux-open-url")
    if opener:
        try:
            subprocess.run([opener, url], stdout=subprocess.DEVNULL,
                           stderr=subprocess.DEVNULL, timeout=10)
        except (OSError, subprocess.TimeoutExpired):
            pass

    real_stdout = sys.stdout
    sys.stdout = _Router(real_stdout)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        sys.stdout = real_stdout
        for job in list(JOBS.values()):
            job.cancel.set()
        time.sleep(0.5)
        server.server_close()
        unlock = shutil.which("termux-wake-unlock")
        if unlock:
            subprocess.run([unlock], stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        say("\nWeb server stopped.", "g")


def menu_web():
    say("\n== Web interface ==")
    run_web()


WEB_PAGE = r"""<!doctype html>
<html lang="en">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<meta name="color-scheme" content="dark">
<title>MediaDL by mrk</title>
<style nonce="__NONCE__">
:root{--bg:#0e1116;--card:#171b22;--card2:#1f242d;--line:#2a303b;--text:#e9ecf2;--muted:#8f97a6;--acc:#5b8cff;--acc2:#3d6ae0;--ok:#3ecf8e;--err:#ff6b6b;--warn:#ffd166;--cy:#5ad1e6}
*{box-sizing:border-box;-webkit-tap-highlight-color:transparent}
html,body{margin:0}
body{background:var(--bg);color:var(--text);font:15px/1.45 system-ui,-apple-system,"Segoe UI",Roboto,sans-serif;padding:max(10px,env(safe-area-inset-top)) 12px max(90px,env(safe-area-inset-bottom))}
.wrap{max-width:680px;margin:0 auto}
header{display:flex;align-items:center;justify-content:space-between;gap:8px;padding:6px 2px 12px}
h1{font-size:19px;margin:0;letter-spacing:.2px}
h1 span{color:var(--acc)}
.chip{font-size:12px;color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:2px 9px}
nav{display:flex;gap:6px;overflow-x:auto;padding-bottom:8px;scrollbar-width:none}
nav::-webkit-scrollbar{display:none}
nav button{flex:0 0 auto;background:var(--card);color:var(--muted);border:1px solid var(--line);border-radius:999px;padding:8px 14px;font:inherit;cursor:pointer}
nav button.on{background:var(--acc);border-color:var(--acc);color:#fff}
.card{background:var(--card);border:1px solid var(--line);border-radius:14px;padding:14px;margin:10px 0}
.card h2{font-size:15px;margin:0 0 6px}
label{display:block;font-size:13px;color:var(--muted);margin:10px 0 4px}
input[type=text],input[type=url],textarea,select{width:100%;background:var(--card2);color:var(--text);border:1px solid var(--line);border-radius:10px;padding:11px 12px;font:inherit;outline:none}
input:focus,textarea:focus,select:focus{border-color:var(--acc)}
textarea{min-height:130px;resize:vertical}
.row{display:flex;gap:8px;align-items:center}
.check{display:flex;gap:8px;align-items:center;margin:10px 0 0;color:var(--muted);font-size:14px}
.check input{width:18px;height:18px}
.check label{margin:0}
.btn{background:var(--acc);color:#fff;border:0;border-radius:10px;padding:11px 16px;font:inherit;font-weight:600;cursor:pointer;margin-top:12px}
.btn:active{background:var(--acc2)}
.btn.ghost{background:var(--card2);color:var(--text);border:1px solid var(--line)}
.btn.small{padding:6px 11px;font-size:13px;margin-top:0}
.btn:disabled{opacity:.5}
.muted{color:var(--muted);font-size:13px;margin:6px 0 0}
.panel{display:none}
.panel.on{display:block}
.res{display:flex;gap:10px;align-items:center;padding:9px 0;border-top:1px solid var(--line)}
.res:first-child{border-top:0}
.res img{width:88px;height:50px;object-fit:cover;border-radius:8px;background:var(--card2);flex:0 0 auto}
.res .t{flex:1;min-width:0}
.res .t b{font-weight:600;overflow:hidden;display:-webkit-box;-webkit-line-clamp:2;-webkit-box-orient:vertical}
.res .t span{font-size:12px;color:var(--muted)}
.job{border-top:1px solid var(--line);padding:11px 0}
.job:first-child{border-top:0}
.jt{display:flex;justify-content:space-between;gap:8px;align-items:center}
.jt b{overflow:hidden;text-overflow:ellipsis;white-space:nowrap;min-width:0}
.st{font-size:12px;border-radius:999px;padding:2px 9px;background:var(--card2);color:var(--muted);flex:0 0 auto}
.st.running{color:var(--cy)}
.st.done{color:var(--ok)}
.st.failed{color:var(--err)}
.st.cancelled{color:var(--warn)}
.bar{height:6px;background:var(--card2);border-radius:999px;overflow:hidden;margin:8px 0}
.bar i{display:block;height:100%;width:0;background:var(--acc);transition:width .4s}
.bar.ind i{width:40%;transition:none;animation:mv 1.1s linear infinite}
@keyframes mv{from{margin-left:-40%}to{margin-left:100%}}
.cur{font-size:13px;color:var(--muted);white-space:nowrap;overflow:hidden;text-overflow:ellipsis}
.acts{display:flex;gap:6px;margin-top:8px;flex-wrap:wrap}
.acts.top{margin-top:12px}
.log{display:none;background:#0a0c10;border:1px solid var(--line);border-radius:10px;padding:8px;margin-top:8px;max-height:260px;overflow:auto;font:12px/1.4 ui-monospace,Menlo,Consolas,monospace;white-space:pre-wrap;word-break:break-word}
.log.on{display:block}
.log .g{color:var(--ok)}
.log .r{color:var(--err)}
.log .y{color:var(--warn)}
.log .c{color:var(--cy)}
.log .b{font-weight:700}
#toast{position:fixed;left:12px;right:12px;bottom:max(14px,env(safe-area-inset-bottom));max-width:660px;margin:0 auto;background:#222836;border:1px solid var(--line);border-radius:12px;padding:11px 14px;display:none;z-index:9}
#toast.on{display:block}
#toast.err{border-color:var(--err)}
.kv{display:grid;grid-template-columns:auto 1fr;gap:4px 12px;font-size:14px;margin-top:12px}
.kv span:nth-child(odd){color:var(--muted)}
</style>
</head>
<body>
<div class="wrap">
<header><h1>MediaDL <span>by mrk</span></h1><span class="chip">v__VERSION__</span></header>
<nav id="tabs">
<button data-tab="music" class="on">Music</button>
<button data-tab="video">Video</button>
<button data-tab="search">Search</button>
<button data-tab="batch">Batch</button>
<button data-tab="tools">Tools</button>
<button data-tab="settings">Settings</button>
</nav>

<section class="panel on" id="tab-music">
<div class="card">
<h2>Music download</h2>
<p class="muted">YouTube, YouTube Music, SoundCloud and Spotify (track, album, playlist). Albums are saved in a folder named after the album.</p>
<label for="musicUrl">Link</label>
<input type="url" id="musicUrl" placeholder="https://..." autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="check"><input type="checkbox" id="musicPl"><label for="musicPl">Song inside a playlist: download the whole playlist</label></div>
<button class="btn" id="goMusic">Download music</button>
</div>
</section>

<section class="panel" id="tab-video">
<div class="card">
<h2>Video download</h2>
<p class="muted">YouTube, TikTok (no watermark), Instagram and more.</p>
<label for="videoUrl">Link</label>
<input type="url" id="videoUrl" placeholder="https://..." autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="videoQ">Quality</label>
<select id="videoQ"><option value="best">Best available</option><option value="1080">1080p</option><option value="720">720p</option><option value="480">480p</option></select>
<div class="check"><input type="checkbox" id="videoPl"><label for="videoPl">Video inside a playlist: download the whole playlist</label></div>
<button class="btn" id="goVideo">Download video</button>
</div>
</section>

<section class="panel" id="tab-search">
<div class="card">
<h2>Search &amp; download by name</h2>
<label for="searchQ">Song name</label>
<div class="row"><input type="text" id="searchQ" placeholder="Artist - Song" autocomplete="off"><button class="btn small" id="goSearch">Search</button></div>
<div id="searchOut"></div>
</div>
</section>

<section class="panel" id="tab-batch">
<div class="card">
<h2>Batch download</h2>
<p class="muted">One link per line (max 50). Lines starting with # are ignored.</p>
<label for="batchLinks">Links</label>
<textarea id="batchLinks" placeholder="https://...&#10;https://..." spellcheck="false" autocapitalize="off"></textarea>
<label for="batchMode">Download as</label>
<select id="batchMode"><option value="music">Music</option><option value="video">Video (best quality)</option></select>
<button class="btn" id="goBatch">Start batch</button>
</div>
</section>

<section class="panel" id="tab-tools">
<div class="card">
<h2>Link tools</h2>
<label for="toolUrl">Link</label>
<input type="url" id="toolUrl" placeholder="https://..." autocomplete="off" autocapitalize="off" spellcheck="false">
<div class="acts top">
<button class="btn ghost small" id="goInfo">Show info</button>
<button class="btn ghost small" id="goThumb">Thumbnail</button>
</div>
<label for="subLang">Subtitles language</label>
<div class="row"><input type="text" id="subLang" value="en" maxlength="12" autocomplete="off"><button class="btn small" id="goSubs">Get subtitles</button></div>
<div id="infoOut"></div>
</div>
<div class="card">
<h2>Maintenance</h2>
<div class="acts top">
<button class="btn ghost small" id="goGallery">Fix Gallery</button>
<button class="btn ghost small" id="goUpdate">Update yt-dlp</button>
<button class="btn ghost small" id="goVersion">Check MediaDL update</button>
</div>
<p class="muted">To update MediaDL itself, close the web page, stop the server with Ctrl+C in Termux and use menu option 10.</p>
</div>
</section>

<section class="panel" id="tab-settings">
<div class="card">
<h2>Settings</h2>
<label for="setMusic">Music folder</label>
<input type="text" id="setMusic" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="setVideo">Video folder</label>
<input type="text" id="setVideo" autocomplete="off" autocapitalize="off" spellcheck="false">
<label for="setFmt">Audio format</label>
<select id="setFmt"><option>mp3</option><option>m4a</option><option>flac</option><option>opus</option></select>
<p class="muted">Folders must be inside your phone storage or the Termux home folder.</p>
<button class="btn" id="goSave">Save settings</button>
</div>
</section>

<div class="card" id="jobsCard">
<h2>Downloads</h2>
<div id="jobs"></div>
<p class="muted" id="noJobs">Nothing yet. Start a download above.</p>
</div>
</div>
<div id="toast"></div>
<script nonce="__NONCE__">
(function(){
'use strict';
var TOKEN=new URLSearchParams(location.search).get('t')||'';
var cards={},hidden={},toastTimer=null;
function $(s,r){return (r||document).querySelector(s);}
function $$(s,r){return Array.prototype.slice.call((r||document).querySelectorAll(s));}
function el(tag,cls,text){var e=document.createElement(tag);if(cls)e.className=cls;if(text!==undefined)e.textContent=text;return e;}
function toast(msg,bad){var t=$('#toast');t.textContent=msg;t.className=bad?'on err':'on';clearTimeout(toastTimer);toastTimer=setTimeout(function(){t.className='';},4000);}
function api(path,opt){
  opt=opt||{};
  var headers={'X-Token':TOKEN};
  var init={method:opt.method||'GET',headers:headers,cache:'no-store'};
  if(opt.body!==undefined){headers['Content-Type']='application/json';init.body=JSON.stringify(opt.body);}
  return fetch(path,init).then(function(r){
    return r.json().catch(function(){return {};}).then(function(d){
      if(!r.ok)throw new Error(d.error||('Error '+r.status));
      return d;
    });
  });
}
function busy(btn,promise){
  btn.disabled=true;
  return promise.catch(function(e){toast(e.message,true);}).then(function(){btn.disabled=false;});
}
$$('#tabs button').forEach(function(b){
  b.addEventListener('click',function(){
    $$('#tabs button').forEach(function(x){x.classList.toggle('on',x===b);});
    $$('.panel').forEach(function(p){p.classList.toggle('on',p.id==='tab-'+b.dataset.tab);});
  });
});
function startJob(body,btn){
  return busy(btn,api('/api/jobs',{method:'POST',body:body}).then(function(d){
    toast('Started: '+d.job.title);
    refresh();
    $('#jobsCard').scrollIntoView({behavior:'smooth',block:'start'});
  }));
}
function onEnter(id,fn){$(id).addEventListener('keydown',function(e){if(e.key==='Enter')fn();});}
function val(id){return $(id).value.trim();}
function goMusic(){startJob({kind:'music',url:val('#musicUrl'),playlist:$('#musicPl').checked},$('#goMusic'));}
function goVideo(){startJob({kind:'video',url:val('#videoUrl'),quality:$('#videoQ').value,playlist:$('#videoPl').checked},$('#goVideo'));}
$('#goMusic').addEventListener('click',goMusic);onEnter('#musicUrl',goMusic);
$('#goVideo').addEventListener('click',goVideo);onEnter('#videoUrl',goVideo);
$('#goBatch').addEventListener('click',function(){
  var urls=$('#batchLinks').value.split('\n').map(function(s){return s.trim();}).filter(function(s){return s&&s.charAt(0)!=='#';});
  startJob({kind:'batch',urls:urls,mode:$('#batchMode').value,quality:'best'},this);
});
$('#goSubs').addEventListener('click',function(){startJob({kind:'subs',url:val('#toolUrl'),lang:val('#subLang')||'en'},this);});
$('#goThumb').addEventListener('click',function(){startJob({kind:'thumb',url:val('#toolUrl')},this);});
$('#goGallery').addEventListener('click',function(){startJob({kind:'gallery'},this);});
$('#goUpdate').addEventListener('click',function(){startJob({kind:'update'},this);});
$('#goInfo').addEventListener('click',function(){
  var out=$('#infoOut');out.textContent='';
  busy(this,api('/api/info?url='+encodeURIComponent(val('#toolUrl'))).then(function(d){
    var kv=el('div','kv');
    [['Title',d.title],['Uploader',d.uploader],['Duration',d.duration],['Views',d.views],['Qualities',(d.heights||[]).map(function(h){return h+'p';}).join(', ')||'n/a']].forEach(function(p){
      kv.appendChild(el('span','',p[0]));
      kv.appendChild(el('span','',p[1]==null?'':String(p[1])));
    });
    out.appendChild(kv);
  }));
});
$('#goVersion').addEventListener('click',function(){
  busy(this,api('/api/versioncheck').then(function(d){
    toast(d.newer?('New version v'+d.latest+' available (you have v'+d.current+'). '+d.changelog):('You have the latest version (v'+d.current+').'));
  }));
});
function doSearch(){
  var q=val('#searchQ'),box=$('#searchOut');
  if(q.length<2){toast('Type a song name first',true);return;}
  box.textContent='';box.appendChild(el('p','muted','Searching...'));
  busy($('#goSearch'),api('/api/search?q='+encodeURIComponent(q)).then(function(d){
    box.textContent='';
    if(!d.results.length){box.appendChild(el('p','muted','No results found.'));return;}
    d.results.forEach(function(r){
      var row=el('div','res');
      var img=el('img');
      if(r.thumb&&r.thumb.indexOf('https://i.ytimg.com/')===0){img.src=r.thumb;}
      img.alt='';img.loading='lazy';img.referrerPolicy='no-referrer';
      var t=el('div','t');
      t.appendChild(el('b','',r.title));
      t.appendChild(el('span','',r.channel+'  '+r.duration));
      var b=el('button','btn small','Download');
      b.addEventListener('click',function(){startJob({kind:'music',url:'https://www.youtube.com/watch?v='+r.id},b);});
      row.appendChild(img);row.appendChild(t);row.appendChild(b);
      box.appendChild(row);
    });
  }).catch(function(e){box.textContent='';throw e;}));
}
$('#goSearch').addEventListener('click',doSearch);onEnter('#searchQ',doSearch);
$('#goSave').addEventListener('click',function(){
  busy(this,api('/api/settings',{method:'POST',body:{music_dir:val('#setMusic'),video_dir:val('#setVideo'),audio_format:$('#setFmt').value}}).then(function(d){
    fillSettings(d.settings);toast('Settings saved');
  }));
});
function fillSettings(s){$('#setMusic').value=s.music_dir;$('#setVideo').value=s.video_dir;$('#setFmt').value=s.audio_format;}
function ansiInto(node,line){
  var re=/\x1b\[(\d+)m/g,last=0,cls='',m;
  function add(t){if(!t)return;if(cls){node.appendChild(el('span',cls,t));}else{node.appendChild(document.createTextNode(t));}}
  while((m=re.exec(line))){
    add(line.slice(last,m.index));
    var c=m[1];
    if(c==='0')cls='';else if(c==='1')cls='b';else if(c==='91')cls='r';else if(c==='92')cls='g';else if(c==='93')cls='y';else if(c==='96')cls='c';
    last=re.lastIndex;
  }
  add(line.slice(last));
}
function appendLog(c,lines){
  var log=c.log,near=log.scrollHeight-log.scrollTop-log.clientHeight<40;
  lines.forEach(function(line){
    var d=el('div');
    if(line){ansiInto(d,line);}else{d.appendChild(document.createTextNode('\u00a0'));}
    log.appendChild(d);
  });
  while(log.childNodes.length>700)log.removeChild(log.firstChild);
  if(near)log.scrollTop=log.scrollHeight;
}
function makeCard(j){
  var c={id:j.id,root:el('div','job'),open:false,next:0,status:'',endFetched:false};
  var top=el('div','jt');
  c.title=el('b','',j.title);c.st=el('span','st','');
  top.appendChild(c.title);top.appendChild(c.st);
  c.bar=el('div','bar');c.fill=el('i');c.bar.appendChild(c.fill);
  c.cur=el('div','cur');
  var acts=el('div','acts');
  c.bLog=el('button','btn ghost small','Log');
  c.bSkip=el('button','btn ghost small','Skip song');
  c.bCancel=el('button','btn ghost small','Cancel');
  c.bHide=el('button','btn ghost small','Hide');
  [c.bLog,c.bSkip,c.bCancel,c.bHide].forEach(function(b){acts.appendChild(b);});
  c.log=el('div','log');
  c.root.appendChild(top);c.root.appendChild(c.bar);c.root.appendChild(c.cur);c.root.appendChild(acts);c.root.appendChild(c.log);
  c.bLog.addEventListener('click',function(){c.open=!c.open;c.log.classList.toggle('on',c.open);if(c.open)pollLog(c);});
  c.bSkip.addEventListener('click',function(){api('/api/jobs/'+c.id+'/skip',{method:'POST',body:{}}).then(function(){toast('Skipping current song...');}).catch(function(e){toast(e.message,true);});});
  c.bCancel.addEventListener('click',function(){api('/api/jobs/'+c.id+'/cancel',{method:'POST',body:{}}).then(function(){toast('Cancelling...');}).catch(function(e){toast(e.message,true);});});
  c.bHide.addEventListener('click',function(){hidden[c.id]=true;c.root.remove();delete cards[c.id];checkEmpty();});
  return c;
}
function updateCard(c,j){
  var active=j.status==='running'||j.status==='queued';
  if(c.status&&c.status!==j.status&&!active){toast(j.title+': '+j.status,j.status==='failed');}
  c.status=j.status;
  c.st.textContent=j.status;c.st.className='st '+j.status;
  if(j.progress==null&&active){c.bar.className='bar ind';}else{c.bar.className='bar';c.fill.style.width=(j.progress==null?(j.status==='done'?100:0):j.progress)+'%';}
  c.cur.textContent=active&&j.current?('Now: '+j.current):'';
  c.bSkip.style.display=(j.status==='running'&&j.skippable)?'':'none';
  c.bCancel.style.display=active?'':'none';
  c.bHide.style.display=active?'none':'';
  if(active)c.endFetched=false;
}
function pollLog(c){
  if(!c.open||c.busy)return;
  var finished=c.status!=='running'&&c.status!=='queued';
  if(finished&&c.endFetched)return;
  c.busy=true;
  api('/api/jobs/'+c.id+'?from='+c.next).then(function(d){
    if(d.lines.length)appendLog(c,d.lines);
    c.next=d.next;
    if(d.status!=='running'&&d.status!=='queued')c.endFetched=true;
  }).catch(function(){}).then(function(){c.busy=false;});
}
function checkEmpty(){$('#noJobs').style.display=Object.keys(cards).length?'none':'';}
function refresh(){
  return api('/api/jobs').then(function(d){
    var box=$('#jobs');
    d.jobs.slice().reverse().forEach(function(j){
      if(hidden[j.id])return;
      var c=cards[j.id];
      if(!c){c=cards[j.id]=makeCard(j);box.insertBefore(c.root,box.firstChild);}
      updateCard(c,j);
      pollLog(c);
    });
    checkEmpty();
  }).catch(function(){});
}
if(!TOKEN){toast('Missing key. Open the full link printed in Termux.',true);}
api('/api/state').then(function(d){fillSettings(d.settings);refresh();}).catch(function(e){toast(e.message,true);});
setInterval(function(){if(!document.hidden)refresh();},1500);
})();
</script>
</body>
</html>
"""


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
    ("13", "Fix Gallery (show downloads in Gallery)", menu_gallery_fix),
    ("14", "Web interface (use in your browser)", menu_web),
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
        if len(sys.argv) > 1 and sys.argv[1] == "web":
            web_port = int(sys.argv[2]) if len(sys.argv) > 2 and sys.argv[2].isdigit() else 8765
            check_requirements()
            run_web(web_port)
        else:
            main()
    except KeyboardInterrupt:
        print()
