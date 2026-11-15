# Clip Watcher

Records a Twitch stream + its chat, finds the moments chat explodes, cuts 45s clips around them, and burns in captions. Runs unattended.

## Install (once)
1. Python 3.10+ and ffmpeg
   - Windows: `winget install Python.Python.3.12 Gyan.FFmpeg`
   - Mac: `brew install python ffmpeg`
   - Linux: `sudo apt install python3-pip ffmpeg`
2. `pip install -r requirements.txt`

## Use
```
python clipwatch.py record CHANNEL
```
Waits until CHANNEL goes live, records video + chat, and when the stream ends writes clips to
`recordings/CHANNEL/<date>/clips/` plus `clips.txt` (why each was picked + credit line).
Keeps waiting for the next stream; add `--once` to stop after one.

Re-clip a finished recording with different settings:
```
python clipwatch.py clips recordings/CHANNEL/<date> --ratio 2 --vertical
```

## Settings
| flag | default | meaning |
|---|---|---|
| `--ratio` | 3 | spike = chat this many times busier than the last 5 min |
| `--min-msgs` | 10 | ignore spikes with fewer msgs per 10s (lower for small channels) |
| `--max-clips` | 10 | best N spikes per stream |
| `--lead` / `--length` | 30 / 45 | start 30s before chat reacts, 45s long |
| `--model` | small | whisper size: tiny, base, small, medium, large-v3 (bigger = slower, better) |
| `--lang` | auto | speech language, e.g. `en`, `ar`, `es` |
| `--vertical` | off | center-crop to 9:16 for Shorts/TikTok/Reels |
| `--quality` | best | `720p60`, `480p`... to save disk (best ~ 3 GB/hour) |

## Limits
- Must be running while the streamer is live. PC must stay on and awake.
- Ctrl+C mid-stream skips auto clipping; run the `clips` command on that folder after.
- Ad breaks can shift clip timing by a few seconds.
- Credit + link isn't permission. Many streamers allow clip channels, some don't. Check before you monetize.
