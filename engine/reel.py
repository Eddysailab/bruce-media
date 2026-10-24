#!/usr/bin/env python3
"""Bruce reel renderer v2: spec.json -> 9:16 MP4 explainer reel (30 to 40 s).

Usage: python3 engine/reel.py spec.json posts/YYYY-MM-DD-slug [--preview]
  --preview  QC contact sheet, cover and audio only, skip the full video

Run engine/setup_reel.sh once per session first (voice model, logo and icon libraries).

spec.json:
{"tag": "Yelp AI", "voice": "am_onyx", "speed": 1.05,
 "scenes": [
  {"kind":"hook",    "say":"...", "logo":"yelp", "kicker":"New from Yelp, Oct 20", "title":"...", "accent":"word"},
  {"kind":"sources", "say":"...", "items":[
      {"publication":"Yelp Official Blog", "domain":"blog.yelp.com", "logo":"yelp", "date":"Oct 20, 2026",
       "headline":"real headline", "accent":"phrase in it"},
      {"publication":"PPC Land", "domain":"ppc.land", "date":"Oct 21, 2026", "headline":"...", "accent":"..."}]},
  {"kind":"article", "say":"...", "publication":"...", "domain":"...", "logo":"...", "date":"...", "headline":"...", "accent":"..."},
  {"kind":"logos",   "say":"...", "title":"...", "accent":"...", "logos":["yelp","opentable"], "names":{"opentable":"OpenTable"}},
  {"kind":"stat",    "say":"...", "big":"$30", "label":"...", "source":"PPC Land, Oct 2026"},
  {"kind":"point",   "say":"...", "title":"...", "accent":"...", "icon":"eye", "body":"optional"},
  {"kind":"cta",     "say":"...", "title":"...", "accent":"...", "keyword":"YELP"}
 ]}
"say" is the narration, written exactly as spoken ("thirty dollars a month"). Captions come from it.
Every reel needs one "sources" scene (2 or 3 real articles, real headlines).
"""
import base64, html, io, json, math, pathlib, re, subprocess, sys, wave

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / ".cache"
NODE = HERE / "node_modules"
W, H, FPS = 1080, 1920, 30
SR = 24000
MIN_S, MAX_S = 30.0, 40.0
GAP = 0.22           # breath after each scene's narration
SFX_LEVEL = 0.7      # sound effects at 70% (Eddy, Oct 24: 30% quieter)
WPS = 3.6            # rough spoken words per second, for length advice

LIMITS = {
    "hook": {"kicker": 34, "title": 42},
    "point": {"title": 36, "body": 90},
    "article": {"publication": 26, "date": 14, "headline": 80, "domain": 28},
    "logos": {"title": 36},
    "stat": {"big": 6, "label": 50, "source": 40},
    "cta": {"title": 50},
}
SRC_LIMITS = {"publication": 26, "date": 14, "headline": 80, "domain": 28}
TRANSITIONS = ["zoom", "push", "whip", "circle", "wipe"]
ENTRANCES = ["punch", "slide", "flip", "drop"]

# ---------------------------------------------------------------- assets

def b64(p):
    return base64.b64encode(pathlib.Path(p).read_bytes()).decode()

def logo_variants():
    from PIL import Image
    im = Image.open(HERE / "assets/logo-black.png").convert("RGBA")
    a = im.getchannel("A")
    out = {}
    for name, rgb in (("black", (0, 0, 0)), ("white", (255, 255, 255))):
        c = Image.new("RGBA", im.size, rgb + (0,)); c.putalpha(a)
        buf = io.BytesIO(); c.save(buf, "PNG"); out[name] = base64.b64encode(buf.getvalue()).decode()
    return out

_SI = None
def brand(slug_or_name):
    global _SI
    if _SI is None:
        f = NODE / "simple-icons/data/simple-icons.json"
        _SI = {}
        for d in (json.loads(f.read_text()) if f.exists() else []):
            slug = d.get("slug") or re.sub(r"[^a-z0-9]", "", d["title"].lower())
            d["slug"] = slug
            _SI[slug] = d
            _SI.setdefault(re.sub(r"[^a-z0-9]", "", d["title"].lower()), d)
    d = _SI.get(re.sub(r"[^a-z0-9]", "", (slug_or_name or "").lower()))
    if not d:
        return None
    svg = NODE / f"simple-icons/icons/{d['slug']}.svg"
    if not svg.exists():
        return None
    m = re.search(r'<path d="([^"]+)"', svg.read_text())
    hexc = d["hex"]
    r, g, b = (int(hexc[i:i + 2], 16) for i in (0, 2, 4))
    if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.82:
        hexc = "000000"
    return (m.group(1), hexc) if m else None

def brand_svg(slug):
    b = brand(slug) if slug else None
    return f'<svg viewBox="0 0 24 24"><path d="{b[0]}" fill="#{b[1]}"/></svg>' if b else None

def icon(name):
    f = NODE / f"lucide-static/icons/{name}.svg"
    if not f.exists():
        f = NODE / "lucide-static/icons/sparkles.svg"
    t = f.read_text()
    inner = re.sub(r"^[\s\S]*?<svg[^>]*>", "", t)
    inner = re.sub(r"</svg>[\s\S]*$", "", inner)
    return re.sub(r"<(path|circle|rect|line|polyline|polygon|ellipse)\b", r'<\1 pathLength="1"', inner).strip()

# ---------------------------------------------------------------- voice

def tts(scenes, voice, speed):
    from kokoro_onnx import Kokoro
    k = Kokoro(str(CACHE / "kokoro.onnx"), str(CACHE / "voices.bin"))
    clips = []
    for s in scenes:
        a, sr = k.create(s["say"], voice=voice, speed=speed, lang="en-us")
        a = np.asarray(a, dtype=np.float32)
        nz = np.where(np.abs(a) > 0.01)[0]
        if len(nz):
            a = a[max(0, nz[0] - 480): nz[-1] + 1440]
        clips.append(a)
    return clips

# ---------------------------------------------------------------- timeline + captions

def words_of(text):
    return re.findall(r"\S+", text or "")

