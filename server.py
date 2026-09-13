import os
import re
import time
import threading
from concurrent.futures import ThreadPoolExecutor, as_completed

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Roku Stream Resolver Master")

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

SERVER_PRIORITY = {
    "vimeos": 1,
    "goodstream": 2,
    "voe": 3,
}

SEARCH_CACHE = {}
SEARCH_CACHE_TTL = 7200

STREAM_CACHE = {}
STREAM_CACHE_TTL = 60

# Evita que dos peticiones simultáneas para el mismo post disparen resoluciones duplicadas.
POST_LOCKS = {}
POST_LOCKS_GUARD = threading.Lock()


def get_post_lock(post_id: int) -> threading.Lock:
    with POST_LOCKS_GUARD:
        lock = POST_LOCKS.get(post_id)
        if lock is None:
            lock = threading.Lock()
            POST_LOCKS[post_id] = lock
        return lock


def unpack_packer(html: str) -> str:
    """Desempaqueta scripts ofuscados con eval(function(p,a,c,k,e,d)...)."""
    match = re.search(r"}\('(.*)',(\d+),(\d+),'(.*)'\.split\('\|'\)", html)
    if not match:
        return html

    payload, radix, count, symtab = match.groups()
    radix = int(radix)
    symtab = symtab.split("|")

    def unbase(val):
        digits = "0123456789abcdefghijklmnopqrstuvwxyzABCDEFGHIJKLMNOPQRSTUVWXYZ"
        res = 0
        for i, c in enumerate(reversed(val)):
            try:
                res += digits.index(c) * (radix ** i)
            except ValueError:
                return -1
        return res

    def replace_token(token_match):
        token = token_match.group(0)
        idx = unbase(token)
        if 0 <= idx < len(symtab) and symtab[idx]:
            return symtab[idx]
        return token

    return re.sub(r"\b\w+\b", replace_token, payload)


def extract_m3u8(html: str):
    match = re.search(r'(https?://[^"\'\s]+\.m3u8[^"\'\s]*)', html)
    if match:
        return match.group(1).replace(r"\/", "/")
    return None


def resolver_vimeos(url: str):
    try:
        req_headers = HEADERS.copy()
        req_headers["Referer"] = "https://lamovie.org/"
        r = requests.get(url, headers=req_headers, timeout=(4, 7))
        r.raise_for_status()
        html = r.text

        stream = extract_m3u8(html)
        if stream:
            return stream

        unpacked = unpack_packer(html)
        stream = extract_m3u8(unpacked)
        if stream:
            return stream

        match_file = re.search(
            r'(?:file|source|src)\s*:\s*["\']([^"\']+\.m3u8[^"\']*)["\']',
            unpacked,
        )
        if match_file:
            return match_file.group(1).replace(r"\/", "/")
    except Exception:
        return None
    return None


def resolver_goodstream(url: str):
    try:
        req_headers = HEADERS.copy()
        req_headers["Referer"] = "https://lamovie.org/"
        r = requests.get(url, headers=req_headers, timeout=(4, 7))
        r.raise_for_status()
        html = r.text

        stream = extract_m3u8(html)
        if stream:
            return stream

        return extract_m3u8(unpack_packer(html))
    except Exception:
        return None


