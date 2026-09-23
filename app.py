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
        hz =