def build_timeline(scenes, clips):
    t, caps = 0.12, []
    for s, c in zip(scenes, clips):
        d = len(c) / SR
        s["_vo"], s["_vodur"] = round(t, 3), round(d, 3)
        ws = words_of(s["say"])
        wt = [len(re.sub(r"[^\w$%]", "", w)) + 2.2 + (2.5 if re.search(r"[.,!?;:]$", w) else 0) for w in ws]
        tot, cur, chunk = sum(wt) or 1, t, []
        for i, (w, x) in enumerate(zip(ws, wt)):
            dur = d * x / tot
            chunk.append({"w": w, "t0": round(cur, 3), "t1": round(cur + dur, 3)})
            cur += dur
            if len(chunk) >= 3 or re.search(r"[.,!?;:]$", w) or i == len(ws) - 1:
                caps.append({"t0": chunk[0]["t0"], "t1": chunk[-1]["t1"], "words": chunk}); chunk = []
        t += d + GAP
    for i, s in enumerate(scenes):
        nxt = scenes[i + 1]["_vo"] - 0.1 if i + 1 < len(scenes) else t + 0.9
        s["_start"] = 0.0 if i == 0 else round(s["_vo"] - 0.1, 3)
        s["_dur"] = round(nxt - s["_start"], 3)
        s["_tr"] = "" if i == 0 else s.get("transition") or TRANSITIONS[(i - 1) % len(TRANSITIONS)]
    for j in range(len(caps) - 1):
        caps[j]["t1"] = min(caps[j + 1]["t0"], caps[j]["t1"] + 0.5)
    return caps, round(t + 0.9, 3)

# ---------------------------------------------------------------- sound design (all generated, no downloads)

def env(n, a, d):
    t = np.arange(n) / SR
    return np.minimum(1, t / max(a, 1e-4)) * np.exp(-t / d)

def lp(x, a):
    y = np.empty_like(x); acc = 0.0
    for i in range(len(x)):
        acc += a * (x[i] - acc); y[i] = acc
    return y

RNG = np.random.default_rng(11)
def sfx_bank():
    B = {}
    n = int(0.42 * SR); t = np.arange(n) / SR
    noise = RNG.standard_normal(n)
    y = np.zeros(n); acc = 0.0
    for i in range(n):
        a = 0.03 + 0.35 * (t[i] / 0.42) ** 1.5
        acc += a * (noise[i] - acc); y[i] = acc
    B["whoosh"] = y * np.sin(np.pi * np.clip(t / 0.42, 0, 1)) ** 2
    n = int(0.9 * SR); t = np.arange(n) / SR
    boom = np.sin(2 * np.pi * (38 * t + 90 * (1 - np.exp(-t * 18)) / 18)) * np.exp(-t * 5)
    B["impact"] = boom + 0.35 * lp(RNG.standard_normal(n), 0.08) * np.exp(-t * 12)
    n = int(0.12 * SR); t = np.arange(n) / SR
    B["pop"] = np.sin(2 * np.pi * (900 * t - 2600 * t * t)) * env(n, 0.002, 0.03)
    n = int(0.03 * SR)
    B["tick"] = lp(RNG.standard_normal(n), 0.5) * env(n, 0.0005, 0.006)
    n = int(1.4 * SR); t = np.arange(n) / SR
    B["ding"] = sum(np.sin(2 * np.pi * f * t) * np.exp(-t * dcy) * g
                    for f, dcy, g in ((1318.5, 3.0, 1), (1975.5, 4.5, .5), (2637, 6.0, .3), (659.3, 2.2, .35))) * env(n, 0.002, 9)
    n = int(1.0 * SR); t = np.arange(n) / SR
    rnoise = lp(RNG.standard_normal(n), 0.15)
    B["riser"] = (rnoise + 0.4 * np.sin(2 * np.pi * (200 * t + 500 * t * t))) * (t / 1.0) ** 2.2
    n = int(0.35 * SR); t = np.arange(n) / SR
    B["swipe"] = lp(RNG.standard_normal(n), 0.25) * np.sin(np.pi * t / 0.35) ** 3
    gains = {"whoosh": .32, "impact": .55, "pop": .22, "tick": .10, "ding": .20, "riser": .18, "swipe": .20}
    for k in B:
        B[k] = (B[k] / (np.max(np.abs(B[k])) + 1e-9) * gains[k]).astype(np.float32)
    return B

def tone_bed(total, seed):
    n = int(total * SR); t = np.arange(n) / SR
    bpm = 112; beat = 60 / bpm; bar = beat * 4
    roots = [57, 53, 48, 55]
    out = np.zeros(n, np.float32)
    for bi in range(int(total / bar) + 1):
        r = roots[bi % 4]; s0 = int(bi * bar * SR); s1 = min(n, int((bi + 1) * bar * SR) + int(0.3 * SR))
        if s0 >= n: break
        tt = t[s0:s1] - bi * bar
        e_ = np.minimum(1, tt / 0.3) * np.exp(-np.maximum(0, tt - bar) * 5)
        for semi in (0, 3 if bi % 2 == 0 else 4, 7, 12):
            f = 440 * 2 ** ((r + semi - 69) / 12)
            out[s0:s1] += (0.045 * e_ * (np.sin(2 * np.pi * f * tt) + 0.4 * np.sin(2 * np.pi * f * 1.004 * tt))).astype(np.float32)
        f = 440 * 2 ** ((r - 24 - 69) / 12)   # bass pulse on 8ths
        for k in range(8):
            a0 = int((bi * bar + k * beat / 2) * SR)
            if a0 >= n: break
            m = min(int(0.2 * SR), n - a0); tb = np.arange(m) / SR
            out[a0:a0 + m] += (0.09 * np.sin(2 * np.pi * f * tb) * np.exp(-tb * 9)).astype(np.float32)
    k_len = int(0.25 * SR); kt = np.arange(k_len) / SR
    kick = np.sin(2 * np.pi * (45 * kt + 70 * (1 - np.exp(-kt * 30)) / 30)) * np.exp(-kt * 13) * 0.4
    hat = lp(RNG.standard_normal(int(0.04 * SR)), 0.9) * np.exp(-np.arange(int(0.04 * SR)) / SR * 120) * 0.04
    for b in range(int(total / beat)):
        s = int(b * beat * SR); out[s:s + k_len] += kick[: n - s].astype(np.float32)
        for off in (0.5,):
            so = int((b + off) * beat * SR)
            if so < n: out[so:so + len(hat)] += hat[: n - so].astype(np.float32)
    y = lp(out, 0.25)
    fade = int(1.2 * SR); y[-fade:] *= np.linspace(1, 0, fade)
    return y / (np.max(np.abs(y)) + 1e-9) * 0.5

