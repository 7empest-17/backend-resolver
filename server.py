import os
import re
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Roku Stream Resolver Pro")

LAMOVIE_API_BASE = "https://lamovie.org/wp-api/v1"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

# 1. Definición de la prioridad oficial del sitio
SERVER_PRIORITY = {
    "vimeos": 1,
    "goodstream": 2,
    "voe": 3
}

def resolver_vimeos(url: str) -> str:
    """Extrae el enlace hls (.m3u8) directamente del reproductor de Vimeos."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', r.text)
        if match:
            return match.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None

def resolver_goodstream(url: str) -> str:
    """Extrae el enlace hls (.m3u8) directamente del HTML de Goodstream."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', r.text)
        if match:
            return match.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None

def resolver_voe(url: str) -> str:
    """Extrae el stream directo de Voe."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        match = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", r.text)
        if match:
            return match.group(1)
    except Exception:
        pass
    return None

def resolver_ytdlp(url: str) -> str:
    """Extractor de respaldo genérico."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "best"
    }
    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("url")

def obtener_peso_prioridad(embed: dict) -> int:
    """Asigna el peso numérico según la prioridad: vimeos (1), goodstream (2), voe (3)."""
    raw_url = embed.get("url", "").lower()
    for servidor, peso in SERVER_PRIORITY.items():
        if servidor in raw_url:
            return peso
    return 99  # Cualquier otro servidor que no esté en la lista de prioridad queda al final

@app.get("/")
def home():
    return {"status": "ok", "service": "stream-resolver"}

@app.get("/api/stream")
def get_stream(post_id: int = Query(..., description="ID del post")):
    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=10)
        res_json = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando API interna: {str(e)}")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores para este post")

    # 2. Ordenar los embeds según el orden estricto de prioridad
    embeds_ordenados = sorted(embeds, key=obtener_peso_prioridad)

    streams_disponibles = []

    for i, embed in enumerate(embeds_ordenados):
        raw_url = embed.get("url", "").replace(r"\/", "/")
        servidor = embed.get("server", f"Opcion {i+1}")
        idioma = embed.get("lang", "Latino")
        calidad = embed.get("quality", "Full HD")
        stream_url = None

        try:
            if "vimeos" in raw_url:
                stream_url = resolver_vimeos(raw_url) or resolver_ytdlp(raw_url)
            elif "goodstream" in raw_url:
                stream_url = resolver_goodstream(raw_url) or resolver_ytdlp(raw_url)
            elif "voe.sx" in raw_url:
                stream_url = resolver_voe(raw_url) or resolver_ytdlp(raw_url)
            else:
                stream_url = resolver_ytdlp(raw_url)

            if stream_url:
                streams_disponibles.append({
                    "id": i,
                    "nombre": f"{servidor} ({idioma} - {calidad})",
                    "stream_format": "hls" if ".m3u8" in stream_url else "mp4",
                    "url": stream_url
                })
        except Exception:
            continue

    if not streams_disponibles:
        raise HTTPException(status_code=500, detail="No se pudo extraer ningún stream disponible")

    return {
        "status": "success",
        "post_id": post_id,
        "selected_stream": streams_disponibles[0],
        "total": len(streams_disponibles),
        "streams": streams_disponibles
    }
