import os
import re
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query

app = FastAPI(title="Roku Stream Resolver Pro")

# Configura aquí el dominio base donde está montado el sitio/API
LAMOVIE_API_BASE = "https://lamovie.org/wp-api/v1"  # <-- Cambia esto por el dominio real
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/122.0.0.0 Safari/537.36"
}

def resolver_con_ytdlp(embed_url: str) -> str:
    """Extrae la URL directa (.m3u8 o .mp4) usando yt-dlp."""
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
    """Extractor ligero por Regex para embeds de Voe."""
    try:
        res = requests.get(embed_url, headers=HEADERS, timeout=8)
        match = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", res.text)
        return match.group(1) if match else None
    except Exception:
        return None

def validar_stream(url: str) -> bool:
    """Comprueba que el .m3u8 devuelva un código 200/activo antes de mandarlo a Roku."""
    try:
        r = requests.head(url, headers=HEADERS, timeout=4, allow_redirects=True)
        return r.status_code in [200, 206, 302]
    except Exception:
        return False

@app.get("/")
def home():
    return {"status": "ok", "service": "stream-resolver-multi"}

@app.get("/api/stream")
def get_streams(post_id: int = Query(..., description="ID del post en la API")):
    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=10)
        res_json = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando API interna: {str(e)}")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores para este título")

    streams_disponibles = []

    # Procesa y valida cada embed disponible
    for i, embed in enumerate(embeds):
        raw_url = embed.get("url", "").replace(r"\/", "/")
        servidor_nombre = embed.get("server", f"Opcion {i+1}")
        idioma = embed.get("lang", "Latino")
        calidad = embed.get("quality", "HD")

        candidate_url = None
        try:
            if "voe.sx" in raw_url:
                candidate_url = resolver_voe_manual(raw_url) or resolver_con_ytdlp(raw_url)
            else:
                candidate_url = resolver_con_ytdlp(raw_url)

            # Si el enlace es válido y responde, lo agregamos a la lista de opciones
            if candidate_url and validar_stream(candidate_url):
                streams_disponibles.append({
                    "id": i,
                    "nombre": f"{servidor_nombre} ({idioma} - {calidad})",
                    "stream_format": "hls" if ".m3u8" in candidate_url else "mp4",
                    "url": candidate_url
                })
        except Exception:
            continue

    if not streams_disponibles:
        raise HTTPException(status_code=500, detail="Ningún servidor de streaming está disponible en este momento")

    return {
        "status": "success",
        "post_id": post_id,
        "total": len(streams_disponibles),
        "streams": streams_disponibles
    }
