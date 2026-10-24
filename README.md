# bruce-media

Media and render engines for Bruce, the Instagram agent for @blackwell_graphics.

- `engine/BRUCE.md` the operating manual (mirrors the "bruce" skill)
- `engine/render.py` carousel renderer (Playwright), Wednesdays
- `engine/reel.py` 45 to 60 second explainer reel renderer, Mondays and Fridays. Run `engine/setup_reel.sh` once per session first (free Kokoro voice "am_onyx", Simple Icons logos, Lucide icons).
- `engine/music/` optional music beds (.mp3 or .wav, royalty free). Empty means the renderer generates a quiet bed.
- `posts/YYYY-MM-DD-slug/` the slides or reel, spec and caption for each post. Media URLs from this repo are what Metricool publishes.
