import os
import re
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Roku Stream Resolver Master")

# Habilitar CORS para evitar restricciones en pruebas de clientes
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

LAMOVIE_API_BASE = "https://lamovie.org/wp-api/v1"
TMDB_API_KEY = os.getenv("TMDB_API_KEY")
TMDB_API_BASE = "https://api.themoviedb.org/3"
HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36",
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Accept-Language": "es-ES,es;q=0.9,en;q=0.8",
}

# 1. Prioridad de servidores solicitada
SERVER_PRIORITY = {
    "vimeos": 1,
    "goodstream": 2,
    "voe": 3
}

# 2. Caché dividida:
# - SEARCH_CACHE: datos estables (título/año -> post encontrado). Dura 2 horas.
# - STREAM_CACHE: enlaces de video firmados. Dura 60 segundos solo para absorber reintentos.
SEARCH_CACHE = {}
SEARCH_CACHE_TTL = 7200

STREAM_CACHE = {}
STREAM_CACHE_TTL = 60


def unpack_packer(html: str) -> str:
    """Desempaqueta scripts ofuscados con eval(function(p,a,c,k,e,d)...)."""
    match = re.search(r"}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)", html)
    if not match:
        return html
    payload, radix, count, symtab = match.groups()
    radix = int(radix)
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
    """Extrae stream HLS para hosts Vimeos y clones asociados."""
    try:
        req_headers = HEADERS.copy()
        req_headers["Referer"] = "https://lamovie.org/"
        r = requests.get(url, headers=req_headers, timeout=6)
        html = r.text

        # 1. Búsqueda directa de m3u8
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
        if match:
            return match.group(1).replace(r"\/", "/")

        # 2. Desempaquetado JS
        unpacked = unpack_packer(html)
        match_unpacked = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', unpacked)
        if match_unpacked:
            return match_unpacked.group(1).replace(r"\/", "/")

        # 3. Propiedad file o source
        match_file = re.search(r'(?:file|source|src)\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']', unpacked)
        if match_file:
            return match_file.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None


def resolver_goodstream(url: str) -> str:
    """Extrae stream directo de Goodstream."""
    try:
        req_headers = HEADERS.copy()
        req_headers["Referer"] = "https://lamovie.org/"
        r = requests.get(url, headers=req_headers, timeout=6)
        html = r.text
        match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
        if match:
            return match.group(1).replace(r"\/", "/")
        
        unpacked = unpack_packer(html)
        match_unpacked = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', unpacked)
        if match_unpacked:
            return match_unpacked.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None


