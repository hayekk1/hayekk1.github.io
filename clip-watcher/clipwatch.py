#!/usr/bin/env python3
"""Clip Watcher: let Twitch chat pick your clips.
Finds the moments chat explodes and cuts them into vertical, captioned, credited clips.

  python clipwatch.py vod https://www.twitch.tv/videos/123   past broadcast (recommended)
  python clipwatch.py vod LINK --preview                      only list the moments, cut nothing
  python clipwatch.py live CHANNEL                            record live, clip when the stream ends
  python clipwatch.py clips recordings/CHANNEL/<date>         re-clip a live recording
"""
import argparse, asyncio, json, random, re, shutil, subprocess, sys, time
from collections import Counter
from pathlib import Path
from statistics import median

HERE = Path(__file__).resolve().parent
CHAT_WS = "wss://irc-ws.chat.twitch.tv:443"  # Twitch IRC over WebSocket, works through firewalls
BOTS = {"nightbot", "streamelements", "streamlabs", "moobot", "fossabot", "wizebot", "sery_bot",
        "soundalerts", "botrixoficial", "kofistreambot", "pokemoncommunitygame", "blerp", "own3d"}
STREAMLINK = [sys.executable, "-m", "streamlink"]  # works even if pip's Scripts folder isn't on PATH
POLL = re.compile(r"\+?\d{1,2}|yo|si+|sí|no+|yes|me|here|aqui|aquí|presente?|yo tambi[eé]n|me too|a ?m[ií]|✋|🙋")
LIVE_NOW = {}  # session being recorded, so Ctrl+C can still clip it


def log(*a):
    print(time.strftime("[%H:%M:%S]"), *a, flush=True)


def hms(s):
    s = max(0, int(s))
    return f"{s // 3600}:{s % 3600 // 60:02d}:{s % 60:02d}"


def run(cmd, **kw):
    return subprocess.run([str(c) for c in cmd], check=True, **kw)


