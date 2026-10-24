#!/usr/bin/env python3
"""Bruce reel renderer: spec.json -> 9:16 MP4 explainer reel (45 to 60 s).

Usage: python3 engine/reel.py spec.json posts/YYYY-MM-DD-slug [--preview]
  --preview  render the QC contact sheet and audio only, skip the full video

Run engine/setup_reel.sh once per session first (voice model, logo and icon libraries).

spec.json:
{"tag": "Yelp AI", "voice": "am_onyx", "speed": 1.05,
 "scenes": [
  {"kind":"hook",    "say":"...", "kicker":"New from Yelp, Oct 20", "title":"Yelp's AI now books the table", "accent":"books"},
  {"kind":"article", "say":"...", "publication":"Yelp", "logo":"yelp", "date":"Oct 20, 2026",
                     "headline":"Yelp unveils 30+ new features in its fall release", "accent":"30+ new features"},
  {"kind":"logos",   "say":"...", "title":"Booking now runs through", "logos":["yelp","opentable"]},
  {"kind":"stat",    "say":"...", "big":"$30", "label":"a month for Smart Lead Reply", "source":"PPC Land, Oct 2026"},
  {"kind":"point",   "say":"...", "title":"It reads what you show it", "accent":"show", "icon":"eye", "body":"optional short line"},
  {"kind":"cta",     "say":"...", "title":"Make the page Yelp's AI reads look like you.", "keyword":"YELP"}
 ]}
"say" is the narration for that scene, written exactly as it should be spoken
("thirty dollars a month", not "$30/mo"). Captions are built from it.
"""
import base64, html, io, json, math, os, pathlib, random, re, shutil, subprocess, sys, wave

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / ".cache"
NODE = HERE / "node_modules"
W, H, FPS = 1080, 1920, 30
SR = 24000
MIN_S, MAX_S = 45.0, 60.0
GAP = 0.32          # pause after each scene's narration
LEAD = 0.25         # silence before the first word
MAX_SFX = 4         # transition whooshes per reel (keeps it clean)

LIMITS = {  # characters, so type always fits
    "hook": {"kicker": 34, "title": 48},
    "point": {"title": 40, "body": 110},
    "article": {"publication": 26, "date": 18, "headline": 95},
    "logos": {"title": 42},
    "stat": {"big": 6, "label": 60, "source": 40},
    "cta": {"title": 60},
}

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
    """Return (svg_path_d, hex) for a brand from simple-icons, or None."""
    global _SI
    if _SI is None:
        f = NODE / "simple-icons/data/simple-icons.json"
        data = json.loads(f.read_text()) if f.exists() else []
        _SI = {}
        for d in data:
            slug = d.get("slug") or re.sub(r"[^a-z0-9]", "", d["title"].lower().replace("+", "plus").replace(".", "dot").replace("&", "and"))
            _SI[slug] = d
            _SI.setdefault(re.sub(r"[^a-z0-9]", "", d["title"].lower()), d)
    key = re.sub(r"[^a-z0-9]", "", (slug_or_name or "").lower())
    d = _SI.get(key)
    if not d:
        return None
    svg = NODE / f"simple-icons/icons/{d.get('slug', key)}.svg"
    if not svg.exists():
        svg = NODE / f"simple-icons/icons/{key}.svg"
    if not svg.exists():
        return None
    m = re.search(r'<path d="([^"]+)"', svg.read_text())
    hexc = d["hex"]
    r, g, b = (int(hexc[i:i + 2], 16) for i in (0, 2, 4))
    if (0.2126 * r + 0.7152 * g + 0.0722 * b) / 255 > 0.82:   # too pale to read on white
        hexc = "000000"
    return (m.group(1), hexc) if m else None

def icon(name):
    f = NODE / f"lucide-static/icons/{name}.svg"
    if not f.exists():
        f = NODE / "lucide-static/icons/sparkles.svg"
    t = f.read_text()
    inner = re.sub(r"^[\s\S]*?<svg[^>]*>", "", t)
    inner = re.sub(r"</svg>[\s\S]*$", "", inner)
    inner = re.sub(r"<(path|circle|rect|line|polyline|polygon|ellipse)\b", r'<\1 pathLength="1"', inner)
    return inner.strip()

