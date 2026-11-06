#!/usr/bin/env python3
"""Runs inside GitHub Actions (full internet). Reads jobs/<slug>.json, downloads Pixabay
photo and video candidates and screenshots of source articles into out/<slug>/, and writes
out/<slug>/manifest.json. The workflow force-pushes out/ to the media-cache branch.

Job file:
{"slug": "2026-11-06-topic",
 "pixabay": [{"id": "p1", "type": "photo", "q": "black woman boutique owner", "n": 4},
             {"id": "v1", "type": "video", "q": "small business owner phone", "n": 3}],
 "shots":   [{"id": "s1", "url": "https://real-article-url"}]}
"""
import json, os, pathlib, subprocess, sys, time, urllib.parse, urllib.request

KEY = os.environ.get("PIXABAY_API_KEY", "").strip()
try:
    import imageio_ffmpeg
    FFMPEG = imageio_ffmpeg.get_ffmpeg_exe()
except Exception:
    FFMPEG = "ffmpeg"
UA = {"User-Agent": "bruce-media-fetcher/1.0 (+https://github.com/Eddysailab/bruce-media)"}

def get(url, binary=False):
    req = urllib.request.Request(url, headers=UA)
    with urllib.request.urlopen(req, timeout=60) as r:
        data = r.read()
    return data if binary else json.loads(data)

BAD = {"animation", "cartoon", "illustration", "vector", "3d", "render", "isometric", "clipart", "drawing", "anime"}
STOP = {"a", "an", "the", "and", "of", "in", "on", "with", "at", "for", "to"}

def rank(hits, q):
    """Pixabay's own order drifts off-topic fast ("black woman" returns black dogs). Keep hits whose tags
    share the most words with the query and drop illustrations and animations."""
    words = [w for w in q.lower().split() if w not in STOP]
    def score(h):
        tags = h.get("tags", "").lower()
        if any(b in tags for b in BAD): return -1
        return sum(1 for w in words if w in tags) + 0.001 * min(h.get("likes", 0), 900)
    return [h for h in sorted(hits, key=score, reverse=True) if score(h) >= 1]

def pixabay(item, out, log):
    if not KEY:
        log.append(f"{item['id']}: skipped, PIXABAY_API_KEY secret is not set"); return []
    n = max(1, min(int(item.get("n", 4)), 6))
    q = urllib.parse.quote_plus(item["q"][:100])
    res = []
    if item.get("type") == "video":
        d = get(f"https://pixabay.com/api/videos/?key={KEY}&q={q}&safesearch=true&video_type=film&per_page=40")
        for k, h in enumerate(rank(d.get("hits", []), item["q"])[:n], 1):
            v = h["videos"].get("large") if h["videos"].get("large", {}).get("url") else h["videos"]["medium"]
            raw = out / f"_{item['id']}_{k}.mp4"; dst = out / f"{item['id']}_{k}.mp4"
            raw.write_bytes(get(v["url"], binary=True))
            # 9:16 cover crop, max 8 s, no audio, small file
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-i", str(raw), "-t", "8", "-an",
                            "-vf", "scale=1080:1920:force_original_aspect_ratio=increase,crop=1080:1920,fps=30",
                            "-c:v", "libx264", "-crf", "24", "-preset", "veryfast", "-pix_fmt", "yuv420p", str(dst)], check=True)
            raw.unlink()
            subprocess.run([FFMPEG, "-loglevel", "error", "-y", "-ss", "1", "-i", str(dst), "-frames:v", "1",
                            "-vf", "scale=360:-1", str(out / f"{item['id']}_{k}.thumb.jpg")], check=True)
            res.append({"file": dst.name, "type": "video", "query": item["q"], "tags": h.get("tags"), "page": h.get("pageURL"),
                        "user": h.get("user"), "seconds": h.get("duration")})
    else:
        d = get(f"https://pixabay.com/api/?key={KEY}&q={q}&image_type=photo&safesearch=true&per_page=60")
        for k, h in enumerate(rank(d.get("hits", []), item["q"])[:n], 1):
            url = h.get("largeImageURL") or h["webformatURL"]
            dst = out / f"{item['id']}_{k}.jpg"
            dst.write_bytes(get(url, binary=True))
            res.append({"file": dst.name, "type": "photo", "query": item["q"], "tags": h.get("tags"), "page": h.get("pageURL"),
                        "user": h.get("user"), "w": h.get("imageWidth"), "h": h.get("imageHeight")})
    log.append(f"{item['id']}: {len(res)} {item.get('type', 'photo')} result(s) for '{item['q']}'")
    time.sleep(0.7)
    return res

