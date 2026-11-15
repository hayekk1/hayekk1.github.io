#!/usr/bin/env python3
"""Clip Watcher: record a Twitch stream + its chat, cut clips where chat spikes, add captions.

  python clipwatch.py record CHANNEL          # waits for live, records, clips when stream ends
  python clipwatch.py clips  SESSION_DIR      # re-run clipping on a finished recording
"""
import argparse, asyncio, json, random, re, shutil, subprocess, sys, time
from collections import Counter
from pathlib import Path

CHAT_WS = "wss://irc-ws.chat.twitch.tv:443"  # same chat as irc:6667, but works through firewalls


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


# ---------------- 1+2: record video + chat ----------------
async def chat_logger(channel, path):
    """Anonymous read-only chat. One line per message: epoch<TAB>user<TAB>text."""
    import websockets
    while True:
        try:
            async with websockets.connect(CHAT_WS, ping_interval=60) as ws:
                await ws.send(f"NICK justinfan{random.randint(10000, 99999)}")
                await ws.send(f"JOIN #{channel}")
                log(f"chat: listening to #{channel}")
                with open(path, "a", encoding="utf-8") as f:
                    async for frame in ws:
                        now = time.time()
                        for line in frame.split("\r\n"):
                            if line.startswith("PING"):
                                await ws.send("PONG" + line[4:])
                            elif " PRIVMSG " in line:
                                user = line[1:line.find("!")]
                                text = line.split(" PRIVMSG ", 1)[1].split(" :", 1)[-1]
                                f.write(f"{now:.2f}\t{user}\t{text.replace(chr(9), ' ')}\n")
                        f.flush()
        except Exception as e:
            log(f"chat: disconnected ({e!r}), retrying in 5s")
            await asyncio.sleep(5)


async def record(a):
    base = Path(a.out) / a.channel.lower()
    base.mkdir(parents=True, exist_ok=True)
    chat_task = asyncio.create_task(chat_logger(a.channel.lower(), base / "chat.log"))
    while True:
        session = base / time.strftime("%Y-%m-%d_%H%M%S")
        session.mkdir()
        ts = session / "stream.ts"
        log(f"waiting for twitch.tv/{a.channel} to go live (checks every 30s)...")
        proc = await asyncio.create_subprocess_exec(
            "streamlink", "--retry-streams", "30",
            f"twitch.tv/{a.channel}", f"{a.quality},best", "-o", str(ts))
        while proc.returncode is None and not ts.exists():
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                pass
        if ts.exists():
            (session / "meta.json").write_text(json.dumps({"channel": a.channel.lower(), "start": time.time()}))
            log(f"LIVE - recording to {ts}")
        await proc.wait()
        if ts.exists() and ts.stat().st_size > 1_000_000:
            log("stream ended, making clips...")
            try:
                await asyncio.to_thread(make_clips, session, a)
            except Exception as e:
                log(f"clipping failed: {e!r}  (retry: python clipwatch.py clips {session})")
        else:
            shutil.rmtree(session, ignore_errors=True)
            if proc.returncode:
                log("streamlink failed (see error above), retrying in 30s")
                await asyncio.sleep(30)
                continue
        if a.once:
            chat_task.cancel()
            return