# ---------------------------------------------------------------- voice

def tts(scenes, voice, speed):
    from kokoro_onnx import Kokoro
    k = Kokoro(str(CACHE / "kokoro.onnx"), str(CACHE / "voices.bin"))
    clips = []
    for s in scenes:
        a, sr = k.create(s["say"], voice=voice, speed=speed, lang="en-us")
        assert sr == SR
        a = np.asarray(a, dtype=np.float32)
        nz = np.where(np.abs(a) > 0.01)[0]          # trim edge silence
        if len(nz):
            a = a[max(0, nz[0] - 600): nz[-1] + 1800]
        clips.append(a)
    return clips

# ---------------------------------------------------------------- timeline + captions

def words_of(text):
    return re.findall(r"\S+", text)

def build_timeline(scenes, clips):
    t = LEAD
    caps = []
    for s, c in zip(scenes, clips):
        d = len(c) / SR
        s["_start"] = round(t if s is not scenes[0] else 0.0, 3)
        s["_vo"] = round(t, 3)
        s["_vodur"] = round(d, 3)
        ws = words_of(s["say"])
        wt = [len(re.sub(r"[^\w$%]", "", w)) + 2.2 + (2.5 if re.search(r"[.,!?;:]$", w) else 0) for w in ws]
        tot = sum(wt) or 1
        cur, chunk, acc = t, [], 0
        for i, (w, x) in enumerate(zip(ws, wt)):
            dur = d * x / tot
            chunk.append({"w": w, "t0": round(cur, 3), "t1": round(cur + dur, 3)})
            cur += dur
            end_here = len(chunk) >= 3 or re.search(r"[.,!?;:]$", w) or i == len(ws) - 1
            if end_here:
                caps.append({"t0": chunk[0]["t0"], "t1": chunk[-1]["t1"], "words": chunk})
                chunk = []
        t += d + GAP
    for i, s in enumerate(scenes):
        nxt = scenes[i + 1]["_vo"] - 0.12 if i + 1 < len(scenes) else t + 0.6
        s["_start"] = 0.0 if i == 0 else round(s["_vo"] - 0.12, 3)
        s["_dur"] = round(nxt - s["_start"], 3)
    total = round(t + 0.6, 3)
    for j in range(len(caps) - 1):   # hold each caption until the next starts (no flicker)
        caps[j]["t1"] = min(caps[j + 1]["t0"], caps[j]["t1"] + 0.6)
    return caps, total

# ---------------------------------------------------------------- audio

def tone_bed(total, seed):
    """Quiet generated music bed: warm minor pads + soft pulse. Replaced by engine/music/*.mp3 when present."""
    rng = np.random.default_rng(seed)
    n = int(total * SR); t = np.arange(n) / SR
    bpm = 96; beat = 60 / bpm; bar = beat * 4
    roots = [57, 53, 48, 55]            # A, F, C, G
    out = np.zeros(n, np.float32)
    for bi in range(int(total / bar) + 1):
        r = roots[bi % 4]
        s0 = int(bi * bar * SR); s1 = min(n, int((bi + 1) * bar * SR) + int(0.4 * SR))
        if s0 >= n: break
        tt = t[s0:s1] - bi * bar
        env = np.minimum(1, tt / 0.6) * np.exp(-np.maximum(0, tt - bar) * 4)
        for semi in (0, 3 if bi % 4 == 0 else 4, 7, 12):
            f = 440 * 2 ** ((r + semi - 69) / 12)
            v = np.sin(2 * np.pi * f * tt) + 0.5 * np.sin(2 * np.pi * f * 1.003 * tt) + 0.25 * np.sin(2 * np.pi * f * 2 * tt)
            out[s0:s1] += (0.05 * env * v).astype(np.float32)
    # soft kick on every beat, light tick on the off beats
    k_len = int(0.25 * SR); kt = np.arange(k_len) / SR
    kick = np.sin(2 * np.pi * (45 * kt + 60 * (1 - np.exp(-kt * 30)) / 30)) * np.exp(-kt * 14) * 0.35
    tick = rng.standard_normal(int(0.03 * SR)) * np.exp(-np.arange(int(0.03 * SR)) / SR * 160) * 0.025
    nb = int(total / beat)
    for b in range(nb):
        s = int(b * beat * SR)
        out[s:s + k_len] += kick[: n - s].astype(np.float32)
        so = int((b + 0.5) * beat * SR)
        if so < n: out[so:so + len(tick)] += tick[: n - so].astype(np.float32)
    # one-pole low pass for warmth
    a = 0.18; y = np.zeros_like(out); acc = 0.0
    for i in range(0, n, 1):
        acc += a * (out[i] - acc); y[i] = acc
    fade = int(1.5 * SR); y[-fade:] *= np.linspace(1, 0, fade)
    return y / (np.max(np.abs(y)) + 1e-9) * 0.5