def resolver_voe(url: str):
    try:
        r = requests.get(url, headers=HEADERS, timeout=(4, 7))
        r.raise_for_status()
        html = r.text

        match_hls = re.search(r"['\"]hls['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        if match_hls:
            return match_hls.group(1).replace(r"\/", "/")

        match_mp4 = re.search(r"['\"]mp4['\"]\s*:\s*['\"]([^'\"]+)['\"]", html)
        if match_mp4:
            return match_mp4.group(1).replace(r"\/", "/")
    except Exception:
        return None
    return None


def resolver_ytdlp(url: str):
    """Extractor estándar de yt-dlp. No intenta saltarse controles anti-bot."""
    ydl_opts = {
        "quiet": True,
        "no_warnings": True,
        "extract_flat": False,
        "format": "best",
        "socket_timeout": 7,
        "noplaylist": True,
        "skip_download": True,
    }

    try:
        with yt_dlp.YoutubeDL(ydl_opts) as ydl:
            info = ydl.extract_info(url, download=False)
            if not info:
                return None
            return info.get("url")
    except Exception:
        return None


def obtener_peso_prioridad(embed: dict) -> int:
    raw_url = str(embed.get("url", "")).lower()
    for servidor, peso in SERVER_PRIORITY.items():
        if servidor in raw_url:
            return peso
    return 99


def procesar_un_embed(index: int, embed: dict):
    raw_url = str(embed.get("url", "")).replace(r"\/", "/").strip()
    if not raw_url:
        return None

    servidor = embed.get("server", "Online")
    idioma = embed.get("lang", "Latino")
    calidad = embed.get("quality", "Full HD")
    stream_url = None

    # Primero usamos los resolvers específicos existentes.
    # Solo si no obtienen un stream se prueba yt-dlp de forma estándar.
    if "vimeos" in raw_url:
        stream_url = resolver_vimeos(raw_url)
    elif "goodstream" in raw_url:
        stream_url = resolver_goodstream(raw_url)
    elif "voe.sx" in raw_url:
        stream_url = resolver_voe(raw_url)

    if not stream_url:
        stream_url = resolver_ytdlp(raw_url)

    if not stream_url or not re.match(r"^https?://", stream_url):
        return None

    stream_lower = stream_url.lower()
    if ".m3u8" in stream_lower:
        stream_format = "hls"
    elif ".mp4" in stream_lower:
        stream_format = "mp4"
    else:
        # Conservador: si yt-dlp devuelve una URL válida pero sin extensión,
        # mantenemos hls solo cuando la URL lo indica explícitamente.
        stream_format = "mp4"

    return {
        "id": index,
        "peso": obtener_peso_prioridad(embed),
        "nombre": f"{servidor} ({idioma} - {calidad})",
        "stream_format": stream_format,
        "url": stream_url,
    }


def _resolve_post(post_id: int):
    now = time.time()

    cached = STREAM_CACHE.get(post_id)
    if cached and cached["expires_at"] > now:
        return cached["data"]

    player_url = f"{LAMOVIE_API_BASE}/player?postId={post_id}&demo=0"
    try:
        r = requests.get(player_url, headers=HEADERS, timeout=(5, 12))
        r.raise_for_status()
        res_json = r.json()
    except requests.RequestException as exc:
        raise HTTPException(status_code=502, detail=f"Error consultando servidores: {exc}")
    except ValueError:
        raise HTTPException(status_code=502, detail="La API de servidores devolvió JSON inválido")

    embeds = res_json.get("data", {}).get("embeds", [])
    if not isinstance(embeds, list) or not embeds:
        raise HTTPException(status_code=404, detail="No se encontraron servidores para este post")

    # Importante: no disparamos cinco resoluciones a la vez.
    # Se procesan en orden de prioridad para reducir solicitudes simultáneas
    # y evitar que un problema de un proveedor bloquee innecesariamente a los demás.
    indexed = list(enumerate(embeds))
    indexed.sort(key=lambda item: (obtener_peso_prioridad(item[1]), item[0]))

    resultados = []
    for idx, embed in indexed:
        resultado = procesar_un_embed(idx, embed)
        if resultado:
            resultados.append(resultado)

        # Con al menos un resultado prioritario ya podemos continuar.
        # Los demás se intentan solo cuando son necesarios para ofrecer fallback.
        if resultados and resultados[0]["peso"] <= 2:
            break

    if not resultados:
        raise HTTPException(
            status_code=502,
            detail="No se pudo resolver ningún servidor. El proveedor puede estar temporalmente inaccesible o protegido.",
        )

    resultados.sort(key=lambda x: (x["peso"], x["id"]))
    for item in resultados:
        item.pop("peso", None)

    respuesta = {
        "status": "success",
        "post_id": post_id,
        "selected_stream": resultados[0],
        "total": len(resultados),
        "streams": resultados,
    }

    STREAM_CACHE[post_id] = {
        "data": respuesta,
        "expires_at": now + STREAM_CACHE_TTL,
    }
    return respuesta


@app.get("/")
def home():
    return {"status": "ok", "service": "roku-stream-resolver-v2"}


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/api/stream")
def get_stream(post_id: int = Query(..., description="ID del post en la API")):
    # Serializa peticiones simultáneas del mismo título/post.
    lock = get_post_lock(post_id)
    with lock:
        return _resolve_post(post_id)


def normalize_text(value):
    if not value:
        return ""
    value = str(value).lower().strip()
    value = re.sub(r"\s*\(\d{4}\)\s*$", "", value)
    value = re.sub(r"[^a-z0-9áéíóúüñ]+", " ", value)
    return re.sub(r"\s+", " ", value).strip()


def get_tmdb_localized_title(title: str, year: str = None):
    if not TMDB_API_KEY:
        return None

    params = {
        "api_key": TMDB_API_KEY,
        "query": title,
        "language": "es-MX",
        "include_adult": "false",
    }
    if year:
        params["year"] = year

    try:
        response = requests.get(
            f"{TMDB_API_BASE}/search/movie",
            params=params,
            timeout=(4, 8),
        )
        response.raise_for_status()
        results = response.json().get("results", [])
        if results:
            return results[0].get("title")
    except Exception:
        return None
    return None


@app.get("/api/resolve_by_title")
def resolve_by_title(
    title: str = Query(..., description="Título de la película o serie"),
    year: str = Query(None, description="Año de estreno opcional"),
):
    """Busca una película por título/año y devuelve un stream fresco."""
    now = time.time()
    clean_year = year.strip()[:4] if year and year.strip() else ""
    cache_key = f"{title.lower().strip()}_{clean_year}"

    selected_post = None
    cached_search = SEARCH_CACHE.get(cache_key)
    if cached_search and cached_search["expires_at"] > now:
        selected_post = cached_search["post"]

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
            r = requests.get(search_url, headers=HEADERS, timeout=(5, 10))
            r.raise_for_status()
            data = r.json().get("data", {})
            posts = data.get("posts", [])
        except requests.RequestException as exc:
            raise HTTPException(status_code=502, detail=f"Error buscando en la API: {exc}")
        except ValueError:
            raise HTTPException(status_code=502, detail="La API de búsqueda devolvió JSON inválido")

        if not posts:
            raise HTTPException(status_code=404, detail="Título no encontrado en la base de datos")

        def post_titles(post):
            return [
                normalize_text(post.get("original_title")),
                normalize_text(post.get("title")),
                normalize_text(post.get("post_title")),
            ]

        if clean_year:
            for post in posts:
                post_year = str(post.get("release_date", ""))[:4]
                if post_year == clean_year and target_title in post_titles(post):
                    selected_post = post
                    break

        if not selected_post:
            for post in posts:
                if target_title in post_titles(post):
                    selected_post = post
                    break

        if not selected_post and clean_year:
            for post in posts:
                post_year = str(post.get("release_date", ""))[:4]
                if post_year == clean_year and any(
                    target_title in pt or pt in target_title for pt in post_titles(post) if pt
                ):
                    selected_post = post
                    break

        if not selected_post:
            raise HTTPException(
                status_code=404,
                detail=f"No se encontró una coincidencia segura para '{title}'"
                + (f" ({year})" if year else ""),
            )

        SEARCH_CACHE[cache_key] = {
            "post": selected_post,
            "expires_at": now + SEARCH_CACHE_TTL,
        }

    selected_id = selected_post.get("_id")
    if not selected_id:
        raise HTTPException(status_code=502, detail="La API encontró el título pero no devolvió _id")

    result = get_stream(post_id=selected_id)
    result["mapping"] = {
        "requested_title": title,
        "requested_year": year,
        "matched_title": (
            selected_post.get("original_title")
            or selected_post.get("title")
            or selected_post.get("post_title")
        ),
        "release_date": selected_post.get("release_date"),
        "post_id": selected_id,
    }
    return result