def write_wav(path, a):
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((np.clip(a, -1, 1) * 32767).astype("<i2").tobytes())

def build_audio(scenes, clips, total, events, out, seed):
    n = int(total * SR) + SR
    vo = np.zeros(n, np.float32); fx = np.zeros(n, np.float32)
    for s, c in zip(scenes, clips):
        i = int(s["_vo"] * SR); vo[i:i + len(c)] += c
    B = sfx_bank()
    for tm, kind in events:
        snd = B[kind]
        st = int(max(0, tm) * SR)
        if kind == "riser":
            st = max(0, st - len(snd))
        if st < n:
            fx[st:st + len(snd)] += snd[: n - st]
    fx *= SFX_LEVEL
    write_wav(out / "vo.wav", vo); write_wav(out / "sfx.wav", fx)
    music = sorted((HERE / "music").glob("*.mp3")) + sorted((HERE / "music").glob("*.wav"))
    if music:
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-stream_loop", "-1", "-i", str(music[seed % len(music)]),
                        "-t", f"{total + 1:.2f}", "-af", f"afade=t=out:st={max(0, total - 1.2):.2f}:d=1.2",
                        "-ac", "1", "-ar", str(SR), str(out / "music.wav")], check=True)
    else:
        write_wav(out / "music.wav", tone_bed(total + 1, seed))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "vo.wav"), "-i", str(out / "music.wav"), "-i", str(out / "sfx.wav"),
                    "-filter_complex",
                    "[0:a]asplit=2[v][sc];[1:a]volume=0.6[mb];[mb][sc]sidechaincompress=threshold=0.02:ratio=8:attack=15:release=300[m];"
                    "[v][m][2:a]amix=inputs=3:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                    "-map", "[a]", "-t", f"{total:.2f}", "-ar", "48000", "-ac", "2", str(out / "mix.wav")], check=True)
    for f in ("vo.wav", "sfx.wav", "music.wav"):
        (out / f).unlink(missing_ok=True)

# ---------------------------------------------------------------- html

