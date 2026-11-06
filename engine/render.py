#!/usr/bin/env python3
"""Bruce slide renderer. Usage: python3 render.py spec.json outdir
spec.json = {"tag": "...", "slides": [ {...}, ... ]}  (max 8 slides)
Slide kinds:
  cover: {"kind":"cover","kicker":"...","title":"...","sub":"..."}
  point: {"kind":"point","title":"...","body":"..."}
  stat:  {"kind":"stat","big":"73%","title":"...","body":"...","source":"..."}
  cta:   {"kind":"cta","title":"...","body":"...","keyword":"LOGO"}
Any slide can add "media": "engine/.cache/media/SLUG/p1_2.jpg" (a Pixabay photo or a video's .thumb.jpg,
or an article screenshot) and optional "focus": "50% 30%". Photos stay in full colour (Eddy, Nov 6); the slide design stays black and white.
Cover and cta show it full-bleed under a dark gradient; point and stat show it as a panel above the text.
Needs: ./fonts/bricolage.woff2 and ./assets/logo-black.png (white version is derived)
"""
import base64, html, io, json, sys, pathlib
from playwright.sync_api import sync_playwright

HERE = pathlib.Path(__file__).parent
W, H = 1080, 1350

def b64(p):
    return base64.b64encode((HERE / p).read_bytes()).decode()

FONT = b64("fonts/bricolage.woff2")
def _logos():
    import io
    from PIL import Image
    im = Image.open(HERE / "assets/logo-black.png").convert("RGBA")
    a = im.getchannel("A")
    out = {}
    for name, rgb in (("black", (0, 0, 0)), ("white", (255, 255, 255))):
        c = Image.new("RGBA", im.size, rgb + (0,)); c.putalpha(a)
        buf = io.BytesIO(); c.save(buf, "PNG"); out[name] = base64.b64encode(buf.getvalue()).decode()
    return out

LOGO = _logos()

CSS = """
@font-face{font-family:'Bricolage';src:url(data:font/woff2;base64,%FONT%) format('woff2');
 font-weight:200 800;font-stretch:75% 100%;}
*{margin:0;padding:0;box-sizing:border-box}
html,body{width:1080px;height:1350px;overflow:hidden}
body{font-family:'Bricolage',sans-serif;-webkit-font-smoothing:antialiased}
.s{position:relative;width:1080px;height:1350px;padding:84px 84px 76px;display:flex;flex-direction:column}
.dark{background:#000;color:#fff}.light{background:#fff;color:#000}
.top{display:flex;justify-content:space-between;align-items:center;font-size:24px;font-weight:600;
 letter-spacing:.14em;text-transform:uppercase}
.pill{border:2px solid currentColor;border-radius:999px;padding:10px 22px}
.count{font-variation-settings:'wdth' 100;opacity:.9}
.foot{margin-top:auto;display:flex;justify-content:space-between;align-items:flex-end}
.foot img{height:74px}
.handle{font-size:24px;font-weight:500;letter-spacing:.06em;opacity:.85}
h1,h2{font-weight:800;font-stretch:78%;line-height:.92;letter-spacing:-.02em}
.cover h1{font-size:132px;margin-top:auto}
.cover .kicker{font-size:30px;font-weight:600;letter-spacing:.12em;text-transform:uppercase;margin-top:110px}
.cover .sub{font-size:38px;line-height:1.25;font-weight:400;margin-top:44px;max-width:860px;opacity:.92}
.swipe{display:flex;align-items:center;gap:18px;font-size:26px;font-weight:700;letter-spacing:.14em;text-transform:uppercase}
.swipe .ln{width:120px;height:4px;background:currentColor}
.rule{height:8px;background:currentColor;width:140px;margin:56px 0 44px}
.num{font-size:300px;font-weight:800;font-stretch:75%;line-height:.8;color:transparent;
 -webkit-text-stroke:4px currentColor;margin-top:70px;letter-spacing:-.04em}
.light .num{-webkit-text-stroke-color:#000}.dark .num{-webkit-text-stroke-color:#fff}
.point h2{font-size:104px}
.blk{margin-top:auto;margin-bottom:96px}
.body{font-size:44px;line-height:1.28;font-weight:400;max-width:900px}
.stat .big{font-size:250px;font-weight:800;font-stretch:75%;line-height:.85;margin-top:90px;letter-spacing:-.04em}
.stat h2{font-size:80px}
.src{font-size:22px;letter-spacing:.04em;opacity:.7;margin-top:28px}
.cta h1{font-size:112px;margin-top:110px}
.cta .body{margin-top:44px}
.dm{margin-top:auto;margin-bottom:56px;display:flex;flex-direction:column;gap:22px}
.dm .k{display:inline-flex;align-self:flex-start;background:#fff;color:#000;font-size:64px;font-weight:800;
 font-stretch:80%;padding:22px 44px;border-radius:18px;letter-spacing:.02em}
.dm .alt{font-size:32px;font-weight:500}
.dm .alt b{font-weight:800}
.bg{position:absolute;inset:0;background-size:cover;background-position:center}
.bg::after{content:"";position:absolute;inset:0;background:linear-gradient(180deg,rgba(0,0,0,.55) 0%,rgba(0,0,0,.15) 28%,rgba(0,0,0,.55) 55%,rgba(0,0,0,.94) 100%)}
.hasbg>*:not(.bg){position:relative;z-index:1}
.hasbg{color:#fff}
.panel{margin-top:44px;height:560px;border-radius:28px;background-size:cover;background-position:center;position:relative;flex:none}
.light .panel{box-shadow:0 0 0 4px #000}.dark .panel{box-shadow:0 0 0 2px rgba(255,255,255,.25)}
.panel .pn{position:absolute;left:28px;bottom:24px;font-size:120px;font-weight:800;font-stretch:75%;line-height:.8;color:transparent;
 -webkit-text-stroke:3px #fff;text-shadow:0 6px 30px rgba(0,0,0,.4)}
.mpoint h2{font-size:84px}.mpoint .blk{margin-bottom:64px}
.mstat .panel{height:370px}.mstat .big{font-size:170px;margin-top:36px}.mstat h2{font-size:64px}.mstat .blk{margin-bottom:56px;margin-top:24px}
.hasbg.cover h1{margin-top:auto}
.hasbg.cta .bg::after{background:rgba(0,0,0,.8)}
"""