# ---------------- 3: find chat spikes ----------------
def find_spikes(offsets, duration, a):
    nbins = int(duration // a.bin) + 1
    counts = [0] * nbins
    for off in offsets:
        if 0 <= off < duration:
            counts[int(off // a.bin)] += 1
    hist = int(a.window // a.bin)
    cands = []
    for i, c in enumerate(counts):
        prev = counts[max(0, i - hist):i]
        if len(prev) < 6:  # need at least 1 minute of history
            continue
        avg = max(sum(prev) / len(prev), 0.5)
        if c >= a.min_msgs and c >= a.ratio * avg:
            cands.append({"t": i * a.bin, "msgs": c, "x": round(c / avg, 1)})
    picked = []  # strongest first, no two clips closer than 60s
    for c in sorted(cands, key=lambda c: -c["msgs"]):
        if all(abs(c["t"] - p["t"]) > 60 for p in picked):
            picked.append(c)
        if len(picked) >= a.max_clips:
            break
    return sorted(picked, key=lambda c: c["t"])


def hms(s):
    s = int(s)
    return f"{s // 3600}h{s % 3600 // 60:02d}m{s % 60:02d}s"


# ---------------- 4+5: cut + captions ----------------
def srt_time(t):
    ms = int(round(t * 1000))
    return f"{ms // 3600000:02d}:{ms // 60000 % 60:02d}:{ms // 1000 % 60:02d},{ms % 1000:03d}"


def make_clips(session, a):
    session = Path(session).resolve()
    meta = json.loads((session / "meta.json").read_text())
    ts = session / "stream.ts"
    start, end = meta["start"], ts.stat().st_mtime
    chat = []
    for line in (session.parent / "chat.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split("\t", 2)
        if len(p) == 3 and start <= float(p[0]) <= end:
            chat.append((float(p[0]) - start, p[2]))
    spikes = find_spikes([o for o, _ in chat], end - start, a)
    log(f"{len(chat)} chat messages, {len(spikes)} spikes")
    out = session / "clips"
    out.mkdir(exist_ok=True)
    model = None
    if spikes and not a.no_captions:
        from faster_whisper import WhisperModel
        log(f"loading whisper model '{a.model}' (first run downloads it)")
        model = WhisperModel(a.model, device="auto", compute_type="int8")
    summary = []
    for n, s in enumerate(spikes, 1):
        words = Counter(w for o, txt in chat if s["t"] <= o < s["t"] + a.bin for w in set(txt.split()))
        top = words.most_common(1)[0][0] if words else "chat"
        name = f"{n:02d}_{hms(s['t'])}_{re.sub(r'[^A-Za-z0-9]', '', top)[:20] or 'spike'}"
        cs = max(0, s["t"] - a.lead)
        raw = out / f"{name}_raw.mp4"
        subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-ss", str(cs), "-i", str(ts), "-t", str(a.length),
                        "-c", "copy", "-avoid_negative_ts", "make_zero", str(raw)], check=True)
        vf = []
        if a.vertical:
            vf.append("crop=ih*9/16:ih,scale=1080:1920")
        if model:
            import numpy as np  # decode audio with ffmpeg (avoids PyAV version issues)
            pcm = subprocess.run(["ffmpeg", "-loglevel", "error", "-i", str(raw), "-f", "f32le", "-ac", "1",
                                  "-ar", "16000", "-"], capture_output=True, check=True).stdout
            segs, _ = model.transcribe(np.frombuffer(pcm, np.float32), language=a.lang, vad_filter=True)
            srt = [f"{i}\n{srt_time(g.start)} --> {srt_time(g.end)}\n{g.text.strip()}\n"
                   for i, g in enumerate(segs, 1)]
            if srt:
                (out / f"{name}.srt").write_text("\n".join(srt), encoding="utf-8")
                vf.append(f"subtitles={name}.srt:force_style='FontSize=20,Outline=2,MarginV=40'")
        final = out / f"{name}.mp4"
        if vf:
            subprocess.run(["ffmpeg", "-y", "-loglevel", "error", "-i", raw.name, "-vf", ",".join(vf),
                            "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "copy", final.name],
                           check=True, cwd=out)
            raw.unlink()
        else:
            raw.replace(final)
        when = time.strftime("%Y-%m-%d", time.localtime(start))
        summary.append(f"{final.name}\n  chat: {s['msgs']} msgs in {a.bin}s ({s['x']}x normal), top word: {top}\n"
                       f"  stream time: {hms(cs)}\n  CREDIT: Clip from twitch.tv/{meta['channel']} ({when} stream)\n")
        log(f"clip {n}/{len(spikes)}: {final.name}")
    (out / "clips.txt").write_text("\n".join(summary) or "No spikes found. Try --ratio 2 or --min-msgs 5.\n")
    log(f"done -> {out}")


def main():
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    r = sub.add_parser("record", help="wait for a channel to go live, record video+chat, clip at the end")
    r.add_argument("channel")
    r.add_argument("--quality", default="best", help="best, 1080p60, 720p60, 480p30... (falls back to best)")
    r.add_argument("--out", default="recordings")
    r.add_argument("--once", action="store_true", help="exit after one stream instead of waiting for the next")
    c = sub.add_parser("clips", help="(re)make clips from a recorded session folder")
    c.add_argument("session")
    for p in (r, c):
        p.add_argument("--ratio", type=float, default=3.0, help="spike = this many x the last 5 min avg (default 3)")
        p.add_argument("--min-msgs", type=int, default=10, help="ignore spikes under N msgs per 10s (default 10)")
        p.add_argument("--max-clips", type=int, default=10)
        p.add_argument("--lead", type=int, default=30, help="seconds before the chat spike to start (default 30)")
        p.add_argument("--length", type=int, default=45, help="clip length in seconds (default 45)")
        p.add_argument("--model", default="small", help="whisper model: tiny/base/small/medium/large-v3 (default small)")
        p.add_argument("--lang", default=None, help="speech language, e.g. en, ar (default: auto-detect)")
        p.add_argument("--no-captions", action="store_true")
        p.add_argument("--vertical", action="store_true", help="center-crop to 9:16 for Shorts/TikTok/Reels")
        p.set_defaults(bin=10, window=300)
    a = ap.parse_args()
    for tool in ("ffmpeg", "streamlink"):
        if not shutil.which(tool):
            sys.exit(f"missing '{tool}' - see README.md")
    try:
        asyncio.run(record(a)) if a.cmd == "record" else make_clips(a.session, a)
    except KeyboardInterrupt:
        log("stopped")


if __name__ == "__main__":
    main()