def whoosh():
    n = int(0.45 * SR); t = np.arange(n) / SR
    noise = np.random.default_rng(7).standard_normal(n)
    env = np.sin(np.pi * np.clip(t / 0.45, 0, 1)) ** 2
    y = np.zeros(n); acc = 0.0
    for i in range(n):
        a = 0.02 + 0.25 * (t[i] / 0.45)
        acc += a * (noise[i] - acc); y[i] = acc
    return (y * env / (np.max(np.abs(y)) + 1e-9) * 0.28).astype(np.float32)

def write_wav(path, a):
    a = np.clip(a, -1, 1)
    with wave.open(str(path), "wb") as w:
        w.setnchannels(1); w.setsampwidth(2); w.setframerate(SR)
        w.writeframes((a * 32767).astype("<i2").tobytes())

def build_audio(scenes, clips, total, out, seed):
    n = int(total * SR) + SR
    vo = np.zeros(n, np.float32); sfx = np.zeros(n, np.float32)
    for s, c in zip(scenes, clips):
        i = int(s["_vo"] * SR); vo[i:i + len(c)] += c
    pick = [i for i, s in enumerate(scenes) if i and s["kind"] in ("article", "stat", "logos", "cta")][:MAX_SFX]
    w = whoosh()
    for i in pick:
        st = max(0, int((scenes[i]["_start"] - 0.18) * SR)); sfx[st:st + len(w)] += w[: n - st]
    write_wav(out / "vo.wav", vo); write_wav(out / "sfx.wav", sfx)
    music = sorted((HERE / "music").glob("*.mp3")) + sorted((HERE / "music").glob("*.wav"))
    if music:
        src = music[seed % len(music)]
        subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-stream_loop", "-1", "-i", str(src), "-t", f"{total + 1:.2f}",
                        "-af", f"afade=t=out:st={max(0, total - 1.5):.2f}:d=1.5", "-ac", "1", "-ar", str(SR), str(out / "music.wav")], check=True)
    else:
        write_wav(out / "music.wav", tone_bed(total + 1, seed))
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "vo.wav"), "-i", str(out / "music.wav"), "-i", str(out / "sfx.wav"),
                    "-filter_complex",
                    "[0:a]asplit=2[v][sc];[1:a]volume=0.55[mb];[mb][sc]sidechaincompress=threshold=0.02:ratio=10:attack=15:release=350[m];"
                    "[v][m][2:a]amix=inputs=3:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                    "-map", "[a]", "-t", f"{total:.2f}", "-ar", "48000", "-ac", "2", str(out / "mix.wav")], check=True)
    for f in ("vo.wav", "sfx.wav", "music.wav"):
        (out / f).unlink(missing_ok=True)

# ---------------------------------------------------------------- html