CSS = """
@font-face{font-family:'Bricolage';src:url(data:font/woff2;base64,%FONT%) format('woff2');font-weight:200 800;font-stretch:75% 100%}
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;background:#000}
body{font-family:'Bricolage',sans-serif;font-stretch:100%;font-optical-sizing:auto;-webkit-font-smoothing:antialiased}
#stage{position:absolute;inset:0;overflow:hidden}
.L{position:absolute;inset:0;overflow:hidden;will-change:transform}
.dark{background:#000;color:#fff}.light{background:#fff;color:#000}
.grid{position:absolute;inset:-240px;background-image:linear-gradient(currentColor 2px,transparent 2px),linear-gradient(90deg,currentColor 2px,transparent 2px);
 background-size:120px 120px;opacity:.07}
.ghost{position:absolute;top:1450px;left:0;white-space:nowrap;font-size:300px;font-weight:800;line-height:1;letter-spacing:-.04em;
 color:transparent;-webkit-text-stroke:2px currentColor;opacity:.13;text-transform:uppercase}
.orb{position:absolute;border-radius:50%;border:2px solid currentColor;opacity:.12}
.content{position:absolute;left:80px;right:140px;top:380px;height:880px;display:flex;flex-direction:column;justify-content:center;transform-origin:50% 40%}
.kicker{align-self:flex-start;font-size:30px;font-weight:700;letter-spacing:.12em;text-transform:uppercase;padding:12px 22px;border-radius:12px;margin-bottom:40px}
.dark .kicker{background:#fff;color:#000}.light .kicker{background:#000;color:#fff}
h1,h2{font-weight:800;line-height:1.02;letter-spacing:-.035em;perspective:900px}
h1{font-size:104px}h2{font-size:88px}
.w{display:inline-block;position:relative;margin-right:.22em;transform-origin:50% 80%;will-change:transform,opacity,filter}
.w .t{position:relative;z-index:1}
.acc .hb{position:absolute;left:-.12em;right:-.12em;top:.04em;bottom:-.04em;border-radius:.1em;transform-origin:left center;z-index:0}
.dark .acc .hb{background:#fff}.dark .acc .t{color:#000}
.light .acc .hb{background:#000}.light .acc .t{color:#fff}
.chip{width:170px;height:170px;border-radius:44px;display:flex;align-items:center;justify-content:center;margin-bottom:56px}
.dark .chip{background:#fff;color:#000}.light .chip{background:#000;color:#fff}
.chip svg{width:96px;height:96px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.chip svg *{stroke-dasharray:1;stroke-dashoffset:1}
.body{font-size:42px;line-height:1.28;font-weight:450;margin-top:36px;max-width:820px;opacity:.9}
.hlogo{width:180px;height:180px;border-radius:46px;background:#fff;display:flex;align-items:center;justify-content:center;margin-bottom:48px}
.hlogo svg{width:106px;height:106px}
.stack{position:relative;height:860px}
.card{position:absolute;left:0;right:0;background:#fff;color:#111;border-radius:28px;overflow:hidden;box-shadow:0 40px 100px rgba(0,0,0,.55),0 0 0 2px rgba(0,0,0,.08)}
.chrome{display:flex;align-items:center;gap:12px;background:#f1f1f1;padding:18px 24px;border-bottom:2px solid #e2e2e2}
.chrome i{width:16px;height:16px;border-radius:50%;background:#c9c9c9;display:block}
.chrome .url{margin-left:14px;flex:1;background:#fff;border-radius:999px;padding:8px 22px;font-size:24px;font-weight:500;color:#555;letter-spacing:.01em}
.cbody{padding:30px 42px 36px}
.pub{display:flex;align-items:center;gap:18px;font-size:32px;font-weight:800}
.pub svg{width:46px;height:46px}
.pub .date{margin-left:auto;font-size:25px;font-weight:500;color:#777}
.cbody hr{border:0;height:3px;background:#111;margin:22px 0 24px}
.hl{font-size:50px;font-weight:800;line-height:1.08;letter-spacing:-.03em}
.tw{display:none}
.mk{background:linear-gradient(#FFE14D,#FFE14D) no-repeat 0 90%/0% 46%;-webkit-box-decoration-break:clone;box-decoration-break:clone;padding:0 4px;margin:0 -4px}
.caret{display:inline-block;width:5px;height:.9em;background:#111;vertical-align:-.1em;margin-left:4px}
.srclabel{position:absolute;left:0;top:0;font-size:28px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;opacity:.75}
.tiles{display:flex;flex-wrap:wrap;gap:30px;margin-top:64px}
.tile{width:300px;height:300px;border-radius:40px;background:#fff;color:#111;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:24px;box-shadow:0 0 0 3px #000 inset}
.tile svg{width:130px;height:130px}
.tile .nm{font-size:32px;font-weight:800;text-align:center;padding:0 14px;line-height:1}
.tile .wm{font-size:50px;font-weight:800;text-align:center;line-height:1;padding:0 16px;letter-spacing:-.03em}
.big{font-size:300px;font-weight:800;line-height:.85;letter-spacing:-.05em}
.label{font-size:58px;font-weight:800;line-height:1.05;margin-top:40px;letter-spacing:-.03em}
.src{font-size:28px;letter-spacing:.06em;opacity:.65;margin-top:32px}
.dm{display:inline-flex;align-self:flex-start;background:#fff;color:#000;font-size:80px;font-weight:800;padding:26px 50px;border-radius:24px;margin-top:64px;letter-spacing:-.02em}
.alt{font-size:36px;font-weight:500;margin-top:34px}.alt b{font-weight:800}
.ctalogo{height:110px;margin-top:70px;align-self:flex-start}
.light .dm{background:#000;color:#fff}
.light .hlogo{box-shadow:0 0 0 3px #000 inset}
.light .card{box-shadow:0 30px 80px rgba(0,0,0,.28),0 0 0 3px #000}
.light .tile{box-shadow:0 0 0 3px #000 inset}
.dark .tile{box-shadow:none}
#top{position:absolute;left:80px;right:140px;top:200px;display:flex;align-items:center;justify-content:space-between;z-index:50;mix-blend-mode:difference;color:#fff}
#top img{height:66px}
#top .pill{border:3px solid #fff;border-radius:999px;padding:10px 26px;font-size:25px;font-weight:700;letter-spacing:.12em;text-transform:uppercase}
#bar{position:absolute;left:80px;right:140px;top:300px;height:6px;z-index:50;mix-blend-mode:difference}
#bar i{display:block;height:100%;background:#fff;width:0;border-radius:3px}
#flash{position:absolute;inset:0;background:#fff;opacity:0;z-index:70;pointer-events:none}
#cap{position:absolute;left:60px;right:120px;top:1330px;z-index:60;display:flex;justify-content:center}
#cap .p{background:rgba(0,0,0,.92);border:2px solid rgba(255,255,255,.2);border-radius:26px;padding:16px 26px;color:#fff;
 font-size:54px;font-weight:800;line-height:1.12;text-align:center;max-width:880px;letter-spacing:-.02em;transform-origin:50% 50%}
#cap .cw{display:inline-block;padding:2px 12px;border-radius:12px;margin:2px 0}
#cap .cw.on{background:#fff;color:#000;transform:scale(1.08)}
"""

