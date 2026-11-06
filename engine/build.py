#!/usr/bin/env python3
"""
Rocky's renderer: progressive-construction explainers.

Different animal from reel.py. reel.py shows a sequence of cards. This shows ONE
object that gets built on screen, part by part, where every part is the answer to
a problem the previous part created. Nothing ever leaves the stage.

    python3 engine/build.py specs/limo.json out/limo [--preview]

--preview skips the frame render and MP4, so you can check timing and captions fast.

Spec shape:
{
  "subject": "limo",
  "narrator": "am_onyx",          optional, default am_onyx
  "speed": 1.0,                    optional
  "theme": {"bg": "#000000", "ink": "#FFFFFF", "accent": "#83FA0C"},
  "viewBox": "0 0 1000 600",
  "beats": [
    {"kind": "hook",    "say": "...", "svg": "<path .../>"},
    {"kind": "break",   "say": "..."},
    {"kind": "fix",     "say": "...", "svg": "...", "label": "stretched frame", "labelAt": [500, 430]},
    {"kind": "problem", "say": "..."},
    {"kind": "payoff",  "say": "..."},
    {"kind": "close",   "say": "...", "banner": "YOU JUST BUILT A LIMO"}
  ]
}

Rocky authors the SVG per video. This file never guesses the artwork, it only owns
timing, narration, labels, captions and the encode. Every part added by a "fix" beat
stays visible for the rest of the film.
"""
import io, json, math, pathlib, subprocess, sys, wave

import numpy as np

HERE = pathlib.Path(__file__).resolve().parent
CACHE = HERE / ".cache"
W, H, FPS = 1080, 1920, 30
SR = 24000
MIN_S, MAX_S = 40.0, 50.0
GAP = 0.20                      # breath after each beat
NARRATOR = "am_onyx"            # deep male, Kokoro
SPEED = 1.0
BUILD_KINDS = {"hook", "fix"}   # beats that may add a part to the stage
SHAKE_KINDS = {"break", "problem"}


# ---------------------------------------------------------------- audio

def write_wav(path, x):
    x = np.clip(x, -1, 1)
    with wave.open(str(path), "wb") as f:
        f.setnchannels(1); f.setsampwidth(2); f.setframerate(SR)
        f.writeframes((x * 32767).astype("<i2").tobytes())


def tts(beats, voice, speed):
    from kokoro_onnx import Kokoro
    k = Kokoro(str(CACHE / "kokoro.onnx"), str(CACHE / "voices.bin"))
    clips = []
    for b in beats:
        a, sr = k.create(b["say"], voice=voice, speed=speed, lang="en-us")
        if sr != SR:
            n = int(len(a) * SR / sr)
            a = np.interp(np.linspace(0, len(a) - 1, n), np.arange(len(a)), a)
        clips.append(np.asarray(a, np.float32))
    return clips


def timeline(beats, clips):
    """Each beat starts when the one before it has finished speaking, plus a breath."""
    t, caps = 0.0, []
    for i, (b, c) in enumerate(zip(beats, clips)):
        dur = len(c) / SR
        b["_start"], b["_vo"], b["_dur"], b["_end"] = t, t, dur + GAP, t + dur + GAP
        words = b["say"].split()
        span = sum(len(w) for w in words) or 1
        wt = t
        for w in words:
            d = dur * len(w) / span
            caps.append({"w": w, "t0": wt, "t1": wt + d, "b": i})
            wt += d
        t += dur + GAP
    return caps, t


def mix(beats, clips, total, out):
    n = int(total * SR) + SR
    vo = np.zeros(n, np.float32)
    for b, c in zip(beats, clips):
        i = int(b["_vo"] * SR)
        vo[i:i + len(c)] += c[: max(0, n - i)]
    write_wav(out / "vo.wav", vo)
    bed = tone_bed(total + 1)
    write_wav(out / "bed.wav", bed)
    subprocess.run(["ffmpeg", "-loglevel", "error", "-y", "-i", str(out / "vo.wav"), "-i", str(out / "bed.wav"),
                    "-filter_complex",
                    "[0:a]asplit=2[v][sc];[1:a]volume=0.5[b];[b][sc]"
                    "sidechaincompress=threshold=0.02:ratio=8:attack=15:release=300[m];"
                    "[v][m]amix=inputs=2:normalize=0,loudnorm=I=-14:TP=-1.5:LRA=11[a]",
                    "-map", "[a]", "-ac", "1", "-ar", str(SR), str(out / "mix.wav")], check=True)