CSS = """
@font-face{font-family:'Bricolage';src:url(data:font/woff2;base64,%FONT%) format('woff2');font-weight:200 800;font-stretch:75% 100%}
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1920px;overflow:hidden;background:#000}
body{font-family:'Bricolage',sans-serif;-webkit-font-smoothing:antialiased}
.L{position:absolute;inset:0;overflow:hidden}
.dark{background:#000;color:#fff}.light{background:#fff;color:#000}
.grid{position:absolute;inset:-200px;background-image:linear-gradient(currentColor 2px,transparent 2px),linear-gradient(90deg,currentColor 2px,transparent 2px);
 background-size:120px 120px;opacity:.06}
.content{position:absolute;left:80px;right:140px;top:380px;height:880px;display:flex;flex-direction:column;justify-content:center}
.hlogo{width:190px;height:190px;border-radius:44px;background:#fff;display:flex;align-items:center;justify-content:center;margin-bottom:56px}
.hlogo svg{width:112px;height:112px}
.kicker{font-size:32px;font-weight:700;letter-spacing:.14em;text-transform:uppercase;margin-bottom:40px;opacity:.85}
h1,h2{font-weight:800;font-stretch:78%;line-height:.95;letter-spacing:-.02em}
h1{font-size:138px}h2{font-size:116px}
.w{display:inline-block;position:relative;margin-right:.18em}
.w .t{position:relative;z-index:1}
.acc .hb{position:absolute;left:-.1em;right:-.1em;top:.06em;bottom:-.02em;border-radius:.08em;transform-origin:left center;z-index:0}
.dark .acc .hb{background:#fff}.dark .acc .t{color:#000}
.light .acc .hb{background:#000}.light .acc .t{color:#fff}
.chip{width:170px;height:170px;border-radius:40px;display:flex;align-items:center;justify-content:center;margin-bottom:56px}
.dark .chip{background:#fff;color:#000}.light .chip{background:#000;color:#fff}
.chip svg{width:96px;height:96px;fill:none;stroke:currentColor;stroke-width:2;stroke-linecap:round;stroke-linejoin:round}
.chip svg *{stroke-dasharray:1;stroke-dashoffset:1}
.body{font-size:46px;line-height:1.25;font-weight:400;margin-top:40px;max-width:820px;opacity:.9}
.card{background:#fff;color:#111;border-radius:30px;padding:56px 54px 60px;box-shadow:0 40px 90px rgba(0,0,0,.45)}
.card .pub{display:flex;align-items:center;gap:20px;font-size:38px;font-weight:800;font-stretch:85%}
.card .pub svg{width:52px;height:52px}
.card .date{margin-left:auto;font-size:28px;font-weight:500;color:#666;font-stretch:100%}
.card hr{border:0;height:3px;background:#111;margin:34px 0 38px;opacity:.9}
.card .hl{font-size:74px;font-weight:800;font-stretch:82%;line-height:1.04;letter-spacing:-.01em}
.mk{background:linear-gradient(#FFE14D,#FFE14D) no-repeat 0 92%/0% 48%;-webkit-box-decoration-break:clone;box-decoration-break:clone;padding:0 4px;margin:0 -4px}
.via{font-size:28px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;margin-top:44px;opacity:.75}
.tiles{display:flex;flex-wrap:wrap;gap:30px;margin-top:70px}
.tile{width:300px;height:300px;border-radius:36px;background:#fff;color:#111;display:flex;flex-direction:column;align-items:center;justify-content:center;gap:22px;box-shadow:0 0 0 3px #000 inset}
.tile svg{width:130px;height:130px}
.tile .nm{font-size:32px;font-weight:800;font-stretch:85%;text-align:center;padding:0 14px;line-height:1}
.tile .wm{font-size:54px;font-weight:800;font-stretch:78%;text-align:center;line-height:.95;padding:0 16px}
.big{font-size:330px;font-weight:800;font-stretch:75%;line-height:.82;letter-spacing:-.04em}
.label{font-size:66px;font-weight:800;font-stretch:80%;line-height:1;margin-top:40px}
.src{font-size:28px;letter-spacing:.06em;opacity:.65;margin-top:36px}
.dm{display:inline-flex;align-self:flex-start;background:#fff;color:#000;font-size:84px;font-weight:800;font-stretch:80%;padding:26px 50px;border-radius:22px;margin-top:70px}
.alt{font-size:38px;font-weight:500;margin-top:36px}.alt b{font-weight:800}
.ctalogo{height:110px;margin-top:80px;align-self:flex-start}
#top{position:absolute;left:80px;right:140px;top:200px;display:flex;align-items:center;justify-content:space-between;z-index:50;mix-blend-mode:difference;color:#fff}
#top img{height:66px}
#top .pill{border:3px solid #fff;border-radius:999px;padding:10px 26px;font-size:26px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
#bar{position:absolute;left:80px;right:140px;top:300px;height:6px;z-index:50;mix-blend-mode:difference}
#bar i{display:block;height:100%;background:#fff;width:0;border-radius:3px}
#cap{position:absolute;left:60px;right:120px;top:1330px;z-index:60;display:flex;justify-content:center}
#cap .p{background:rgba(0,0,0,.9);border:2px solid rgba(255,255,255,.22);border-radius:26px;padding:16px 26px;color:#fff;
 font-size:60px;font-weight:800;font-stretch:82%;line-height:1.1;text-align:center;max-width:880px}
#cap .cw{display:inline-block;padding:2px 12px;border-radius:12px;margin:2px 0}
#cap .cw.on{background:#fff;color:#000}
"""