JS = r"""
const D = window.__D;
const cl = x => Math.max(0, Math.min(1, x));
const out = x => 1 - Math.pow(1 - x, 3);
const outq = x => 1 - Math.pow(1 - x, 5);
const back = x => { const c = 2.0, c3 = c + 1; return 1 + c3 * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
const elastic = x => x === 0 || x === 1 ? x : Math.pow(2, -10 * x) * Math.sin((x * 10 - .75) * 2 * Math.PI / 3) + 1;
const pr = (lt, a, d) => cl((lt - a) / d);
const TR = 0.45;
let lastCap = -1;
function enter(tr, e) {   // incoming layer
  switch (tr) {
    case 'zoom':   return {tf: `scale(${1.6 - 0.6 * e})`, op: cl(e * 2.2), clip: 'none', blur: (1 - e) * 14};
    case 'push':   return {tf: `translateX(${(1 - e) * 100}%)`, op: 1, clip: 'none', blur: (1 - e) * 6};
    case 'whip':   return {tf: `translateY(${(1 - e) * 100}%)`, op: 1, clip: 'none', blur: (1 - e) * 18};
    case 'circle': return {tf: 'none', op: 1, clip: `circle(${e * 130}% at 50% 45%)`, blur: 0};
    case 'wipe':   { const x = e * 160 - 30; return {tf: 'none', op: 1, clip: `polygon(0 0, ${x}% 0, ${x - 30}% 100%, 0 100%)`, blur: 0}; }
    default:       return {tf: 'none', op: 1, clip: 'none', blur: 0};
  }
}
function leave(tr, e) {   // outgoing layer while the next one enters
  switch (tr) {
    case 'zoom':   return {tf: `scale(${1 + 0.35 * e})`, blur: e * 10, op: 1 - e * 0.6};
    case 'push':   return {tf: `translateX(${-e * 45}%)`, blur: e * 4, op: 1 - e * 0.4};
    case 'whip':   return {tf: `translateY(${-e * 40}%)`, blur: e * 16, op: 1};
    case 'circle': return {tf: `scale(${1 - 0.08 * e})`, blur: 0, op: 1 - e * 0.5};
    case 'wipe':   return {tf: `translateX(${-e * 12}%)`, blur: 0, op: 1 - e * 0.3};
    default:       return {tf: 'none', blur: 0, op: 1};
  }
}
function shake(t) {
  let x = 0, y = 0, r = 0;
  for (const s of D.shakes) { const d = t - s.t; if (d < 0 || d > 0.6) continue;
    const a = s.a * Math.exp(-d * 9); x += a * Math.sin(d * 71); y += a * Math.cos(d * 53); r += a * 0.04 * Math.sin(d * 47); }
  return `translate(${x}px, ${y}px) rotate(${r}deg)`;
}
function wordFx(el, style, e, raw) {
  switch (style) {
    case 'punch': el.style.transform = `scale(${1 + 0.9 * (1 - back(e))})`; el.style.filter = `blur(${(1 - e) * 12}px)`; break;
    case 'slide': el.style.transform = `translateX(${(1 - out(e)) * (el.dataset.dir === 'l' ? -260 : 260)}px) skewX(${(1 - out(e)) * -18}deg)`; el.style.filter = `blur(${(1 - e) * 8}px)`; break;
    case 'flip':  el.style.transform = `rotateX(${(1 - out(e)) * -95}deg) translateY(${(1 - out(e)) * 30}px)`; break;
    case 'drop':  el.style.transform = `translateY(${(1 - elastic(cl(raw))) * -160}px)`; break;
  }
  el.style.opacity = cl(raw * 3);
}
function render(t) {
  document.getElementById('bar').firstChild.style.width = (100 * cl(t / D.total)) + '%';
  let fl = 0; for (const f of D.flashes) { const d = t - f; if (d >= 0 && d < .4) fl = Math.max(fl, .75 * Math.exp(-d * 14)); }
  document.getElementById('flash').style.opacity = fl;
  document.getElementById('stage').style.transform = shake(t);
  D.scenes.forEach((s, i) => {
    const L = document.getElementById('L' + i), lt = t - s._start;
    const nx = D.scenes[i + 1];
    const end = nx ? nx._start + TR : D.total + 1;
    if (lt < 0 || t > end) { L.style.display = 'none'; return; }
    L.style.display = 'block';
    let tf = 'none', clip = 'none', blur = 0, op = 1;
    if (i > 0 && lt < TR) { const r = enter(s._tr, outq(pr(lt, 0, TR))); tf = r.tf; clip = r.clip; blur = r.blur; op = r.op; }
    if (nx && t >= nx._start) { const r = leave(nx._tr, out(pr(t - nx._start, 0, TR))); tf = r.tf; blur = r.blur; op = r.op; }
    L.style.transform = tf; L.style.clipPath = clip; L.style.filter = blur > 0.3 ? `blur(${blur}px)` : 'none'; L.style.opacity = op;
    const g = L.querySelector('.grid'); if (g) g.style.transform = `translate(${-lt * 18}px, ${-lt * 30}px)`;
    const gh = L.querySelector('.ghost'); if (gh) gh.style.transform = `translateX(${300 - lt * 160}px)`;
    L.querySelectorAll('.orb').forEach((o, k) => { o.style.transform = `translate(${Math.sin(lt * (0.6 + k * .3) + k) * 40}px, ${Math.cos(lt * (0.5 + k * .2)) * 50 - lt * 20}px)`; });
    const C = L.querySelector('.content');
    const punch = 1.07 - 0.07 * out(pr(lt, 0, 0.7));
    C.style.transform = `scale(${punch * (1 + 0.04 * cl(lt / Math.max(1, s._dur)))})` + (C.dataset.push ? ` translateY(${-out(pr(lt, +C.dataset.push, 0.9)) * 60}px) scale(${1 + 0.1 * out(pr(lt, +C.dataset.push, 0.9))})` : '');
    L.querySelectorAll('[data-fx]').forEach(el => {
      const a = +el.dataset.a || 0, fx = el.dataset.fx;
      if (fx === 'word') { const raw = pr(lt, a, 0.45); wordFx(el, el.dataset.st, raw, raw); }
      else if (fx === 'rise') { const e = out(pr(lt, a, 0.5)); el.style.opacity = e; el.style.transform = `translateY(${(1 - e) * 70}px)`; }
      else if (fx === 'fade') { el.style.opacity = out(pr(lt, a, 0.4)); }
      else if (fx === 'pop') { const p = pr(lt, a, 0.5); el.style.opacity = cl(p * 4); el.style.transform = `scale(${0.4 + 0.6 * back(p)}) rotate(${(1 - out(p)) * -8}deg)`; }
      else if (fx === 'hb') { el.style.transform = `scaleX(${outq(pr(lt, a, 0.3))})`; }
      else if (fx === 'accent') { const p = pr(lt, a, 0.35); el.style.transform = `rotate(${-2.5 * out(p)}deg) scale(${1 + 0.12 * Math.sin(Math.PI * p) + 0.02 * Math.sin(lt * 4) * cl(lt - a - .35)})`; }
      else if (fx === 'draw') { const e = out(pr(lt, a, 0.9)); el.querySelectorAll('*').forEach(p => p.style.strokeDashoffset = 1 - e); }
      else if (fx === 'card') { const e = outq(pr(lt, a, 0.6)); const rot = +el.dataset.rot || 0;
        el.style.opacity = cl(e * 3); el.style.transform = `translateY(${(1 - e) * 900}px) rotate(${rot + (1 - e) * (rot > 0 ? 14 : -14)}deg)`; }
      else if (fx === 'type') { const n = +el.dataset.n, dur = +el.dataset.d, k = Math.floor(pr(lt, a, dur) * n + 0.0001);
        const tws = el.querySelectorAll('.tw'); tws.forEach((w, j) => w.style.display = j < k ? 'inline' : 'none');
        const c = el.querySelector('.caret');
        if (c) { const last = tws[Math.max(0, k - 1)]; if (k > 0 && last.nextSibling !== c) last.after(c);
          c.style.opacity = (lt > a && (k < n || Math.floor(lt * 3) % 2 === 0)) ? 1 : 0; } }
      else if (fx === 'mark') { el.style.backgroundSize = `${100 * outq(pr(lt, a, 0.5))}% 46%`; }
      else if (fx === 'count') { const e = outq(pr(lt, a, 0.9)); const to = +el.dataset.to, dec = +el.dataset.dec || 0;
        el.textContent = el.dataset.pre + (to * e).toFixed(dec) + el.dataset.suf;
        el.style.transform = `scale(${1 + 0.25 * Math.max(0, 1 - Math.abs((lt - a - 0.9) * 6))})`; }
      else if (fx === 'pulse') { const p = pr(lt, a, 0.5); const k = lt > a + 0.5 ? 1 + 0.045 * Math.sin((lt - a) * 6) : 0.4 + 0.6 * back(p);
        el.style.opacity = cl(p * 3); el.style.transform = `scale(${k})`; }
    });
  });
  let ci = -1;
  for (let j = 0; j < D.caps.length; j++) if (t >= D.caps[j].t0 - 0.05 && t < D.caps[j].t1) { ci = j; break; }
  const cap = document.getElementById('cap');
  if (ci !== lastCap) {
    lastCap = ci;
    cap.innerHTML = ci < 0 ? '' : '<div class="p">' + D.caps[ci].words.map(w => `<span class="cw">${w.w.replace(/</g, '&lt;')}</span>`).join(' ') + '</div>';
  }
  if (ci >= 0) {
    const p = cap.firstChild, d = t - D.caps[ci].t0; p.style.transform = `scale(${0.85 + 0.15 * back(cl(d / 0.22))})`;
    cap.querySelectorAll('.cw').forEach((el, k) => { const w = D.caps[ci].words[k]; el.classList.toggle('on', t >= w.t0 && t < w.t1 + 0.04); });
  }
}
window.render = render;
"""

