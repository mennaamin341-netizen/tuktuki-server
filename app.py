"""
Tuktuki Server - Edge-TTS (Egyptian Arabic) + Shotstack timeline builder
Endpoints:
  GET  /                -> health check
  POST /tts             -> one line of speech, returns audio/mpeg
  POST /tts-batch       -> whole episode: voices + images + ready Shotstack timeline
  GET  /files/{name}    -> public audio/image files (Shotstack downloads from here)
"""
import os, re, time, uuid, asyncio, urllib.parse, base64
from pathlib import Path
import edge_tts, httpx
from mutagen.mp3 import MP3
from fastapi import FastAPI, HTTPException, Header, Request
from fastapi.responses import FileResponse, Response
from fastapi.staticfiles import StaticFiles

API_KEY = os.getenv("API_KEY", "")                 # set in Render > Environment
CF_ACCOUNT_ID = os.getenv("CF_ACCOUNT_ID", "")      # Cloudflare Workers AI (free FLUX images)
CF_API_TOKEN = os.getenv("CF_API_TOKEN", "")
POLLINATIONS_TOKEN = os.getenv("POLLINATIONS_TOKEN", "")  # optional fallback
PUBLIC_HOST = os.getenv("RENDER_EXTERNAL_HOSTNAME") or os.getenv("SPACE_HOST") or ""  # set automatically by Render
FILES = Path("/tmp/files"); FILES.mkdir(parents=True, exist_ok=True)
STATIC = Path(__file__).parent / "static"; STATIC.mkdir(exist_ok=True)

app = FastAPI(title="Tuktuki Server")
app.mount("/static", StaticFiles(directory=STATIC), name="static")

# ---------- voices (from Series_Bible) ----------
FEMALE, MALE = "ar-EG-SalmaNeural", "ar-EG-ShakirNeural"
DEFAULT_VOICES = {
    "توكتوكي": {"voice": MALE,   "pitch": "+25%", "rate": "-10%"},
    "مشمش":   {"voice": FEMALE, "pitch": "+30%", "rate": "-10%"},
    "فرفور":   {"voice": FEMALE, "pitch": "+45%", "rate": "-5%"},
    "الشمس":  {"voice": FEMALE, "pitch": "+10%", "rate": "-15%"},
    "الراوية": {"voice": FEMALE, "pitch": "+0%",  "rate": "-15%"},
}
NARRATOR = DEFAULT_VOICES["الراوية"]

# Always cartoon, even if the request forgets style_prompt
DEFAULT_STYLE = ("cute 2D children's cartoon illustration, flat vector style, thick soft rounded outlines, "
                 "bright cheerful pastel colors, simple clean background, big expressive eyes, toddler-friendly, "
                 "colorful Egyptian neighborhood alley with low houses in orange, yellow, purple and green, "
                 "colorful triangle bunting flags, smiling yellow sun in a blue sky")
STYLE_SUFFIX = "cartoon, animated kids TV show style, not photorealistic, not 3D render, no humans, no driver, no text, no watermark"

def norm_rate(v):
    v = str(v or "+0%").strip()
    return v if re.fullmatch(r"[+-]\d+%", v) else ("+" + v if re.fullmatch(r"\d+%", v) else "+0%")

def norm_pitch(v, voice):
    """edge-tts needs Hz. Accepts '+25%' (converted) or '+40Hz'."""
    v = str(v or "+0Hz").strip()
    if re.fullmatch(r"[+-]?\d+Hz", v):
        return v if v[0] in "+-" else "+" + v
    m = re.fullmatch(r"([+-]?)(\d+)%", v)
    if m:
        base = 120 if voice == MALE else 200
        hz = round(int(m.group(2)) / 100 * base)
        return f"{m.group(1) or '+'}{hz}Hz"
    return "+0Hz"

def check_key(key):
    if API_KEY and key != API_KEY:
        raise HTTPException(401, "invalid api key")

def public_url(name, request: Request):
    host = PUBLIC_HOST or request.url.netloc
    return f"https://{host}/files/{name}"

def cleanup(max_age_hours=48):
    now = time.time()
    for f in FILES.iterdir():
        if now - f.stat().st_mtime > max_age_hours * 3600:
            f.unlink(missing_ok=True)