JS = r"""
const D = window.__D;
const cl = x => Math.max(0, Math.min(1, x));
const out = x => 1 - Math.pow(1 - x, 3);
const back = x => { const c = 1.70158, c3 = c + 1; return 1 + c3 * Math.pow(x - 1, 3) + c * Math.pow(x - 1, 2); };
const pr = (lt, a, d) => cl((lt - a) / d);
let lastCap = -1;
function render(t) {
  document.getElementById('bar').firstChild.style.width = (100 * cl(t / D.total)) + '%';
  D.scenes.forEach((s, i) => {
    const L = document.getElementById('L' + i), lt = t - s._start;
    const vis = lt >= 0 && lt < s._dur + 0.6;
    L.style.display = vis ? 'block' : 'none';
    if (!vis) return;
    const rv = i === 0 ? 1 : out(pr(lt, 0, 0.42));
    L.style.clipPath = `inset(${(1 - rv) * 100}% 0 0 0)`;
    const g = L.querySelector('.grid');
    if (g) g.style.transform = `translate(${-lt * 14}px, ${-lt * 22}px)`;
    L.querySelectorAll('[data-fx]').forEach(el => {
      const a = +el.dataset.a || 0, fx = el.dataset.fx;
      if (fx === 'rise') { const e = out(pr(lt, a, 0.5)); el.style.opacity = e; el.style.transform = `translateY(${(1 - e) * 70}px)`; }
      else if (fx === 'fade') { el.style.opacity = out(pr(lt, a, 0.5)); }
      else if (fx === 'pop') { const p = pr(lt, a, 0.55); el.style.opacity = cl(p * 3); el.style.transform = `scale(${0.55 + 0.45 * back(p)})`; }
      else if (fx === 'hb') { el.style.transform = `scaleX(${out(pr(lt, a, 0.38))})`; }
      else if (fx === 'draw') { const e = out(pr(lt, a, 1.0)); el.querySelectorAll('*').forEach(p => p.style.strokeDashoffset = 1 - e); }
      else if (fx === 'card') { const e = out(pr(lt, a, 0.7)); el.style.opacity = cl(e * 2); el.style.transform = `translateY(${(1 - e) * 420}px) rotate(${-9 + 7 * e}deg) scale(${0.92 + 0.08 * e})`; }
      else if (fx === 'count') { const e = out(pr(lt, a, 1.1)); const to = +el.dataset.to, dec = +el.dataset.dec || 0;
        el.textContent = el.dataset.pre + (to * e).toFixed(dec) + el.dataset.suf; }
      else if (fx === 'pulse') { const e = out(pr(lt, a, 0.5)); const k = lt > a + 0.5 ? 1 + 0.035 * Math.sin((lt - a) * 5.5) : 0.6 + 0.4 * back(pr(lt, a, 0.5));
        el.style.opacity = cl(e * 2); el.style.transform = `scale(${k})`; }
      else if (fx === 'mark') { el.style.backgroundSize = `${100 * out(pr(lt, a, 0.6))}% 48%`; }
      else if (fx === 'drift') { el.style.transform = `scale(${1 + 0.035 * cl(lt / s._dur)})`; }
    });
  });
  let ci = -1;
  for (let j = 0; j < D.caps.length; j++) if (t >= D.caps[j].t0 - 0.05 && t < D.caps[j].t1) { ci = j; break; }
  const cap = document.getElementById('cap');
  if (ci !== lastCap) {
    lastCap = ci;
    cap.innerHTML = ci < 0 ? '' : '<div class="p">' + D.caps[ci].words.map(w => `<span class="cw">${w.w.replace(/</g, '&lt;')}</span>`).join(' ') + '</div>';
  }
  if (ci >= 0) cap.querySelectorAll('.cw').forEach((el, k) => {
    const w = D.caps[ci].words[k]; el.classList.toggle('on', t >= w.t0 && t < w.t1 + 0.04);
  });
}
window.render = render;
"""