def e(x):
    return html.escape(str(x or ""))

class Ctx:
    def __init__(self):
        self.events, self.shakes, self.flashes = [], [], []
    def sfx(self, t, kind): self.events.append((round(t, 3), kind))
    def shake(self, t, a=14): self.shakes.append({"t": round(t, 3), "a": a})
    def flash(self, t): self.flashes.append(round(t, 3))

def norm(w):
    return re.sub(r"[^\w$%+'’]", "", w).lower()

def words_html(text, accent, start, step, style, ctx, t0, light=False):
    toks = words_of(text); acc = [norm(a) for a in words_of(accent)]
    lo = [norm(t) for t in toks]; s0 = -1
    for i in range(len(lo) - len(acc) + 1 if acc else 0):
        if lo[i:i + len(acc)] == acc: s0 = i; break
    out, d, acc_t = [], start, None
    for i, tk in enumerate(toks):
        is_acc = s0 >= 0 and s0 <= i < s0 + len(acc)
        st = style if style != "mix" else ENTRANCES[i % len(ENTRANCES)]
        hb = ""
        if is_acc:
            if acc_t is None:
                acc_t = d + 0.25
            hb = f'<span class="hb" data-fx="hb" data-a="{acc_t:.2f}" style="transform:scaleX(0)"></span>'
        inner = f'{hb}<span class="t">{e(tk)}</span>'
        word = f'<span class="w{" acc" if is_acc else ""}" data-fx="word" data-st="{st}" data-dir="{"l" if i % 2 else "r"}" data-a="{d:.2f}" style="opacity:0">{inner}</span>'
        if is_acc:
            word = f'<span class="w" data-fx="accent" data-a="{acc_t:.2f}" style="margin-right:0">{word}</span>'
        out.append(word); d += step
    if acc_t is not None:
        ctx.sfx(t0 + acc_t, "pop"); ctx.shake(t0 + acc_t, 8)
    return "".join(out), d

def typed_headline(text, accent, a, dur, ctx, t0):
    toks = words_of(text); acc = [norm(x) for x in words_of(accent)]
    lo = [norm(x) for x in toks]; s0 = -1
    for i in range(len(lo) - len(acc) + 1 if acc else 0):
        if lo[i:i + len(acc)] == acc: s0 = i; break
    parts, inmk = [], False
    for i, tk in enumerate(toks):
        if s0 >= 0 and i == s0:
            parts.append(f'<span class="mk" data-fx="mark" data-a="{a + dur + 0.1:.2f}">'); inmk = True
        parts.append(f'<span class="tw">{e(tk)} </span>')
        if inmk and i == s0 + len(acc) - 1:
            parts.append('</span>'); inmk = False
    if inmk: parts.append('</span>')
    n = len(toks)
    for j in range(n):
        if j % 2 == 0: ctx.sfx(t0 + a + dur * j / n, "tick")
    if s0 >= 0: ctx.sfx(t0 + a + dur + 0.1, "swipe")
    return f'<div class="hl" data-fx="type" data-a="{a:.2f}" data-d="{dur:.2f}" data-n="{n}">{"".join(parts)}<span class="caret"></span></div>'

def card_html(it, a, rot, top, ctx, t0, type_dur=0.9):
    lg = brand_svg(it.get("logo")) or ""
    dom = it.get("domain") or ""
    ctx.sfx(t0 + a, "whoosh"); ctx.sfx(t0 + a + 0.45, "pop"); ctx.shake(t0 + a + 0.45, 7)
    head = typed_headline(it["headline"], it.get("accent"), a + 0.5, type_dur, ctx, t0)
    return f'''<div class="card" data-fx="card" data-a="{a:.2f}" data-rot="{rot}" style="top:{top}px;opacity:0">
<div class="chrome"><i></i><i></i><i></i><span class="url">{e(dom)}</span></div>
<div class="cbody"><div class="pub">{lg}<span>{e(it["publication"])}</span><span class="date">{e(it.get("date"))}</span></div><hr>{head}</div></div>'''

