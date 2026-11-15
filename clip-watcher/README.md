# Clip Watcher

Let Twitch chat pick your clips. It finds the moments chat explodes and cuts them into **vertical, captioned, credited** clips.

Combines the "Chat Clip Finder" guide (VOD mode) with live recording and auto captions.

## Install (once, Windows or Mac)
1. **Python 3.10+**: python.org, or `winget install Python.Python.3.12`. On Mac use `brew install python`.
2. **ffmpeg**: `winget install Gyan.FFmpeg`. On Mac use `brew install ffmpeg`.
3. In this folder: `py -m pip install -r requirements.txt` (Mac: `python3 -m pip ...`)

## Use

**VOD (recommended).** Paste a past broadcast link (Videos → Past Broadcasts):
```
py clipwatch.py vod https://www.twitch.tv/videos/1234567890 --preview   # see the moments first
py clipwatch.py vod https://www.twitch.tv/videos/1234567890             # cut them
```
It downloads only the chat (12 parts at once) and the 30s around each moment, not the whole stream.

**Live.** Waits for the channel to go live, records it, and clips when the stream ends:
```
py clipwatch.py live CHANNEL
```
Use this when VODs get deleted, muted or are sub-only. The PC must stay on. Ctrl+C stops it and still clips what it recorded.

Output goes to `recordings/.../clips/`:
- `01_...mp4`: the clips, ranked. #1 is the biggest moment, so post it first.
- `clips.txt`: why each clip was picked, plus a ready-to-use credit line.
- `moments.json`: the raw list.

## Settings (add to any command)
| flag | default | what |
|---|---|---|
| `--top` | 5 | how many clips |
| `--before` / `--after` | 20 / 10 | seconds before/after the chat spike. Clips start late? Raise `--before` |
| `--layout` | crop | `crop` = 9:16 fill · `fit` = whole frame on a blurred 9:16 background (keeps the facecam) · `wide` = 16:9 |
| `--hook "wait for it"` | none | big yellow text for the first 3s |
| `--lang` | auto | speech language: `en`, `ar`, `es`... |
| `--model` | small | whisper size: `tiny` (fast) → `large-v3` (best, slow) |
| `--words` | 4 | max words per caption |
| `--no-captions` / `--no-credit` | | turn those off |
| `--gap` | 90 | min seconds between two clips |
| `--min-score` | 2 | ignore moments under 2x normal chat |
| `--chat chat.json` | | use a TwitchDownloader chat file you already have |
| `--tdl PATH` | | download chat with TwitchDownloaderCLI instead of the built-in downloader |
| `--video file.mp4` | | cut from a video you already have |
| `--quality` | best | e.g. `720p60` (falls back to best) |

## How moments are picked
- Chat is counted in 10-second slices. Each person counts once per slice, so one spammer can't fake a spike.
- Bots (Nightbot, StreamElements...) and `!commands` are ignored.
- Score = slice ÷ (median of the 5 min around it + 1). Busy streams only flag what is loud *for them*.

## Rules
Only clip streamers who allow it: check their panels or Discord, or join their clipping campaign. Credit + link is not permission.