def e(x):
    return html.escape(str(x or ""))

def words_html(text, accent, delay0=0.15, step=0.07):
    acc = [a.lower() for a in words_of(accent or "")]
    out, d = [], delay0
    toks = words_of(text)
    # mark the accent as a contiguous run of words
    lo = [re.sub(r"[^\w$%+'’-]", "", t).lower() for t in toks]
    clean_acc = [re.sub(r"[^\w$%+'’-]", "", a) for a in acc]
    start = -1
    if clean_acc:
        for i in range(len(lo) - len(clean_acc) + 1):
            if lo[i:i + len(clean_acc)] == clean_acc:
                start = i; break
    for i, tk in enumerate(toks):
        is_acc = start >= 0 and start <= i < start + len(clean_acc)
        hb = f'<span class="hb" data-fx="hb" data-a="{d + 0.35:.2f}" style="transform:scaleX(0)"></span>' if is_acc else ""
        out.append(f'<span class="w{" acc" if is_acc else ""}" data-fx="rise" data-a="{d:.2f}" style="opacity:0">{hb}<span class="t">{e(tk)}</span></span>')
        d += step
    return "".join(out), d

def marker_html(text, accent):
    if accent and accent.lower() in text.lower():
        i = text.lower().index(accent.lower())
        a, b, c = text[:i], text[i:i + len(accent)], text[i + len(accent):]
        return f'{e(a)}<span class="mk" data-fx="mark" data-a="1.05">{e(b)}</span>{e(c)}'
    return e(text)

def brand_svg(slug, size_cls=""):
    b = brand(slug)
    if not b:
        return None
    d, hexc = b
    return f'<svg viewBox="0 0 24 24" class="{size_cls}"><path d="{d}" fill="#{hexc}"/></svg>'