def scene_html(s, i, logos, ctx):
    k, t0 = s["kind"], s["_start"]
    theme = s.get("theme") or ("dark" if i % 2 == 0 else "light")   # black and white alternate scene to scene
    style = s.get("style") or ["punch", "slide", "flip", "drop", "mix"][i % 5]
    ghost_word = s.get("ghost") or (s.get("accent") or s.get("keyword") or s.get("big") or "").split(" ")[0]
    deco = f'<div class="grid"></div><div class="ghost">{e(ghost_word)} {e(ghost_word)} {e(ghost_word)}</div>' \
           f'<div class="orb" style="width:420px;height:420px;left:640px;top:180px"></div><div class="orb" style="width:220px;height:220px;left:-60px;top:1250px"></div>'
    push = ""
    if i > 0:
        ctx.sfx(t0, "whoosh")
        if s["_tr"] in ("zoom", "whip"): ctx.flash(t0 + 0.12)
    if k == "hook":
        ctx.sfx(0.0, "impact"); ctx.flash(0.0); ctx.shake(0.05, 18)
        lg = brand_svg(s.get("logo"))
        tile = f'<div class="hlogo" data-fx="pop" data-a="0.02" style="opacity:0">{lg}</div>' if lg else ""
        if lg: ctx.sfx(0.05, "pop")
        t, _ = words_html(s["title"], s.get("accent"), 0.12, 0.06, "punch", ctx, t0)
        kick = f'<div class="kicker" data-fx="pop" data-a="0.3" style="opacity:0">{e(s.get("kicker"))}</div>' if s.get("kicker") else ""
        inner = f'{tile}{kick}<h1>{t}</h1>'
    elif k == "point":
        ctx.sfx(t0 + 0.15, "pop")
        t, d = words_html(s["title"], s.get("accent"), 0.35, 0.07, style, ctx, t0)
        body = f'<p class="body" data-fx="rise" data-a="{d + 0.2:.2f}" style="opacity:0">{e(s["body"])}</p>' if s.get("body") else ""
        inner = f'<div class="chip" data-fx="pop" data-a="0.12" style="opacity:0"><svg viewBox="0 0 24 24" data-fx="draw" data-a="0.3">{icon(s.get("icon", "sparkles"))}</svg></div><h2>{t}</h2>{body}'
    elif k == "sources":
        items = s["items"][:3]
        span = max(1.2, s["_vodur"] - 0.4)
        step = span / len(items)
        cards = []
        for j, it in enumerate(items):
            rot = [-3, 2.5, -1.5][j]
            gapy = 405 if len(items) == 2 else 260
            cards.append(card_html(it, 0.1 + j * step, rot, 56 + j * gapy, ctx, t0, type_dur=min(1.0, step * 0.55)))
        label = f'<div class="srclabel" data-fx="fade" data-a="0.6" style="opacity:0">Our sources</div>'
        inner = f'<div class="stack">{"".join(cards)}{label}</div>'
    elif k == "article":
        it = {f: s.get(f) for f in ("publication", "domain", "logo", "date", "headline", "accent")}
        inner = f'<div class="stack">{card_html(it, 0.05, -2, 150, ctx, t0, 1.0)}</div>'
        push = "1.9"
    elif k == "logos":
        t, d = words_html(s["title"], s.get("accent"), 0.12, 0.06, style, ctx, t0)
        tiles = []
        for j, name in enumerate(s["logos"][:4]):
            sv = brand_svg(name)
            label = e(s.get("names", {}).get(name, name.replace("-", " ").title()))
            body = f'{sv}<span class="nm">{label}</span>' if sv else f'<span class="wm">{label}</span>'
            ta = d + 0.15 + 0.2 * j
            ctx.sfx(t0 + ta, "pop")
            tiles.append(f'<div class="tile" data-fx="pop" data-a="{ta:.2f}" style="opacity:0">{body}</div>')
        inner = f'<h2>{t}</h2><div class="tiles">{"".join(tiles)}</div>'
    elif k == "stat":
        m = re.match(r"^([^\d]*)([\d.,]+)(.*)$", s["big"])
        if m:
            pre, num, suf = m.groups(); to = float(num.replace(",", "")); dec = len(num.split(".")[1]) if "." in num else 0
            big = f'<div class="big" data-fx="count" data-a="0.15" data-to="{to}" data-dec="{dec}" data-pre="{e(pre)}" data-suf="{e(suf)}">{e(pre)}0{e(suf)}</div>'
        else:
            big = f'<div class="big" data-fx="pop" data-a="0.15" style="opacity:0">{e(s["big"])}</div>'
        ctx.sfx(t0 + 1.05, "riser"); ctx.sfx(t0 + 1.05, "impact"); ctx.shake(t0 + 1.05, 16); ctx.flash(t0 + 1.05)
        src = f'<div class="src" data-fx="fade" data-a="1.2" style="opacity:0">Source: {e(s["source"])}</div>' if s.get("source") else ""
        inner = f'{big}<div class="label" data-fx="rise" data-a="0.5" style="opacity:0">{e(s["label"])}</div>{src}'
    elif k == "cta":
        t, d = words_html(s["title"], s.get("accent"), 0.12, 0.06, "slide", ctx, t0)
        ctx.sfx(t0 + d + 0.2, "ding"); ctx.shake(t0 + d + 0.2, 6)
        inner = f'''<h2>{t}</h2>
<div class="dm" data-fx="pulse" data-a="{d + 0.2:.2f}" style="opacity:0">DM &ldquo;{e(s["keyword"].upper())}&rdquo;</div>
<div class="alt" data-fx="fade" data-a="{d + 0.5:.2f}" style="opacity:0">or tap the link in bio: <b>blackwellgraphics.com</b></div>
<img class="ctalogo" data-fx="fade" data-a="{d + 0.8:.2f}" style="opacity:0" src="data:image/png;base64,{logos['white' if theme == 'dark' else 'black']}">'''
    else:
        raise SystemExit(f"unknown scene kind {k}")
    pa = f' data-push="{push}"' if push else ""
    return f'<div class="L {theme}" id="L{i}" style="display:none;z-index:{i + 1}">{deco}<div class="content"{pa}>{inner}</div></div>'