async def synth(text, voice, rate, pitch, path):
    for attempt in range(3):
        try:
            await edge_tts.Communicate(text, voice, rate=norm_rate(rate), pitch=norm_pitch(pitch, voice)).save(str(path))
            return MP3(str(path)).info.length
        except Exception:
            if attempt == 2:
                raise
            await asyncio.sleep(2)

async def cf_image(client, prompt, seed, path):
    """Cloudflare Workers AI - FLUX.1 schnell (free daily quota)."""
    url = f"https://api.cloudflare.com/client/v4/accounts/{CF_ACCOUNT_ID}/ai/run/@cf/black-forest-labs/flux-1-schnell"
    for attempt in range(5):
        try:
            r = await client.post(url, headers={"Authorization": f"Bearer {CF_API_TOKEN}"},
                                  json={"prompt": prompt[:2000], "steps": 8}, timeout=120)
            if r.status_code == 200:
                img = (r.json().get("result") or {}).get("image")
                if img:
                    path.write_bytes(base64.b64decode(img))
                    return True
            print("cloudflare image error:", r.status_code, r.text[:300])
        except Exception as e:
            print("cloudflare image exception:", e)
        await asyncio.sleep(5 * (attempt + 1))
    return False

async def fetch_image(client, url, path):
    headers = {"Authorization": f"Bearer {POLLINATIONS_TOKEN}"} if POLLINATIONS_TOKEN else {}
    for attempt in range(3):
        try:
            r = await client.get(url, headers=headers, timeout=120)
            if r.status_code == 200 and r.headers.get("content-type", "").startswith("image"):
                path.write_bytes(r.content)
                return True
        except Exception:
            pass
        await asyncio.sleep(3)
    return False

# ---------- endpoints ----------
@app.get("/")
def health():
    return {"status": "ok", "voices": list(DEFAULT_VOICES)}

@app.get("/files/{name}")
def files(name: str):
    p = FILES / Path(name).name
    if not p.exists():
        raise HTTPException(404)
    return FileResponse(p)

@app.post("/tts")
async def tts(body: dict, x_api_key: str = Header(default="")):
    check_key(x_api_key)
    voice = body.get("voice", FEMALE)
    p = FILES / f"{uuid.uuid4().hex}.mp3"
    await synth(body["text"], voice, body.get("rate"), body.get("pitch"), p)
    return Response(p.read_bytes(), media_type="audio/mpeg")