def scene_html(s, i, n, logos):
    k = s["kind"]
    theme = s.get("theme") or {"hook": "dark", "article": "dark", "stat": "dark", "cta": "dark", "logos": "light"}.get(k) \
        or ("light" if s.get("_pi", 0) % 2 == 1 else "dark")
    s["_theme"] = theme
    grid = '<div class="grid" ></div>'
    if k == "hook":
        t, _ = words_html(s["title"], s.get("accent"), 0.25)
        lg = brand_svg(s.get("logo")) if s.get("logo") else None
        tile = f'<div class="hlogo" data-fx="pop" data-a="0.0" style="opacity:0">{lg}</div>' if lg else ""
        inner = f'{tile}<div class="kicker" data-fx="fade" data-a="0.05" style="opacity:0">{e(s.get("kicker"))}</div><h1>{t}</h1>'
    elif k == "point":
        t, d = words_html(s["title"], s.get("accent"), 0.45)
        body = f'<p class="body" data-fx="rise" data-a="{d + 0.25:.2f}" style="opacity:0">{e(s["body"])}</p>' if s.get("body") else ""
        inner = f'<div class="chip" data-fx="pop" data-a="0.12" style="opacity:0"><svg viewBox="0 0 24 24" data-fx="draw" data-a="0.3">{icon(s.get("icon", "sparkles"))}</svg></div><h2>{t}</h2>{body}'
    elif k == "article":
        lg = brand_svg(s.get("logo") or s.get("publication")) or ""
        inner = f'''<div class="card" data-fx="card" data-a="0.05" style="opacity:0">
<div class="pub">{lg}<span>{e(s["publication"])}</span><span class="date">{e(s.get("date"))}</span></div><hr>
<div class="hl">{marker_html(s["headline"], s.get("accent"))}</div></div>
<div class="via" data-fx="fade" data-a="0.9" style="opacity:0">Source: {e(s["publication"])}</div>'''
    elif k == "logos":
        t, d = words_html(s["title"], s.get("accent"), 0.15)
        tiles = []
        for j, name in enumerate(s["logos"][:4]):
            sv = brand_svg(name)
            label = e(s.get("names", {}).get(name, name.replace("-", " ").title()))
            body = f'{sv}<span class="nm">{label}</span>' if sv else f'<span class="wm">{label}</span>'
            tiles.append(f'<div class="tile" data-fx="pop" data-a="{d + 0.2 + 0.18 * j:.2f}" style="opacity:0">{body}</div>')
        inner = f'<h2 style="font-size:104px">{t}</h2><div class="tiles">{"".join(tiles)}</div>'
    elif k == "stat":
        m = re.match(r"^([^\d]*)([\d.,]+)(.*)$", s["big"])
        if m:
            pre, num, suf = m.groups(); to = float(num.replace(",", "")); dec = len(num.split(".")[1]) if "." in num else 0
            big = f'<div class="big" data-fx="count" data-a="0.2" data-to="{to}" data-dec="{dec}" data-pre="{e(pre)}" data-suf="{e(suf)}">{e(pre)}0{e(suf)}</div>'
        else:
            big = f'<div class="big" data-fx="pop" data-a="0.2" style="opacity:0">{e(s["big"])}</div>'
        src = f'<div class="src" data-fx="fade" data-a="1.2" style="opacity:0">Source: {e(s["source"])}</div>' if s.get("source") else ""
        inner = f'{big}<div class="label" data-fx="rise" data-a="0.6" style="opacity:0">{e(s["label"])}</div>{src}'
    elif k == "cta":
        t, d = words_html(s["title"], s.get("accent"), 0.15, 0.06)
        inner = f'''<h2 style="font-size:96px">{t}</h2>
<div class="dm" data-fx="pulse" data-a="{d + 0.2:.2f}" style="opacity:0">DM &ldquo;{e(s["keyword"].upper())}&rdquo;</div>
<div class="alt" data-fx="fade" data-a="{d + 0.6:.2f}" style="opacity:0">or tap the link in bio: <b>blackwellgraphics.com</b></div>
<img class="ctalogo" data-fx="fade" data-a="{d + 0.9:.2f}" style="opacity:0" src="data:image/png;base64,{logos['white']}">'''
    else:
        raise SystemExit(f"unknown scene kind {k}")
    return f'<div class="L {theme}" id="L{i}" style="display:none;z-index:{i + 1}">{grid}<div class="content" data-fx="drift">{inner}</div></div>'

def page(spec, scenes, caps, total):
    logos = logo_variants()
    font = b64(HERE / "fonts/bricolage.woff2")
    pi = 0
    for s in scenes:
        if s["kind"] == "point":
            s["_pi"] = pi; pi += 1
    layers = "".join(scene_html(s, i, len(scenes), logos) for i, s in enumerate(scenes))
    data = {"total": total, "caps": caps,
            "scenes": [{"_start": s["_start"], "_dur": s["_dur"]} for s in scenes]}
    return f"""<!doctype html><html><head><meta charset="utf-8"><style>{CSS.replace('%FONT%', font)}</style></head><body>
{layers}
<div id="top"><img src="data:image/png;base64,{logos['white']}"><span class="pill">{e(spec['tag'])}</span></div>
<div id="bar"><i></i></div><div id="cap"></div>
<script>window.__D={json.dumps(data)};</script><script>{JS}</script></body></html>"""

# ---------------------------------------------------------------- checks