CLOSE = ["Accept all", "Accept All", "I Accept", "Accept", "Agree", "I agree", "Got it", "OK", "Continue", "No thanks", "Close"]

def shots(items, out, log):
    if not items: return []
    from playwright.sync_api import sync_playwright
    res = []
    with sync_playwright() as p:
        b = p.chromium.launch()
        ctx = b.new_context(viewport={"width": 430, "height": 932}, device_scale_factor=2.5, is_mobile=True, has_touch=True,
                            user_agent="Mozilla/5.0 (iPhone; CPU iPhone OS 17_0 like Mac OS X) AppleWebKit/605.1.15 (KHTML, like Gecko) Version/17.0 Mobile/15E148 Safari/604.1")
        for it in items:
            pg = ctx.new_page()
            try:
                pg.goto(it["url"], wait_until="domcontentloaded", timeout=45000); pg.wait_for_timeout(3500)
                for label in CLOSE:
                    try:
                        btn = pg.get_by_role("button", name=label, exact=True)
                        if btn.count(): btn.first.click(timeout=1500); pg.wait_for_timeout(600)
                    except Exception:
                        pass
                pg.evaluate("""()=>{for(const el of document.querySelectorAll('*')){const s=getComputedStyle(el);
                  if((s.position==='fixed'||s.position==='sticky')&&el.offsetHeight>window.innerHeight*0.25)el.remove();}}""")
                pg.wait_for_timeout(500)
                h = min(pg.evaluate("document.documentElement.scrollHeight"), 2600)
                dst = out / f"{it['id']}.jpg"
                pg.screenshot(path=str(dst), type="jpeg", quality=82, full_page=True, clip={"x": 0, "y": 0, "width": 430, "height": h})
                res.append({"file": dst.name, "type": "shot", "url": it["url"], "title": pg.title()})
                log.append(f"{it['id']}: screenshot ok ({it['url']})")
            except Exception as ex:
                log.append(f"{it['id']}: screenshot failed ({it['url']}): {str(ex)[:160]}")
            finally:
                pg.close()
        b.close()
    return res

def main():
    jobs = sorted(pathlib.Path("jobs").glob("*.json"), key=lambda f: f.stat().st_mtime)
    want = os.environ.get("JOB", "").strip()
    job = pathlib.Path(want) if want else (jobs[-1] if jobs else None)
    if not job or not job.exists():
        sys.exit("no job file")
    spec = json.loads(job.read_text())
    slug = spec["slug"]
    out = pathlib.Path("out") / slug; out.mkdir(parents=True, exist_ok=True)
    log, files = [], []
    for item in spec.get("pixabay", []):
        try:
            files += pixabay(item, out, log)
        except Exception as ex:
            log.append(f"{item.get('id')}: pixabay error: {str(ex)[:200]}")
    files += shots(spec.get("shots", []), out, log)
    (out / "manifest.json").write_text(json.dumps({"slug": slug, "job": str(job), "created": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
                                                   "credit": "Photos and footage from Pixabay (pixabay.com)", "log": log, "files": files}, indent=1))
    print("\n".join(log))

if __name__ == "__main__":
    main()