def tone_bed(secs):
    """Low pulsing bed so the film is never silent. Deterministic, no music files needed."""
    t = np.arange(int(secs * SR)) / SR
    bed = (0.05 * np.sin(2 * np.pi * 55 * t) + 0.03 * np.sin(2 * np.pi * 82.5 * t)).astype(np.float32)
    pulse = 0.6 + 0.4 * np.sin(2 * np.pi * t / 2.0)
    bed *= pulse.astype(np.float32)
    fade = np.minimum(1, np.minimum(t / 1.5, (secs - t) / 1.5)).clip(0, 1).astype(np.float32)
    return bed * fade


# ---------------------------------------------------------------- page

def page(spec, beats, caps, total):
    th = {"bg": "#000000", "ink": "#FFFFFF", "accent": "#83FA0C"}
    th.update(spec.get("theme") or {})
    font = (HERE / "fonts/bricolage.woff2")
    face = ""
    if font.exists():
        import base64
        b64 = base64.b64encode(font.read_bytes()).decode()
        face = ("@font-face{font-family:Bric;src:url(data:font/woff2;base64,%s) format('woff2');"
                "font-weight:200 800;font-display:block}" % b64)

    layers, labels = [], []
    for i, b in enumerate(beats):
        if b.get("svg") and b["kind"] in BUILD_KINDS:
            layers.append({"i": i, "t": b["_start"], "svg": b["svg"]})
        if b.get("label"):
            x, y = (b.get("labelAt") or [500, 540])
            # the name appears just after the part lands and retires when the beat ends,
            # so only one term is ever on screen at a time
            labels.append({"i": i, "t": b["_start"] + 0.5, "out": b["_end"] + 0.35,
                           "text": b["label"].upper(), "x": x, "y": y})

    shakes = [{"t": b["_start"], "d": min(0.6, b["_dur"])} for b in beats if b["kind"] in SHAKE_KINDS]
    banner = next((b.get("banner") for b in beats if b.get("banner")), None)
    bslot = next((b for b in beats if b.get("banner")), None)

    data = {"total": total, "caps": caps, "layers": layers, "labels": labels, "shakes": shakes,
            "banner": banner, "bannerT": (bslot["_start"] + 0.2) if bslot else None,
            "viewBox": spec.get("viewBox", "0 0 1000 600"), "theme": th}

    return """<!doctype html><meta charset=utf-8><style>
%s
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:%dpx;height:%dpx;background:%s;overflow:hidden}
body{font-family:Bric,system-ui,sans-serif;color:%s}
#stage{position:absolute;left:0;right:0;top:0;bottom:620px;display:flex;align-items:center;justify-content:center}
#wrap{width:100%%;transform-origin:50%% 50%%}
svg{width:100%%;height:auto;overflow:visible}
.part{opacity:0}
.lab{position:absolute;transform:translate(-50%%,-50%%) scale(.8);opacity:0;
 background:%s;color:#000;font-weight:800;letter-spacing:.02em;
 padding:16px 30px;border-radius:999px;font-size:46px;white-space:nowrap;
 box-shadow:0 0 0 10px #000}
#cap{position:absolute;left:56px;right:56px;bottom:300px;text-align:center;
 font-size:72px;line-height:1.16;font-weight:800;letter-spacing:-.01em;text-wrap:balance}
#cap b{font-weight:800;color:%s}
#ban{position:absolute;left:0;right:0;top:50%%;transform:translateY(-50%%) scale(.9);opacity:0;
 text-align:center;font-size:104px;font-weight:800;line-height:1.05;padding:0 70px;color:%s}
</style>
<div id=stage><div id=wrap></div></div>
<div id=cap></div><div id=ban></div>
<script>
const D=%s;
const wrap=document.getElementById('wrap'), stage=document.getElementById('stage');
const cap=document.getElementById('cap'), ban=document.getElementById('ban');
const svgns='http://www.w3.org/2000/svg';
const svg=document.createElementNS(svgns,'svg');
svg.setAttribute('viewBox',D.viewBox);
wrap.appendChild(svg);
const parts=D.layers.map(L=>{
  const g=document.createElementNS(svgns,'g');
  g.setAttribute('class','part'); g.innerHTML=L.svg; svg.appendChild(g);
  const strokes=[...g.querySelectorAll('path,line,polyline,rect,circle,ellipse,polygon')];
  strokes.forEach(s=>{ try{ const len=s.getTotalLength?s.getTotalLength():0;
    if(len){ s.dataset.len=len; s.style.strokeDasharray=len; } }catch(e){} });
  return {L,g,strokes};
});
// labelAt is in viewBox coordinates, so map it through the svg's real box on screen
const vb=D.viewBox.split(/[ ,]+/).map(Number);
const box=svg.getBoundingClientRect(), sb=stage.getBoundingClientRect();
const labs=D.labels.map(L=>{
  const d=document.createElement('div'); d.className='lab'; d.textContent=L.text;
  d.style.left=(box.left-sb.left+(L.x-vb[0])/vb[2]*box.width)+'px';
  d.style.top =(box.top -sb.top +(L.y-vb[1])/vb[3]*box.height)+'px';
  stage.appendChild(d); return {L,d};
});
const ease=t=>t<0?0:t>1?1:1-Math.pow(1-t,3);
const clamp=(a,b,v)=>Math.max(a,Math.min(b,v));

function render(t){
  // parts: draw in over 0.7s, then stay for the rest of the film
  for(const {L,g,strokes} of parts){
    const p=ease((t-L.t)/0.7);
    g.style.opacity = t>=L.t ? 1 : 0;
    const sc = 0.96+0.04*p;
    g.style.transform='scale('+sc+')'; g.style.transformOrigin='50%% 50%%';
    for(const s of strokes){ if(s.dataset.len) s.style.strokeDashoffset=(1-p)*s.dataset.len; }
  }
  // labels: pop in when the part lands, retire when the beat ends
  for(const {L,d} of labs){
    const p=ease((t-L.t)/0.45), q=ease((t-L.out)/0.3);
    d.style.opacity=p*(1-q);
    d.style.transform='translate(-50%%,-50%%) scale('+(0.8+0.2*p)+')';
  }
  // shake on the beats where the thing breaks
  let dx=0,dy=0,rot=0;
  for(const s of D.shakes){
    const u=(t-s.t)/s.d;
    if(u>=0&&u<=1){ const a=(1-u)*14;
      dx+=Math.sin(t*74)*a; dy+=Math.sin(t*61)*a*0.6; rot+=Math.sin(t*53)*a*0.12; }
  }
  wrap.style.transform='translate('+dx+'px,'+dy+'px) rotate('+rot+'deg)';
  // captions: the whole sentence being spoken, with the live word lit
  let bi=-1;
  for(const c of D.caps){ if(t>=c.t0) bi=c.b; }
  if(bi<0) bi=0;
  const line=D.caps.filter(c=>c.b===bi);
  cap.innerHTML=line.map(c=>(t>=c.t0&&t<=c.t1)?'<b>'+c.w+'</b>':c.w).join(' ');
  // closing banner takes the frame to itself
  if(D.bannerT!==null&&D.banner){
    const p=ease((t-D.bannerT)/0.5);
    ban.style.opacity=p; ban.textContent=D.banner;
    ban.style.transform='translateY(-50%%) scale('+(0.9+0.1*p)+')';
    cap.style.opacity=1-p;
    stage.style.opacity=1-p;
  }
}
window.render=render; window.prep=render; render(0);
</script>""" % (face, W, H, th["bg"], th["ink"], th["accent"], th["accent"], th["accent"],
                json.dumps(data, ensure_ascii=False))