def validate(spec):
    sc = spec["scenes"]
    errs = []
    if not 5 <= len(sc) <= 9: errs.append("use 5 to 9 scenes")
    if sc[0]["kind"] != "hook": errs.append("first scene must be hook")
    if sc[-1]["kind"] != "cta": errs.append("last scene must be cta")
    if not any(s["kind"] == "article" for s in sc): errs.append("include at least one article scene")
    for i, s in enumerate(sc, 1):
        for f, lim in LIMITS.get(s["kind"], {}).items():
            if len(s.get(f) or "") > lim: errs.append(f"scene {i} {f} is {len(s[f])} chars, max {lim}")
        if not s.get("say"): errs.append(f"scene {i} has no narration")
        if "—" in json.dumps(s, ensure_ascii=False) or "–" in json.dumps(s, ensure_ascii=False):
            errs.append(f"scene {i} contains a dash character, remove it")
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
        raise SystemExit(f"reel runs {total:.1f}s; narration must land between {MIN_S:.0f} and {MAX_S:.0f}s. "
                         f"{'Add' if total < MIN_S else 'Cut'} about {abs((MIN_S + 2 if total < MIN_S else MAX_S - 2) - total) * 2.6:.0f} words and re-render.")
    seed = sum(map(ord, out.name))
    build_audio(scenes, clips, total, out, seed)
    doc = page(spec, scenes, caps, total)
    (out / "reel.html").write_text(doc)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H})
        pg.set_content(doc); pg.evaluate("document.fonts.ready"); pg.wait_for_timeout(500)
        # overflow guard: every scene's content must fit its box at full state
        over = []
        for i, s in enumerate(scenes):
            pg.evaluate(f"render({s['_start'] + s['_dur'] - 0.05})")
            if pg.evaluate(f"(()=>{{const c=document.querySelector('#L{i} .content');c.style.transform='none';"
                           f"c.querySelectorAll('[data-fx]').forEach(x=>x.style.transform='none');"
                           f"return c.scrollHeight>c.clientHeight+4||c.scrollWidth>c.clientWidth+4}})()"):
                over.append(i + 1)
        if over:
            raise SystemExit(f"scene(s) {over} overflow the frame, shorten that copy and re-render")
        # QC contact sheet: each scene at its settled state
        from PIL import Image
        shots = []
        for s in scenes:
            pg.evaluate(f"render({s['_start'] + min(s['_dur'] - 0.1, 2.6)})")
            shots.append(Image.open(io.BytesIO(pg.screenshot(type="jpeg", quality=80))))
        cw, ch = 270, 480
        cols = min(5, len(shots)); rows = math.ceil(len(shots) / cols)
        sheet = Image.new("RGB", (cols * (cw + 10) + 10, rows * (ch + 10) + 10), "#777")
        for j, im in enumerate(shots):
            sheet.paste(im.resize((cw, ch)), (10 + (j % cols) * (cw + 10), 10 + (j // cols) * (ch + 10)))
        sheet.save(out / "sheet.jpg", quality=85)
        pg.evaluate("render(1.6)")
        pg.screenshot(path=str(out / "cover.jpg"), type="jpeg", quality=92)
        if not preview:
            nframes = int(math.ceil(total * FPS))
            ff = subprocess.Popen(["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS), "-c:v", "mjpeg", "-i", "-",
                                   "-i", str(out / "mix.wav"), "-map", "0:v", "-map", "1:a",
                                   "-c:v", "libx264", "-preset", "veryfast", "-crf", "21", "-pix_fmt", "yuv420p", "-r", str(FPS),
                                   "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out / "reel.mp4")],
                                  stdin=subprocess.PIPE)
            for f in range(nframes):
                pg.evaluate(f"render({f / FPS})")
                ff.stdin.write(pg.screenshot(type="jpeg", quality=88))
            ff.stdin.close(); ff.wait()
            if ff.returncode: raise SystemExit("ffmpeg failed")
        b.close()
    srt = []
    fmt = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x % 1 * 1000):03d}"
    for j, c in enumerate(caps, 1):
        srt.append(f"{j}\n{fmt(c['t0'])} --> {fmt(c['t1'])}\n{' '.join(w['w'] for w in c['words'])}\n")
    (out / "captions.srt").write_text("\n".join(srt))
    (out / "reel.html").unlink(missing_ok=True)
    (out / "mix.wav").unlink(missing_ok=True) if not preview else None
    mb = (out / "reel.mp4").stat().st_size / 1e6 if (out / "reel.mp4").exists() else 0
    print(json.dumps({"seconds": round(total, 1), "scenes": len(scenes), "mp4_mb": round(mb, 1),
                      "files": sorted(x.name for x in out.iterdir())}))

if __name__ == "__main__":
    main()
