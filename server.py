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

# 1. Prioridad oficial del sitio
SERVER_PRIORITY = {
    "vimeos": 1,
    "goodstream": 2,
    "voe": 3
}

def unpack_packer(html: str) -> str:
    """Desempaqueta scripts ofuscados con eval(function(p,a,c,k,e,d)...)."""
    match = re.search(r"}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)", html)
    if not match:
        return html
    payload, radix, count, symtab = match.groups()
    radix = int(radix)
    count = int(count)
    symtab = symtab.split('|')

    def unbase(val):
        digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = 0
        for i, c in enumerate(reversed(val)):
            res += digits.index(c) * (radix ** i)
        return res

    def replace_token(token_match):
        token = token_match.group(0)
        idx = unbase(token)
        if idx < len(symtab) and symtab[idx]:
            return symtab[idx]
        return token

    return re.sub(r"\b\w+\b", replace_token, payload)

def resolver_vimeos(url: str) -> str:
    """Extrae el enlace hls (.m3u8) desempaquetando el reproductor de Vimeos."""
    try:
        headers = {
            "User-Agent": HEADERS["User-Agent"],
            "Referer": "https://lamovie.org/"
        }
        r = requests.get(url, headers=headers, timeout=8)
        html = r.text

        # 1. Búsqueda directa
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
        if match:
            return match.group(1).replace(r"\/", "/")

        # 2. Desempaquetado si usa Packer
        unpacked = unpack_packer(html)
        match_unpacked = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', unpacked)
        if match_unpacked:
            return match_unpacked.group(1).replace(r"\/", "/")

        # 3. Búsqueda en propiedad 'file' o 'source'
        match_file = re.search(r'file\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', unpacked)
        if match_file:
            return match_file.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None

def resolver_goodstream(url: str) -> str:
    """Extrae el enlace directo de Goodstream."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', r.text)
        if match:
            return match.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None

def resolver_voe(url: str) -> str:
    """Extrae el enlace directo de Voe."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=8)
        match = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", r.text)
        if match:
            return match.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None

def resolver_ytdlp(url: str) -> str:
    """Extractor universal de respaldo."""
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
    """Calcula el orden numérico: vimeos (1), goodstream (2), voe (3), otros (99)."""
    raw_url = embed.get("url", "").lower()
    for servidor, peso in SERVER_PRIORITY.items():
        if servidor in raw_url:
            return peso
    return 99

@app.get("/")
def home():
    return {"status": "ok", "service": "stream-resolver"}

@app.get("/api/stream")
def get_stream(post_id: int = Query(..., description="ID del post en la API")):
    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=10)
        res_json = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando API interna: {str(e)}")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores para este post")

    # Ordenar lista aplicando la prioridad
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