# ---------------------------------------------------------------- checks

def validate(spec):
    b, errs = spec.get("beats") or [], []
    if not b:
        errs.append("spec has no beats")
    else:
        if b[0]["kind"] != "hook":
            errs.append("first beat must be a hook")
        if b[-1]["kind"] != "close":
            errs.append("last beat must be a close")
        fixes = [x for x in b if x["kind"] == "fix"]
        if not 3 <= len(fixes) <= 5:
            errs.append(f"{len(fixes)} fix beats, use 3 to 5 (each one names a part)")
        for x in fixes:
            if not x.get("label"):
                errs.append("every fix beat needs a label naming the part it adds")
        for i, x in enumerate(b, 1):
            if not x.get("say"):
                errs.append(f"beat {i} has no narration")
            if any(d in json.dumps(x, ensure_ascii=False) for d in ("—", "–")):
                errs.append(f"beat {i} contains a dash character, remove it")
    if errs:
        raise SystemExit("spec problems:\n- " + "\n- ".join(errs))


def main():
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    preview = "--preview" in sys.argv
    spec = json.loads(pathlib.Path(args[0]).read_text())
    out = pathlib.Path(args[1]); out.mkdir(parents=True, exist_ok=True)
    validate(spec)
    beats = spec["beats"]

    clips = tts(beats, spec.get("narrator", NARRATOR), float(spec.get("speed", SPEED)))
    caps, total = timeline(beats, clips)
    if not MIN_S <= total <= MAX_S:
        short = total < MIN_S
        diff = (MIN_S + 2 - total) if short else (total - MAX_S + 2)
        raise SystemExit(f"film runs {total:.1f}s, it must land between {MIN_S:.0f} and {MAX_S:.0f}s. "
                         f"{'Add' if short else 'Cut'} about {diff * 3.0:.0f} words of narration and re-render.")

    work = out / "_work"; work.mkdir(exist_ok=True)
    (work / "page.html").write_text(page(spec, beats, caps, total))
    mix(beats, clips, total, out)

    from playwright.sync_api import sync_playwright
    with sync_playwright() as p:
        b = p.chromium.launch(channel="chrome")
        pg = b.new_page(viewport={"width": W, "height": H})
        pg.goto((work / "page.html").resolve().as_uri())
        pg.evaluate("document.fonts.ready"); pg.wait_for_timeout(400)
        pg.evaluate("render(0)")

        # contact sheet, one frame per beat, so Rocky can eyeball every stage
        from PIL import Image
        shots = []
        for x in beats:
            pg.evaluate(f"render({x['_start'] + x['_dur'] * 0.7})")
            shots.append(Image.open(io.BytesIO(pg.screenshot(type="jpeg", quality=80))))
        cw, ch = 270, 480
        cols = min(5, len(shots)); rows = math.ceil(len(shots) / cols)
        sheet = Image.new("RGB", (cols * (cw + 10) + 10, rows * (ch + 10) + 10), "#777")
        for j, im in enumerate(shots):
            sheet.paste(im.resize((cw, ch)), (10 + (j % cols) * (cw + 10), 10 + (j // cols) * (ch + 10)))
        sheet.save(out / "sheet.jpg", quality=85)

        pg.evaluate(f"render({min(1.3, beats[0]['_dur'] - 0.1)})")
        pg.screenshot(path=str(out / "cover.jpg"), type="jpeg", quality=92)

        if not preview:
            n = int(math.ceil(total * FPS))
            ff = subprocess.Popen(
                ["ffmpeg", "-loglevel", "error", "-y", "-f", "image2pipe", "-framerate", str(FPS),
                 "-c:v", "mjpeg", "-i", "-", "-i", str(out / "mix.wav"), "-map", "0:v", "-map", "1:a",
                 "-c:v", "libx264", "-preset", "veryfast", "-crf", "20", "-pix_fmt", "yuv420p", "-r", str(FPS),
                 "-c:a", "aac", "-b:a", "192k", "-shortest", "-movflags", "+faststart", str(out / "short.mp4")],
                stdin=subprocess.PIPE)
            for f in range(n):
                pg.evaluate(f"render({f / FPS})")
                ff.stdin.write(pg.screenshot(type="jpeg", quality=88))
            ff.stdin.close(); ff.wait()
            if ff.returncode:
                raise SystemExit("ffmpeg failed")
        b.close()

    fmt = lambda x: f"{int(x // 3600):02d}:{int(x % 3600 // 60):02d}:{int(x % 60):02d},{int(x % 1 * 1000):03d}"
    lines, j, i = [], 1, 0
    while i < len(caps):
        grp = caps[i:i + 6]
        lines.append(f"{j}\n{fmt(grp[0]['t0'])} --> {fmt(grp[-1]['t1'])}\n{' '.join(c['w'] for c in grp)}\n")
        i += 6; j += 1
    (out / "captions.srt").write_text("\n".join(lines))

    for tmp in ("vo.wav", "bed.wav"):
        (out / tmp).unlink(missing_ok=True)
    if not preview:
        (out / "mix.wav").unlink(missing_ok=True)
    mb = (out / "short.mp4").stat().st_size / 1e6 if (out / "short.mp4").exists() else 0
    print(json.dumps({"seconds": round(total, 1), "beats": len(beats),
                      "parts": sum(1 for x in beats if x.get("svg") and x["kind"] in BUILD_KINDS),
                      "mp4_mb": round(mb, 1), "files": sorted(x.name for x in out.iterdir() if x.is_file())}))


if __name__ == "__main__":
    main()