ROOT = HERE.parent

def photo(path, w, h):
    """Full-colour photo (Eddy, Nov 6 2026), resized, as a data URL."""
    from PIL import Image, ImageOps, ImageEnhance
    f = pathlib.Path(path)
    if not f.is_absolute():
        f = ROOT / path if (ROOT / path).exists() else pathlib.Path.cwd() / path
    if not f.exists():
        raise SystemExit(f"media not found: {path}")
    im = ImageOps.exif_transpose(Image.open(f)).convert("RGB")
    im.thumbnail((w * 2, h * 2 if h else 4000))
    buf = io.BytesIO(); im.convert("RGB").save(buf, "JPEG", quality=86)
    return "data:image/jpeg;base64," + base64.b64encode(buf.getvalue()).decode()

def e(t):
    return html.escape(t or "")

def frame(theme, tag, i, n, inner, cls, bg=""):
    logo = LOGO["white" if theme == "dark" or bg else "black"]
    bgd = f'<div class="bg" style="background-image:url({bg[0]});background-position:{bg[1]}"></div>' if bg else ""
    return f"""<div class="s {theme} {cls}{' hasbg' if bg else ''}">{bgd}
<div class="top"><span class="pill">{e(tag)}</span><span class="count">{i:02d} / {n:02d}</span></div>
{inner}
<div class="foot"><img src="data:image/png;base64,{logo}"><span class="handle">@blackwell_graphics</span></div>
</div>"""

