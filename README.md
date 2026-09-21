# MediaDL by mrk

An all-in-one music and video downloader for **Termux** (Android). One Python file, a simple menu, and an optional glass-style web interface you can open in your phone's browser.

> One file to install: `mediadl.py`. Everything else (the web page included) lives inside it.

---

## Features

- **Music** from YouTube, YouTube Music, SoundCloud and Spotify (tracks, albums, playlists)
  - Albums and playlists are saved in a folder named after the album
  - Title, artist, album, track number and cover art are added to the files
  - Formats: `mp3`, `m4a`, `opus`, `flac`
- **Video** from YouTube, TikTok (no watermark), Instagram and other sites
  - Best quality, 1080p, 720p or 480p
  - Playlist support
- **Search by name** - type a song name, pick a result, download it
- **Batch downloads** - a list of links in a `.txt` file (or pasted in the web page)
- **Skip a song** while an album or playlist is downloading (press `ENTER`)
- **Subtitles**, **thumbnails**, **video info**, and **local video to MP3** conversion
- **Requirement checker** - detects missing or outdated tools on start and offers to install them
- **Self-update** from this repository
- **Gallery fix** - helps new downloads appear in your Gallery and Music apps
- **Web interface** - every feature as a glass card that expands into its own window

---

## Install

1. Install **Termux** from [F-Droid](https://f-droid.org/packages/com.termux/) or the [Termux GitHub releases](https://github.com/termux/termux-app/releases). The Play Store version is outdated.
2. Open Termux and run:

```bash
pkg update && pkg upgrade -y
pkg install python curl -y
termux-setup-storage
```

3. Download the script into your Termux home folder:

```bash
curl -L -o ~/mediadl.py https://raw.githubusercontent.com/mrkkk091/mediadl/main/mediadl.py
```

4. Run it:

```bash
python ~/mediadl.py
```

On the first run it checks for everything it needs (ffmpeg, Node.js, yt-dlp and more). Press **ENTER** at `Install all requirements?` to install them.

> Tip: download the file with `curl` as shown. Copying it into Termux with a file manager can leave it with wrong permissions.

---

## Menu

| # | Option | What it does |
|---|--------|--------------|
| 1 | Music download | YouTube, YouTube Music, SoundCloud, Spotify |
| 2 | Video download | YouTube, TikTok, Instagram and more |
| 3 | Batch download | Read links from a `.txt` file (one per line) |
| 4 | Download subtitles | Saves subtitles as `.srt` |
| 5 | Download thumbnail | Saves the video's cover image |
| 6 | Convert local video(s) to MP3 | Turns video files on your phone into MP3 |
| 7 | Video info | Title, uploader, length, available qualities |
| 8 | Settings | Music folder, video folder, audio format, cookies file |
| 9 | Update tools | Installs the newest yt-dlp |
| 10 | Check for script update | Updates `mediadl.py` from this repository |
| 11 | Check / install requirements | Re-runs the requirement checker |
| 12 | Search & download song by name | Search YouTube and pick a result |
| 13 | Fix Gallery | Asks Android to rescan your download folders |
| 14 | Web interface | Starts the browser interface |

---

## Where files are saved

| Type | Location |
|------|----------|
| Music (single songs) | `/storage/emulated/0/Music/` |
| Music (albums / playlists) | `/storage/emulated/0/Music/<album name>/` |
| Videos | `/storage/emulated/0/Movies/<YouTube, TikTok, Instagram or Other>/` |
| Subtitles | `/storage/emulated/0/Movies/Subtitles/` |
| Thumbnails | `/storage/emulated/0/Movies/Thumbnails/` |

Change the folders in **Settings (8)**. Settings are stored in `~/.mediadl.json`.

---

## Skipping a song

While an album or playlist is downloading, press **ENTER** to skip the current song. Its partial files are removed and the next song starts. The final line shows how many were skipped. In the web interface, use the **Skip song** button.

---

## Web interface

Start it from menu option **14**, or directly:

```bash
python ~/mediadl.py web
```

It prints a link like `http://127.0.0.1:8765/?t=...` and tries to open your browser. Keep Termux open while you use it, and press `Ctrl+C` to stop.

- **Music, Video, Search, Batch** - the same downloads as the menu
- **Downloads** - progress bars, a live log, **Skip song** and **Cancel**
- **Tools** - video info, subtitles, thumbnails, Fix Gallery, update yt-dlp, update check
- **Settings** - folders and audio format

Each feature is a card on the home screen. Tap a card and it expands into its own window; close it with the X, the back button or `Esc`.

**Safety**

- Listens on `127.0.0.1` only, so only your phone can open it
- A new random secret key is created every time you start it and is required for every request
- Requests must come from the local page (host and origin are checked)
- Only `http://` and `https://` links are accepted
- Folder settings are limited to your phone storage or the Termux home folder
- Text from downloaded titles is always shown as plain text

---

## Updating

- **The script:** menu option **10** (it also checks by itself about once a day). Your old version is kept as `mediadl.py.bak`.
- **yt-dlp:** menu option **9**. Do this first if YouTube downloads suddenly fail.

### For maintainers: publishing a new version

1. Raise `__version__` near the top of `mediadl.py`.
2. Set the same number in `version.json`, and add a short changelog:

```json
{
  "version": "1.3.0",
  "changelog": "What changed"
}
```

3. Commit both files. Users see the update on their next check.

If the two version numbers do not match, users get "not ready on GitHub yet" and nothing changes.

---

## Troubleshooting

| Problem | Fix |
|---------|-----|
| `HTTP Error 403` on YouTube | Menu **9** (installs the newest yt-dlp). The script also retries with other YouTube methods automatically. |
| ffmpeg is broken or missing | Run `pkg upgrade -y`, then `pkg install ffmpeg -y` (or use menu **11**) |
| `Permission denied` on the script | `rm -f ~/mediadl.py`, then download it again with `curl` (see Install) |
| Downloads do not show in Gallery | Menu **13**, then close and reopen the Gallery. If they still do not show, restart your phone. |
| Instagram asks for login | Export `cookies.txt` from your browser and set its path in Settings (option 4) |
| Storage permission missing | Run `termux-setup-storage` and tap Allow |

---

## Notes and limits

- **Spotify:** Spotify audio cannot be downloaded directly. MediaDL reads the track list from Spotify's public page, finds each song on YouTube (matching by length), and tags the file with Spotify's title, artist, album and cover. The audio therefore comes from YouTube and may not be the exact Spotify version. Spotify only shares about the first 50 tracks of a playlist.
- **TikTok:** the watermark-free version is used when available. A backup method is tried if the first one fails.
- **Websites change often.** If something stops working, update yt-dlp (menu 9) and the script (menu 10).

---

## Legal notice and disclaimer

**Please read this before using MediaDL.**

- **Purpose.** MediaDL is a free, open-source tool that puts a simple menu and web interface on top of existing programs (yt-dlp, FFmpeg and mutagen). It is meant for personal use, such as saving content you own, content in the public domain, content under a licence that allows downloading, or content you have permission to download.
- **No hosting or distribution.** The developer (mrk) does not host, store, upload, share or distribute any music, video or other media. MediaDL contains no copyrighted content. Everything is downloaded straight to the user's own device from the sources the user chooses.
- **No illegal involvement by the developer.** The developer does not encourage, condone or take part in piracy or any other illegal activity, and has no involvement in how anyone chooses to use this software. Any misuse is the sole responsibility of the user.
- **User responsibility.** You alone are responsible for what you download and how you use it. You must follow the copyright laws of your country and the terms of service of every website you use, including YouTube, Spotify, TikTok, Instagram and SoundCloud. Downloading copyrighted material without permission may be illegal.
- **No affiliation.** MediaDL is an independent project. It is not affiliated with, endorsed by or sponsored by YouTube, Google, Spotify, TikTok, Instagram, Meta, SoundCloud or any other service. All names and trademarks belong to their owners.
- **Spotify.** MediaDL does not download audio from Spotify and does not bypass Spotify's protection. It only reads public track information and looks for matching songs on other sources.
- **No warranty and no liability.** The software is provided "as is", without warranty of any kind, and you use it at your own risk. The developer is not liable for any damage, data loss, account restriction or legal issue that results from using it.
- **Rights holders.** If you are a rights holder and have a concern about this project, please open an issue in this repository.

By using MediaDL you agree to these terms. If you do not agree, do not use it.

This notice is for information only and is not legal advice.

---

## Credits

- Made by **mrk**
- Powered by [yt-dlp](https://github.com/yt-dlp/yt-dlp), [FFmpeg](https://ffmpeg.org/) and [mutagen](https://github.com/quodlibet/mutagen)