def page(spec, scenes, caps, total, ctx):
    logos = logo_variants()
    pi = 0
    for s in scenes:
        if s["kind"] == "point": s["_pi"] = pi; pi += 1
    layers = "".join(scene_html(s, i, logos, ctx) for i, s in enumerate(scenes))
    data = {"total": total, "caps": caps, "shakes": ctx.shakes, "flashes": ctx.flashes,
            "scenes": [{"_start": s["_start"], "_dur": s["_dur"], "_tr": s["_tr"]} for s in scenes]}
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS.replace('%FONT%', b64(HERE / 'fonts/bricolage.woff2'))}</style></head><body>
<div id="stage">{layers}</div>
<div id="top"><img src="data:image/png;base64,{logos['white']}"><span class="pill">{e(spec['tag'])}</span></div>
<div id="bar"><i></i></div><div id="cap"></div><div id="flash"></div>
<script>window.__D={json.dumps(data)};</script><script>{JS}</script></body></html>"""

# ---------------------------------------------------------------- checks

def validate(spec):
    sc, errs = spec["scenes"], []
    if not 4 <= len(sc) <= 7: errs.append("use 4 to 7 scenes")
    if sc[0]["kind"] != "hook": errs.append("first scene must be hook")
    if sc[-1]["kind"] != "cta": errs.append("last scene must be cta")
    if not any(s["kind"] == "sources" for s in sc): errs.append("include one sources scene with 2 or 3 real articles")
    for i, s in enumerate(sc, 1):
        for f, lim in LIMITS.get(s["kind"], {}).items():
            if len(s.get(f) or "") > lim: errs.append(f"scene {i} {f} is {len(s[f])} chars, max {lim}")
        if s["kind"] == "sources":
            if not 2 <= len(s.get("items", [])) <= 3: errs.append(f"scene {i} needs 2 or 3 items")
            for j, it in enumerate(s.get("items", []), 1):
                for f, lim in SRC_LIMITS.items():
                    if len(it.get(f) or "") > lim: errs.append(f"scene {i} item {j} {f} is {len(it[f])} chars, max {lim}")
                if not it.get("headline") or not it.get("publication"): errs.append(f"scene {i} item {j} needs publication and headline")
        if not s.get("say"): errs.append(f"scene {i} has no narration")
        blob = json.dumps(s, ensure_ascii=False)
        if "—" in blob or "–" in blob: errs.append(f"scene {i} contains a dash character, remove it")
    if errs:
        raise SystemExit("spec problems:\n- " + "\n- ".join(errs))

# ---------------------------------------------------------------- main

def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    preview = "--preview" in sys.argv
    spec = json.loads(pathlib.Path(args[0]).read_text())
    out = pathlib.Path(args[1]); out.mkdir(parents=True, exist_ok=True)
    validate(spec)
    scenes = spec["scenes"]
    clips = tts(scenes, spec.get("voice", "am_onyx"), float(spec.get("speed", 1.05)))
    caps, total = build_timeline(scenes, clips)
    if not MIN_S <= total <= MAX_S:
        diff = (MIN_S + 2 - total) if total < MIN_S else (total - MAX_S + 2)
        raise SystemExit(f"reel runs {total:.1f}s; it must land between {MIN_S:.0f} and {MAX_S:.0f}s. "
                         f"{'Add' if total < MIN_S else 'Cut'} about {diff * WPS:.0f} words of narration and re-render.")
    ctx = Ctx()
    doc = page(spec, scenes, caps, total, ctx)
    build_audio(scenes, clips, total, ctx.events, out, sum(map(ord, out.name)))

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H})
        pg.set_content(doc); pg.evaluate("document.fonts.ready"); pg.wait_for_timeout(500)
        over = []
        for i, s in enumerate(scenes):
            pg.evaluate(f"render({s['_start'] + s['_dur'] - 0.05})")
            if pg.evaluate(f"(()=>{{const L=document.querySelector('#L{i}');L.style.display='block';const c=L.querySelector('.content');c.style.transform='none';"
                           f"c.querySelectorAll('[data-fx]').forEach(x=>x.style.transform='none');"
                           f"return c.scrollHeight>c.clientHeight+4||c.scrollWidth>c.clientWidth+4}})()"):
                over.append(i + 1)
        if over:
            raise SystemExit(f"scene(s) {over} overflow the frame, shorten that copy and re-render")
        from PIL import Image
        shots = []
        for s in scenes:
            pg.evaluate(f"render({s['_start'] + min(s['_dur'] - 0.1, max(2.4, s['_dur'] * 0.75))})")
            shots.append(Image.open(io.BytesIO(pg.screenshot(type="jpeg", quality=80))))
        cw, ch = 270, 480
        cols = min(5, len(shots)); rows = math.ceil(len(shots) / cols)
        sheet = Image.new("RGB", (cols * (cw + 10) + 10, rows * (ch + 10) + 10), "#777")
        for j, im in enumerate(shots):
            sheet.paste(im.resize((cw, ch)), (10 + (j % cols) * (cw + 10), 10 + (j // cols) * (ch + 10)))
        sheet.save(out / "sheet.jpg", quality=85)
        pg.evaluate(f"render({min(1.4, scenes[0]['_dur'] - 0.1)})")
        pg.screenshot(path=str(out / "cover.jpg"), type="jpeg", quality=92)
        if not preview:
            nframes = int(math.ceil(total * FPS))
            ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                                   "-i", str(out / "mix.wav"), "-map", "0:v", "-map", "1:a",
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
                                   "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out / "reel.mp4")],
                                  stdin=subprocess.PIPE)
            for f in range(nframes):
                pg.evaluate(f"render({f / FPS})")
                ff.stdin.write(pg.screenshot(type="jpeg", quality=88))
            ff.stdin.close(); ff.wait()
            if ff.returncode: raise SystemExit("ffmpeg failed")
        b.close()
    fmt = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x % 1 * 1000):03d}"
    (out / "captions.srt").write_text("\n".join(f"{j}\n{fmt(c['t0'])} --> {fmt(c['t1'])}\n{' '.join(w['w'] for w in c['words'])}\n"
                                                for j, c in enumerate(caps, 1)))
    if not preview:
        (out / "mix.wav").unlink(missing_ok=True)
    mb = (out / "reel.mp4").stat().st_size / 1e6 if (out / "reel.mp4").exists() else 0
    print(json.dumps({"seconds": round(total, 1), "scenes": len(scenes), "sfx": len(ctx.events), "mp4_mb": round(mb, 1),
                      "files": sorted(x.name for x in out.iterdir())}))

if __name__ == "__main__":
    main()