def slide_html(s, i, n, tag):
    k = s["kind"]
    m = s.get("media"); pos = e(s.get("focus") or "50% 40%")
    full = (photo(m, W, H), pos) if m and k in ("cover", "cta") else ""
    panel = photo(m, W, 600) if m and k in ("point", "stat") else ""
    if k == "point" and panel:
        theme = "light" if i % 2 == 0 else "dark"
        inner = f"""<div class="panel" style="background-image:url({panel});background-position:{pos}"><span class="pn">{s['_n']:02d}</span></div>
<div class="blk"><h2>{e(s['title'])}</h2><p class="body" style="margin-top:30px">{e(s.get('body'))}</p></div>"""
        return frame(theme, tag, i, n, inner, "point mpoint")
    if k == "stat" and panel:
        theme = "light" if i % 2 == 0 else "dark"
        src = f'<div class="src">Source: {e(s["source"])}</div>' if s.get("source") else ""
        inner = f"""<div class="panel" style="background-image:url({panel});background-position:{pos}"></div><div class="big">{e(s['big'])}</div>
<div class="blk"><h2>{e(s['title'])}</h2><p class="body" style="margin-top:22px">{e(s.get('body'))}</p>{src}</div>"""
        return frame(theme, tag, i, n, inner, "stat mstat")
    if k == "cover":
        inner = f"""<div class="kicker">{e(s.get('kicker'))}</div><h1>{e(s['title'])}</h1>
<div class="sub">{e(s.get('sub'))}</div>
<div class="swipe" style="margin-top:56px"><span>Swipe</span><span class="ln"></span></div>"""
        return frame("dark", tag, i, n, inner, "cover", full)
    if k == "point":
        theme = "light" if i % 2 == 0 else "dark"
        inner = f"""<div class="num">{s['_n']:02d}</div><div class="blk"><div class="rule"></div>
<h2>{e(s['title'])}</h2><p class="body" style="margin-top:36px">{e(s.get('body'))}</p></div>"""
        return frame(theme, tag, i, n, inner, "point")
    if k == "stat":
        theme = "light" if i % 2 == 0 else "dark"
        src = f'<div class="src">Source: {e(s["source"])}</div>' if s.get("source") else ""
        inner = f"""<div class="big">{e(s['big'])}</div><div class="blk"><div class="rule"></div><h2>{e(s['title'])}</h2>
<p class="body" style="margin-top:28px">{e(s.get('body'))}</p>{src}</div>"""
        return frame(theme, tag, i, n, inner, "stat")
    if k == "cta":
        inner = f"""<h1>{e(s['title'])}</h1><p class="body">{e(s.get('body'))}</p>
<div class="dm"><span class="k">DM &ldquo;{e(s['keyword'].upper())}&rdquo;</span>
<span class="alt">or tap the link in bio: <b>blackwellgraphics.com</b></span></div>"""
        return frame("dark", tag, i, n, inner, "cta", full)
    raise ValueError(k)

def main(spec_path, out):
    spec = json.loads(pathlib.Path(spec_path).read_text())
    slides = spec["slides"]
    assert 2 <= len(slides) <= 8, "2 to 8 slides"
    assert slides[0]["kind"] == "cover" and slides[-1]["kind"] == "cta"
    out = pathlib.Path(out); out.mkdir(parents=True, exist_ok=True)
    css = CSS.replace("%FONT%", FONT)
    n = len(slides)
    c = 0
    for s in slides:
        if s["kind"] == "point":
            c += 1; s["_n"] = c
    with sync_playwright() as p:
        b = p.chromium.launch()
        pg = b.new_page(viewport={"width": W, "height": H})
        for i, s in enumerate(slides, 1):
            doc = f"<!doctype html><html><head><meta charset='utf-8'><style>{css}</style></head><body>{slide_html(s, i, n, spec['tag'])}</body></html>"
            pg.set_content(doc)
            pg.evaluate("document.fonts.ready")
            pg.wait_for_timeout(600)
            # overflow guard: fail loudly if text spills past the slide
            over = pg.evaluate("(()=>{const s=document.querySelector('.s'),f=s.querySelector('.foot');"
                               "return s.scrollHeight>1350||f.getBoundingClientRect().bottom>1350-60})()")
            if over:
                raise SystemExit(f"slide {i} overflows, shorten the copy")
            pg.screenshot(path=str(out / f"slide-{i:02d}.png"), clip={"x": 0, "y": 0, "width": W, "height": H})
        b.close()
    print(f"rendered {n} slides to {out}")

if __name__ == "__main__":
    main(sys.argv[1], sys.argv[2])
