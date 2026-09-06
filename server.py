import os
import re
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query

app = FastAPI()

LAMOVIE_API_BASE = "https://lamovie.org/wp-api/v1"  # Reemplaza por la URL base real
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
}

def resolver_con_ytdlp(embed_url: str) -> str:
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "best"
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(embed_url, download=False)
        return info.get("url")

def resolver_voe_manual(embed_url: str) -> str:
    res = requests.get(embed_url, headers=HEADERS, timeout=10)
    match = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", res.text)
    return match.group(1) if match else None

@app.get("/")
def health_check():
    return {"status": "ok", "service": "stream-resolver"}

@app.get("/api/stream")
def get_stream(post_id: int = Query(..., description="ID de la película (_id)")):
    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=10)
        res_json = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando API interna: {str(e)}")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores")

    stream_url = None
    selected_server = None

    for embed in embeds:
        raw_url = embed.get("url", "").replace(r"\/", "/")
        try:
            if "voe.sx" in raw_url:
                stream_url = resolver_voe_manual(raw_url) or resolver_con_ytdlp(raw_url)
            else:
                stream_url = resolver_con_ytdlp(raw_url)

            if stream_url:
                selected_server = embed.get("server", "Online")
                break
        except Exception:
            continue

    if not stream_url:
        raise HTTPException(status_code=500, detail="No se pudo extraer el stream")

    return {
        "status": "success",
        "post_id": post_id,
        "server": selected_server,
        "stream_format": "hls" if ".m3u8" in stream_url else "mp4",
        "url": stream_url
    }