@app.post("/tts-batch")
async def tts_batch(body: dict, request: Request, x_api_key: str = Header(default="")):
    """
    Body:
    {
      "episode": { parsed Gemini JSON with "scenes": [{"scene","speaker","text","image_prompt"}] },
      "style_prompt": "art_style + world image_prompt_en (optional, prepended to every image prompt)",
      "voices": { optional override: {"name": {"voice","pitch","rate"}} },
      "music_url": "optional direct mp3 link",
      "series_title": "توكتوكي",
      "outro_text": "باي باي! نشوفكم بكرة!",
      "seed": 12345
    }
    """
    check_key(x_api_key)
    cleanup()
    ep = body.get("episode") or {}
    scenes = ep.get("scenes") or body.get("scenes") or []
    if not scenes:
        raise HTTPException(400, "no scenes")
    voices = {**DEFAULT_VOICES, **(body.get("voices") or {})}
    style = (body.get("style_prompt") or "").strip() or DEFAULT_STYLE
    seed = int(body.get("seed", 12345))
    run = uuid.uuid4().hex[:8]

    # 1) voices (parallel, max 4 at a time)
    sem = asyncio.Semaphore(4)
    async def do_audio(i, s):
        v = voices.get(str(s.get("speaker", "")).strip(), NARRATOR)
        p = FILES / f"{run}_a{i}.mp3"
        async with sem:
            d = await synth(s["text"], v.get("voice", FEMALE), v.get("rate"), v.get("pitch"), p)
        return p.name, d
    audio = await asyncio.gather(*[do_audio(i, s) for i, s in enumerate(scenes)])

    # 2) images (download from Pollinations so Shotstack never times out)
    async def do_image(client, i, s):
        prompt = ", ".join(x for x in [style, s.get("image_prompt", ""), STYLE_SUFFIX] if x)
        url = ("https://image.pollinations.ai/prompt/" + urllib.parse.quote(prompt[:1500]) +
               f"?width=1280&height=720&seed={seed}&nologo=true")
        p = FILES / f"{run}_i{i}.jpg"
        ok = False
        if CF_ACCOUNT_ID and CF_API_TOKEN:
            ok = await cf_image(client, prompt + ", wide shot, centered composition", seed + i, p)
        if not ok:
            ok = await fetch_image(client, url, p)
        return public_url(p.name, request) if ok else None
    async with httpx.AsyncClient(follow_redirects=True) as client:
        img_sem = asyncio.Semaphore(2)
        async def limited(i, s):
            async with img_sem:
                return await do_image(client, i, s)
        images = await asyncio.gather(*[limited(i, s) for i, s in enumerate(scenes)])

    # fill failed scenes with the nearest successful image, so the render never breaks
    good = [u for u in images if u]
    if not good:
        raise HTTPException(502, "all image generations failed (check CF_ACCOUNT_ID / CF_API_TOKEN and Cloudflare quota)")
    for i in range(len(images)):
        if not images[i]:
            prev = next((images[j] for j in range(i - 1, -1, -1) if images[j]), None)
            images[i] = prev or good[0]
            print(f"scene {i}: image failed, reused another scene image")

    # 3) Shotstack timeline
    font_url = public_url_static("Lalezar.ttf", request)
    css_sub = ("p{font-family:'Lalezar';font-size:44px;color:#ffffff;text-align:center;direction:rtl;"
               "text-shadow:3px 3px 0 #3E2723,-3px -3px 0 #3E2723,3px -3px 0 #3E2723,-3px 3px 0 #3E2723;}")
    css_title = ("p{font-family:'Lalezar';font-size:110px;color:#FFEB3B;text-align:center;direction:rtl;"
                 "text-shadow:5px 5px 0 #3E2723,-5px -5px 0 #3E2723,5px -5px 0 #3E2723,-5px 5px 0 #3E2723;}")
    effects = ["zoomInSlow", "slideLeftSlow", "zoomOutSlow", "slideRightSlow"]
    INTRO, OUTRO = 3.0, 4.0
    subs, pics, sounds, titles = [], [], [], []
    t = INTRO
    titles.append(html_clip(body.get("series_title", "توكتوكي"), css_title, 0, INTRO, 1280, 300, "center"))
    pics.append({"asset": {"type": "image", "src": images[0]}, "start": 0, "length": INTRO, "effect": "zoomIn", "fit": "cover"})
    for i, s in enumerate(scenes):
        name, dur = audio[i]
        length = round(dur + 0.4, 2)
        pics.append({"asset": {"type": "image", "src": images[i]}, "start": t, "length": length,
                     "effect": effects[i % 4], "fit": "cover", "transition": {"in": "fade"}})
        sounds.append({"asset": {"type": "audio", "src": public_url(name, request), "volume": 1}, "start": t, "length": length})
        if body.get("subtitles", False):
            subs.append(html_clip(s["text"], css_sub, t, length, 1200, 160, "bottom"))
        t = round(t + length, 2)
    pics.append({"asset": {"type": "image", "src": images[-1]}, "start": t, "length": OUTRO, "effect": "zoomOut", "fit": "cover"})
    titles.append(html_clip(body.get("outro_text", "باي باي! نشوفكم بكرة!"), css_title.replace("110px", "80px"), t, OUTRO, 1280, 300, "center"))
    total = round(t + OUTRO, 2)

    timeline = {"fonts": [{"src": font_url}], "background": "#B3E5FC",
                "tracks": ([{"clips": subs}] if subs else []) + [{"clips": titles}, {"clips": pics}, {"clips": sounds}]}
    music = body.get("music_url") or (public_url_static("music.mp3", request) if (STATIC / "music.mp3").exists() else "")
    if music:
        timeline["soundtrack"] = {"src": music, "effect": "fadeOut", "volume": 0.1}

    return {"timeline": timeline,
            "output": {"format": "mp4", "resolution": "hd"},
            "total_duration": total,
            "scene_count": len(scenes)}

def html_clip(text, css, start, length, w, h, position):
    safe = str(text).replace("&", "&amp;").replace("<", "&lt;").replace(">", "&gt;")
    return {"asset": {"type": "html", "html": f"<p>{safe}</p>", "css": css, "width": w, "height": h},
            "start": start, "length": length, "position": position,
            **({"offset": {"y": 0.05}} if position == "bottom" else {})}

def public_url_static(name, request: Request):
    host = PUBLIC_HOST or request.url.netloc
    return f"https://{host}/static/{name}"