def resolver_voe(url: str) -> str:
    """Extrae stream HLS o MP4 de Voe."""
    try:
        r = requests.get(url, headers=HEADERS, timeout=6)
        html = r.text
        match_hls = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        if match_hls:
            return match_hls.group(1).replace(r"\/", "/")
        match_mp4 = re.search(r"['\"]mp4['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        if match_mp4:
            return match_mp4.group(1).replace(r"\/", "/")
    except Exception:
        pass
    return None


def resolver_ytdlp(url: str) -> str:
    """Extractor universal yt-dlp con impersonación para sitios con anti-bot."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "best",
        "socket_timeout": 10,
        "extractor_args": {
            "generic": {
                "impersonate": [""]
            }
        }
    }

    with yt_dlp.YoutubeDL(ydl_opts) as ydl:
        info = ydl.extract_info(url, download=False)
        return info.get("url")


def obtener_peso_prioridad(embed: dict) -> int:
    """Ordena los servidores: vimeos (1), goodstream (2), voe (3), otros (99)."""
    raw_url = embed.get("url", "").lower()
    for servidor, peso in SERVER_PRIORITY.items():
        if servidor in raw_url:
            return peso
    return 99


def procesar_un_embed(index: int, embed: dict) -> dict:
    """Ejecuta la extracción de un embed individual."""
    raw_url = embed.get("url", "").replace(r"\/", "/")
    servidor = embed.get("server", "Online")
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
    except Exception:
        pass

    if stream_url:
        return {
            "id": index,
            "peso": obtener_peso_prioridad(embed),
            "nombre": f"{servidor} ({idioma} - {calidad})",
            "stream_format": "hls" if ".m3u8" in stream_url else "mp4",
            "url": stream_url
        }
    return None


@app.get("/")
def home():
    return {"status": "ok", "service": "roku-stream-resolver-v2"}


@app.get("/api/stream")
def get_stream(post_id: int = Query(..., description="ID del post en la API")):
    ahora = time.time()

    # 1. Retorno desde memoria únicamente si no ha expirado la ventana corta (60s)
    if post_id in STREAM_CACHE and STREAM_CACHE[post_id]["expires_at"] > ahora:
        return STREAM_CACHE[post_id]["data"]

    # 2. Consulta de embeds frescos a Lamovie
    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=10)
        res_json = r.json()
    except Exception as e:
        raise HTTPException(status_code=502, detail=f"Error consultando API interna: {str(e)}")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores para este post")

    # 3. Extracción en paralelo (hasta 5 servidores simultáneos)
    resultados = []
    with ThreadPoolExecutor(max_workers=5) as executor:
        futures = [executor.submit(procesar_un_embed, idx, emb) for idx, emb in enumerate(embeds)]
        for future in as_completed(futures):
            resultado = future.result()
            if resultado:
                resultados.append(resultado)

    if not resultados:
        raise HTTPException(status_code=500, detail="No se pudo extraer ningún stream funcional")

    # 4. Ordenar resultados por la prioridad solicitada
    resultados.sort(key=lambda x: (x["peso"], x["id"]))

    # Limpiar campo temporal de peso
    for item in resultados:
        del item["peso"]

    respuesta = {
        "status": "success",
        "post_id": post_id,
        "selected_stream": resultados[0],
        "total": len(resultados),
        "streams": resultados
    }

    # 5. Guardar en caché con TTL corto (60 s) para evitar tokens caducados
    STREAM_CACHE[post_id] = {
        "data": respuesta,
        "expires_at": ahora + STREAM_CACHE_TTL
    }

    return respuesta


def get_tmdb_localized_title(title: str, year: str = None):
    if not TMDB_API_KEY:
        return None

    params = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "language": "es-MX",
        "include_adult": "false"
    }

    if year:
        params["year"] = year

    try:
        response = requests.get(
            f"{TMDB_API_BASE}/search/movie",
            params=params,
            timeout=8
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            return results[0].get("title")
    except Exception:
        pass

    return None


@app.get("/api/resolve_by_title")
def resolve_by_title(
    title: str = Query(..., description="Título de la película o serie"),
    year: str = Query(None, description="Año de estreno opcional")
):
    """Busca una película por título y año y devuelve su stream fresco usando el post_id encontrado."""

    def normalize_text(value):
        if not value:
            return ""
        value = str(value).lower().strip()
        value = re.sub(r"\s*\(\d{4}\)\s*$", "", value)
        value = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", value)
        return re.sub(r"\s+", " ", value).strip()

    ahora = time.time()
    clean_year = year.strip()[:4] if year and year.strip() else ""
    cache_key = f"{title.lower().strip()}_{clean_year}"

    selected_post = None

    # 1. Revisar si ya conocemos qué post_id corresponde a este título (Caché de 2 horas)
    if cache_key in SEARCH_CACHE and SEARCH_CACHE[cache_key]["expires_at"] > ahora:
        selected_post = SEARCH_CACHE[cache_key]["post"]

    if not selected_post:
        tmdb_title = get_tmdb_localized_title(title, year)
        search_title = tmdb_title or title
        target_title = normalize_text(search_title)
        
        search_url = (
            f"{LAMOVIE_API_BASE}/search"
            f"?postType=any"
            f"&q={requests.utils.quote(search_title)}"
            f"&postsPerPage=20"
        )

        try:
            r = requests.get(search_url, headers=HEADERS, timeout=8)
            r.raise_for_status()
            data = r.json().get("data", {})
            posts = data.get("posts", [])
        except Exception as e:
            raise HTTPException(
                status_code=502,
                detail=f"Error buscando en la API: {str(e)}"
            )

        if not posts:
            raise HTTPException(
                status_code=404,
                detail="Título no encontrado en la base de datos"
            )

        # A. Coincidencia exacta de título + año
        if clean_year:
            for post in posts:
                post_titles = [
                    normalize_text(post.get("original_title")),
                    normalize_text(post.get("title")),
                    normalize_text(post.get("post_title"))
                ]
                post_year = str(post.get("release_date", ""))[:4]
                if target_title in post_titles and post_year == clean_year:
                    selected_post = post
                    break

        # B. Coincidencia exacta de título sin año
        if not selected_post:
            for post in posts:
                post_titles = [
                    normalize_text(post.get("original_title")),
                    normalize_text(post.get("title")),
                    normalize_text(post.get("post_title"))
                ]
                if target_title in post_titles:
                    selected_post = post
                    break

        # C. Coincidencia parcial + año
        if not selected_post and clean_year:
            for post in posts:
                post_titles = [
                    normalize_text(post.get("original_title")),
                    normalize_text(post.get("title")),
                    normalize_text(post.get("post_title"))
                ]
                post_year = str(post.get("release_date", ""))[:4]
                if post_year == clean_year and any(target_title in pt or pt in target_title for pt in post_titles):
                    selected_post = post
                    break

        if not selected_post:
            raise HTTPException(
                status_code=404,
                detail=f"No se encontró una coincidencia segura para '{title}'"
                       + (f" ({year})" if year else "")
            )

        # Guardar en la caché de búsqueda estable (2 horas)
        SEARCH_CACHE[cache_key] = {
            "post": selected_post,
            "expires_at": ahora + SEARCH_CACHE_TTL
        }

    selected_id = selected_post.get("_id")
    if not selected_id:
        raise HTTPException(
            status_code=502,
            detail="La API encontró el título pero no devolvió _id"
        )

    # 2. Obtener siempre el stream en vivo (o dentro de la ventana de 60s)
    result = get_stream(post_id=selected_id)

    if isinstance(result, dict):
        result["mapping"] = {
            "requested_title": title,
            "requested_year": year,
            "matched_title": (
                selected_post.get("original_title")
                or selected_post.get("title")
                or selected_post.get("post_title")
            ),
            "release_date": selected_post.get("release_date"),
            "post_id": selected_id
        }

    return result