# ---------------- finding the moments ----------------
def find_moments(chat, duration, a):
    """chat = [(seconds, user, text)]. Score = people in bucket / (median of 5 min each side + 1)."""
    nb = int(duration // a.bucket) + 1
    counts, seen, words, answers = [0] * nb, set(), {}, {}
    for off, user, text in chat:
        b = int(off // a.bucket)
        if not 0 <= b < nb or user.lower() in BOTS or text.startswith("!") or (b, user) in seen:
            continue  # skip bots, !commands, and repeat messages from one person (spam can't fake a spike)
        seen.add((b, user))
        counts[b] += 1
        words.setdefault(b, Counter()).update(set(text.split()))
        answers.setdefault(b, Counter())[text.strip().lower()] += 1
    k = round(300 / a.bucket)
    score = [c / (median(counts[max(0, i - k):i + k + 1]) + 1) for i, c in enumerate(counts)]
    peaks = []
    for i in sorted(range(nb), key=lambda i: -score[i]):
        if len(peaks) >= a.top or score[i] < a.min_score:
            break
        t = i * a.bucket
        if not a.keep_polls and answers.get(i):  # chat answering the streamer ("+1", "yo", "si") isn't a moment
            polls = sum(n for m, n in answers[i].items() if POLL.fullmatch(m))
            if polls >= 0.25 * sum(answers[i].values()):
                continue
        if counts[i] >= a.min_msgs and all(abs(p["t"] - t) >= a.gap for p in peaks):
            top = words[i].most_common(1)[0][0] if words.get(i) else ""
            peaks.append({"t": t, "people": counts[i], "x": round(score[i], 1), "chat": top})
    return counts, peaks  # strongest first


def show(counts, peaks, duration, n_msgs, a):
    bars = " ▁▂▃▄▅▆▇█"
    per = max(1, -(-len(counts) // 60))
    cols = [max(counts[i:i + per]) for i in range(0, len(counts), per)]
    top = max(cols) or 1
    print(f"\n{n_msgs} chat messages, {hms(duration)} of stream\n")
    print("".join(bars[round(c / top * 8)] for c in cols))
    print("0:00:00".ljust(max(8, len(cols) - 7)) + hms(duration) + "\n")
    for n, p in enumerate(peaks, 1):
        print(f"#{n}  {hms(p['t'])}  {p['people']} people in {a.bucket}s  ({p['x']}x normal)  "
              f"chat: {p['chat'][:20]}  -> clip {hms(p['t'] - a.before)} to {hms(p['t'] + a.after)}")
    if not peaks:
        print("No moments found. Try a longer stream with a livelier chat, or --min-score 1.5")
    print()


# ---------------- captions ----------------
_model = None


def captions(cut, a):
    """Whisper speech-to-text, grouped into short punchy chunks of a few words."""
    global _model
    import numpy as np
    from faster_whisper import WhisperModel
    if _model is None:
        log(f"loading whisper '{a.model}' (first run downloads it)")
        _model = WhisperModel(a.model, device="cpu", compute_type="int8")
    pcm = run(["ffmpeg", "-loglevel", "error", *cut, "-vn", "-f", "f32le", "-ac", "1", "-ar", "16000", "-"],
              capture_output=True).stdout
    segs, _ = _model.transcribe(np.frombuffer(pcm, np.float32), language=a.lang, vad_filter=True,
                                word_timestamps=True)
    chunks, cur = [], []
    for w in (w for s in segs for w in (s.words or [])):
        if cur and (len(cur) >= a.words or w.start - cur[-1].end > 0.6):
            chunks.append(cur)
            cur = []
        cur.append(w)
    chunks += [cur] if cur else []
    return [(c[0].start, c[-1].end, "".join(w.word for w in c).strip()) for c in chunks]


def write_ass(path, events, tall):
    """Subtitle file with 3 styles: Cap (speech, bottom), Hook (first 3s, top), Credit (top, small)."""
    W, H = (1080, 1920) if tall else (1920, 1080)
    cap, hook, cred = (78, 84, 42) if tall else (60, 64, 34)
    low, top = (430, 230) if tall else (70, 100)  # keep clear of TikTok/Shorts buttons
    style = "Style: {},Arial,{},{},&H00000000,&H00000000,-1,0,0,0,100,100,0,0,1,{},0,{},40,40,{},1"
    def ts(t):
        cs = int(round(max(0, t) * 100))
        return f"{cs // 360000}:{cs // 6000 % 60:02d}:{cs // 100 % 60:02d}.{cs % 100:02d}"
    lines = ["[Script Info]", "ScriptType: v4.00+", f"PlayResX: {W}", f"PlayResY: {H}", "WrapStyle: 0",
             "ScaledBorderAndShadow: yes", "", "[V4+ Styles]",
             "Format: Name, Fontname, Fontsize, PrimaryColour, OutlineColour, BackColour, Bold, Italic, Underline, "
             "StrikeOut, ScaleX, ScaleY, Spacing, Angle, BorderStyle, Outline, Shadow, Alignment, MarginL, "
             "MarginR, MarginV, Encoding",
             style.format("Cap", cap, "&H00FFFFFF", 6, 2, low), style.format("Hook", hook, "&H0000FFFF", 7, 8, top),
             style.format("Credit", cred, "&H00FFFFFF", 4, 8, top - cred - 20), "", "[Events]",
             "Format: Layer, Start, End, Style, Name, MarginL, MarginR, MarginV, Effect, Text"]
    for s, e, st, text in events:
        text = text.replace("\\", "/").replace("{", "(").replace("}", ")")
        lines.append(f"Dialogue: 0,{ts(s)},{ts(e)},{st},,0,0,0,,{text}")
    path.write_text("\n".join(lines) + "\n", encoding="utf-8")


# ---------------- cutting ----------------
LAYOUTS = {
    "crop": "crop=ih*9/16:ih,scale=1080:1920",  # 9:16 centre crop, fills the screen
    "fit": "split[a][b];[a]scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,"
           "boxblur=20:2[bg];[b]scale=1080:-2[fg];[bg][fg]overlay=(W-w)/2:(H-h)/2",  # whole frame on blur
    "wide": "",  # keep 16:9
}


def make_clips(chat, duration, source, out, channel, date, a):
    counts, peaks = find_moments(chat, duration, a)
    show(counts, peaks, duration, len(chat), a)
    out.mkdir(parents=True, exist_ok=True)
    for p in peaks:
        p.update(start=max(0, p["t"] - a.before), end=p["t"] + a.after)
    (out / "moments.json").write_text(json.dumps(peaks, indent=2))
    if a.preview or not peaks:
        log(f"saved {out / 'moments.json'}" + ("  (run again without --preview to cut)" if a.preview else ""))
        return
    src = source() if callable(source) else source
    notes = []
    for n, p in enumerate(peaks, 1):
        name = f"{n:02d}_{hms(p['t']).replace(':', '-')}_{re.sub(r'[^A-Za-z0-9]', '', p['chat'])[:16] or 'clip'}"
        final = out / f"{name}.mp4"
        cut = ["-ss", p["start"], "-i", src, "-t", a.before + a.after]  # frame-accurate, reads only this part
        log(f"#{n}/{len(peaks)} cutting {hms(p['start'])} ({a.before + a.after}s)")
        events = [] if a.no_captions else [(s, e, "Cap", t) for s, e, t in captions(cut, a)]
        if a.hook:
            events.append((0, 3, "Hook", a.hook))
        if not a.no_credit:
            events.append((0, a.before + a.after, "Credit", f"twitch.tv/{channel}"))
        vf = [LAYOUTS[a.layout]] if LAYOUTS[a.layout] else []
        if events:
            write_ass(out / f"{name}.ass", events, tall=a.layout != "wide")
            vf.append(f"ass={name}.ass")
        run(["ffmpeg", "-y", "-loglevel", "error", *cut, *(["-vf", ",".join(vf)] if vf else []),
             "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-c:a", "aac", "-b:a", "160k",
             "-movflags", "+faststart", final.name], cwd=out)
        notes.append(f"#{n} {final.name}\n   {p['x']}x normal chat, {p['people']} people in {a.bucket}s, "
                     f"chat said: {p['chat']}\n   stream time {hms(p['start'])} - {hms(p['end'])}\n"
                     f"   CREDIT: Clip from twitch.tv/{channel} ({date} stream)\n")
    (out / "clips.txt").write_text("Post #1 first (biggest moment).\n\n" + "\n".join(notes), encoding="utf-8")
    log(f"done: {len(peaks)} clips in {out}")


# ---------------- VOD mode ----------------
def find_tdl(a):
    for c in [a.tdl, "TwitchDownloaderCLI", HERE / "TwitchDownloaderCLI", HERE / "TwitchDownloaderCLI.exe",
              Path.cwd() / "TwitchDownloaderCLI", Path.cwd() / "TwitchDownloaderCLI.exe"]:
        if c and (shutil.which(str(c)) or Path(c).is_file()):
            return shutil.which(str(c)) or str(c)
    sys.exit("TwitchDownloaderCLI not found (needed to download VOD chat). See README.md step 3, or pass --tdl PATH")


def download_chat(vid, path, workers=12):
    """Download a VOD's chat with twitch-dl, many time chunks in parallel. Saves TwitchDownloader-style JSON."""
    from concurrent.futures import ThreadPoolExecutor, as_completed
    from twitchdl import twitch
    video = twitch.get_video(vid)
    if not video:
        sys.exit("VOD not found (deleted, expired or sub-only?)")
    length = video["lengthSeconds"]
    step = max(300, -(-length // (workers * 4)))

    def page(**kw):
        for attempt in range(5):
            try:
                return twitch.get_comments(vid, **kw)["comments"]
            except Exception:
                time.sleep(2 + attempt * 3)
        raise RuntimeError(f"chat download failed at {kw}")

    def chunk(start):
        got, c = [], page(offset_seconds=start)
        while True:
            for e in c["edges"]:
                n = e["node"]
                if n["contentOffsetSeconds"] >= start + step:
                    return got
                if n["contentOffsetSeconds"] >= start:
                    got.append(n)
            if not c["edges"] or not c["pageInfo"]["hasNextPage"]:
                return got
            c = page(cursor=c["edges"][-1]["cursor"])

    parts, starts = [], range(0, length, step)
    with ThreadPoolExecutor(workers) as ex:
        for i, f in enumerate(as_completed([ex.submit(chunk, t) for t in starts]), 1):
            parts += f.result()
            print(f"\r  chat: {round(100 * i / len(starts))}%  ({len(parts)} messages)", end="", flush=True)
    print()
    seen, comments = set(), []
    for n in sorted(parts, key=lambda n: n["contentOffsetSeconds"]):
        if n["id"] not in seen:
            seen.add(n["id"])
            comments.append({"content_offset_seconds": n["contentOffsetSeconds"],
                             "commenter": {"name": (n.get("commenter") or {}).get("login") or ""},
                             "message": {"body": "".join(f.get("text") or "" for f in n["message"]["fragments"])}})
    path.write_text(json.dumps({"streamer": {"name": video["creator"]["login"]},
                                "video": {"id": vid, "created_at": video["publishedAt"], "length": length,
                                          "title": video["title"]}, "comments": comments}), encoding="utf-8")


def vod(a):
    m = re.search(r"(\d{6,})", a.vod)
    if not m:
        sys.exit("Give a VOD link like https://www.twitch.tv/videos/1234567890")
    vid = m.group(1)
    out = Path(a.out) / f"vod_{vid}"
    out.mkdir(parents=True, exist_ok=True)
    chat_file = Path(a.chat) if a.chat else out / "chat.json"
    if not chat_file.exists():
        log("downloading chat (a long, busy VOD can take a few minutes)...")
        if a.tdl:
            run([find_tdl(a), "chatdownload", "--id", vid, "-o", chat_file])
        else:
            download_chat(vid, chat_file)
    data = json.loads(chat_file.read_text(encoding="utf-8-sig"))
    chat = [(c["content_offset_seconds"], (c.get("commenter") or {}).get("name") or "",
             (c.get("message") or {}).get("body") or "") for c in data.get("comments", [])]
    if not chat:
        sys.exit("No chat messages found. Is this a TwitchDownloader chat .json?")
    video = data.get("video") or {}
    duration = max(video.get("length") or 0, max(c[0] for c in chat))
    channel = (data.get("streamer") or {}).get("name") or "streamer"

    def source():
        if a.video:
            return str(Path(a.video).resolve())
        log("getting VOD video link...")
        try:
            return run([*STREAMLINK, "--stream-url", f"https://www.twitch.tv/videos/{vid}", a.quality],
                       capture_output=True, text=True).stdout.strip()
        except subprocess.CalledProcessError as e:
            sys.exit(f"Can't open the VOD video (sub-only, deleted or expired?)\n{e.stdout}{e.stderr}")
    make_clips(chat, duration, source, out / "clips", channel.lower(), (video.get("created_at") or "")[:10], a)


# ---------------- live mode ----------------
async def chat_logger(channel, path):
    """Anonymous, read-only. One line per message: epoch<TAB>user<TAB>text."""
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


async def live(a):
    base = Path(a.out) / a.channel.lower()
    base.mkdir(parents=True, exist_ok=True)
    chat_task = asyncio.create_task(chat_logger(a.channel.lower(), base / "chat.log"))
    while True:
        session = base / time.strftime("%Y-%m-%d_%H%M%S")
        session.mkdir()
        ts = session / "stream.ts"
        log(f"waiting for twitch.tv/{a.channel} to go live (checks every 30s)...")
        proc = await asyncio.create_subprocess_exec(
            *STREAMLINK, "--retry-streams", "30", f"twitch.tv/{a.channel}", a.quality, "-o", str(ts))
        while proc.returncode is None and not ts.exists():
            try:
                await asyncio.wait_for(proc.wait(), 1)
            except asyncio.TimeoutError:
                pass
        if ts.exists():
            (session / "meta.json").write_text(json.dumps({"channel": a.channel.lower(), "start": time.time()}))
            LIVE_NOW["session"] = session
            log(f"LIVE - recording to {ts}  (Ctrl+C stops and still makes the clips)")
        await proc.wait()
        LIVE_NOW.clear()
        if ts.exists() and ts.stat().st_size > 1_000_000:
            log("stream ended, making clips...")
            try:
                await asyncio.to_thread(clip_session, session, a)
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


def clip_session(session, a):
    session = Path(session).resolve()
    meta = json.loads((session / "meta.json").read_text())
    ts = session / "stream.ts"
    start, end = meta["start"], ts.stat().st_mtime
    chat = []
    for line in (session.parent / "chat.log").read_text(encoding="utf-8", errors="replace").splitlines():
        p = line.split("\t", 2)
        if len(p) == 3 and start <= float(p[0]) <= end:
            chat.append((float(p[0]) - start, p[1], p[2]))
    make_clips(chat, end - start, ts, session / "clips", meta["channel"],
               time.strftime("%Y-%m-%d", time.localtime(start)), a)


# ---------------- command line ----------------
def main():
    sys.stdout.reconfigure(errors="replace")
    common = argparse.ArgumentParser(add_help=False)
    g = common.add_argument_group("clip settings")
    g.add_argument("--top", type=int, default=5, help="how many clips (default 5)")
    g.add_argument("--before", type=int, default=20, help="seconds before the chat spike (default 20)")
    g.add_argument("--after", type=int, default=10, help="seconds after the chat spike (default 10)")
    g.add_argument("--gap", type=int, default=90, help="min seconds between two clips (default 90)")
    g.add_argument("--bucket", type=int, default=10, help="counting slice in seconds (default 10)")
    g.add_argument("--min-msgs", type=int, default=5, help="ignore moments with fewer people (default 5)")
    g.add_argument("--min-score", type=float, default=2, help="ignore moments under Nx normal (default 2)")
    g.add_argument("--keep-polls", action="store_true", help='keep moments where chat answers the streamer ("+1", "yo")')
    g.add_argument("--layout", choices=LAYOUTS, default="crop",
                   help="crop = 9:16 fill (default), fit = whole frame on blurred 9:16, wide = keep 16:9")
    g.add_argument("--model", default="small", help="whisper: tiny/base/small/medium/large-v3 (default small)")
    g.add_argument("--lang", default=None, help="speech language e.g. en, ar, es (default auto)")
    g.add_argument("--words", type=int, default=4, help="max words per caption (default 4)")
    g.add_argument("--no-captions", action="store_true")
    g.add_argument("--no-credit", action="store_true", help="no twitch.tv/name on screen")
    g.add_argument("--hook", help='text on screen for the first 3s, e.g. "wait for it"')
    g.add_argument("--preview", action="store_true", help="only list the moments, cut nothing")
    g.add_argument("--quality", default="best", help="video quality (default best)")
    g.add_argument("--out", default="recordings", help="output folder (default recordings)")

    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    v = sub.add_parser("vod", parents=[common], help="clip a past broadcast")
    v.add_argument("vod", help="VOD link or id")
    v.add_argument("--chat", help="TwitchDownloader chat .json you already have")
    v.add_argument("--video", help="cut from a video file you already have")
    v.add_argument("--tdl", help="use TwitchDownloaderCLI at this path for the chat instead")
    l = sub.add_parser("live", aliases=["record"], parents=[common], help="record a live stream and clip it")
    l.add_argument("channel")
    l.add_argument("--once", action="store_true", help="stop after one stream")
    c = sub.add_parser("clips", parents=[common], help="re-clip a live recording folder")
    c.add_argument("session")
    a = ap.parse_args()
    a.quality = f"{a.quality},best"

    if not shutil.which("ffmpeg"):
        sys.exit("ffmpeg not found - see README.md")
    try:
        import streamlink, websockets, faster_whisper, twitchdl  # noqa: F401
    except ImportError as e:
        sys.exit(f"{e.name} missing - run: py -m pip install -r requirements.txt")
    try:
        if a.cmd == "vod":
            vod(a)
        elif a.cmd == "clips":
            clip_session(a.session, a)
        else:
            asyncio.run(live(a))
    except KeyboardInterrupt:
        session = LIVE_NOW.get("session")
        if session and (session / "stream.ts").exists():
            log("stopped - making clips from what was recorded...")
            time.sleep(2)
            clip_session(session, a)
        else:
            log("stopped")


if __name__ == "__main__":
    main()
