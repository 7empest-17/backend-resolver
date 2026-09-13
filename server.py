import os
import re
import time
import threading
import logging

import requests
import yt_dlp
from fastapi import FastAPI, HTTPException, Query
from fastapi.middleware.cors import CORSMiddleware

app = FastAPI(title="Roku Stream Resolver Master")

logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(levelname)s | %(message)s")
logger = logging.getLogger("roku-resolver")

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


def _safe_host(url: str) -> str:
    try:
        from urllib.parse import urlparse, urljoin
        return urlparse(url).netloc or "unknown"
    except Exception:
        return "unknown"


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
    except requests.HTTPError as exc:
        logger.warning("RESOLVER vimeos | host=%s | HTTP=%s", _safe_host(url), getattr(exc.response, "status_code", "?"))
        return None
    except requests.Timeout:
        logger.warning("RESOLVER vimeos | host=%s | TIMEOUT", _safe_host(url))
        return None
    except Exception as exc:
        logger.warning("RESOLVER vimeos | host=%s | ERROR=%s", _safe_host(url), type(exc).__name__)
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
    except requests.HTTPError as exc:
        logger.warning("RESOLVER voe | host=%s | HTTP=%s", _safe_host(url), getattr(exc.response, "status_code", "?"))
        return None
    except requests.Timeout:
        logger.warning("RESOLVER voe | host=%s | TIMEOUT", _safe_host(url))
        return None
    except Exception as exc:
        logger.warning("RESOLVER voe | host=%s | ERROR=%s", _safe_host(url), type(exc).__name__)
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
    except Exception as exc:
        logger.warning("RESOLVER yt-dlp | host=%s | ERROR=%s | detalle=%s", _safe_host(url), type(exc).__name__, str(exc)[:180])
        return None


def _fetch_hls(url: str, timeout=(3, 5)):
    req_headers = HEADERS.copy()
    req_headers["Accept"] = "application/vnd.apple.mpegurl, application/x-mpegURL, */*"
    started = time.monotonic()
    r = requests.get(url, headers=req_headers, timeout=timeout)
    elapsed = (time.monotonic() - started) * 1000
    return r, elapsed


def _parse_hls_master(body: str, base_url: str):
    variants = []
    lines = [x.strip() for x in body.splitlines() if x.strip()]
    pending = None
    for line in lines:
        if line.startswith("#EXT-X-STREAM-INF:"):
            attrs = {}
            for part in re.split(r',(?=[A-Z0-9-]+=)', line.split(":", 1)[1]):
                if "=" in part:
                    k, v = part.split("=", 1)
                    attrs[k.strip()] = v.strip().strip('"')
            pending = attrs
        elif pending is not None and not line.startswith("#"):
            item = dict(pending)
            item["url"] = urljoin(base_url, line)
            variants.append(item)
            pending = None
    return variants


def _first_media_segment(body: str, base_url: str):
    for line in body.splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        return urljoin(base_url, line)
    return None


def validar_hls_playlist(stream_url: str):
    """Diagnóstico/validación ligera del master, variante y primer segmento.

    Solo descarta fallos HTTP claros (4xx/5xx) en las comprobaciones. Los
    timeouts y errores de red son no concluyentes y conservan el stream para
    no romper una reproducción que pudiera funcionar.
    """
    started = time.monotonic()
    host = _safe_host(stream_url)
    try:
        r, elapsed = _fetch_hls(stream_url)
        status = r.status_code
        content_type = r.headers.get("Content-Type", "")
        body = r.text[:200000]

        if status >= 400:
            logger.warning("HLS_CHECK | etapa=master | host=%s | HTTP=%s | content_type=%s | resultado=DESCARTAR | %.0fms", host, status, content_type[:80], elapsed)
            return False, {"estado": "http", "status": status}
        if not body.lstrip().startswith("#EXTM3U"):
            logger.warning("HLS_CHECK | etapa=master | host=%s | HTTP=%s | resultado=NO-HLS | %.0fms", host, status, elapsed)
            return False, {"estado": "contenido_no_hls", "status": status}

        is_master = "#EXT-X-STREAM-INF" in body
        variants = _parse_hls_master(body, stream_url) if is_master else []
        codecs = []
        for match in re.findall(r'CODECS="([^"]+)"', body):
            for codec in match.split(","):
                codec = codec.strip()
                if codec and codec not in codecs:
                    codecs.append(codec)
        resolutions = re.findall(r"RESOLUTION=(\d+x\d+)", body)
        max_resolution = None
        if resolutions:
            def area(value):
                try:
                    w, h = value.split("x", 1)
                    return int(w) * int(h)
                except Exception:
                    return 0
            max_resolution = max(resolutions, key=area)

        logger.info("HLS_CHECK | etapa=master | host=%s | HTTP=%s | tipo=%s | variantes=%s | max_res=%s | codecs=%s | bytes=%s | resultado=OK | %.0fms",
                    host, status, "master" if is_master else "media", len(variants), max_resolution or "?", ",".join(codecs)[:180] or "?", len(r.content), elapsed)

        child_url = None
        if variants:
            def variant_score(v):
                try:
                    w, h = v.get("RESOLUTION", "0x0").split("x", 1)
                    return int(w) * int(h)
                except Exception:
                    return 0
            variants.sort(key=variant_score, reverse=True)
            child_url = variants[0].get("url")
        else:
            child_url = stream_url

        try:
            child, child_elapsed = _fetch_hls(child_url)
            child_status = child.status_code
            child_body = child.text[:200000]
            child_type = child.headers.get("Content-Type", "")
            if child_status >= 400:
                logger.warning("HLS_CHECK | etapa=playlist | host=%s | HTTP=%s | resultado=DESCARTAR | %.0fms", _safe_host(child_url), child_status, child_elapsed)
                return False, {"estado": "playlist_http", "status": child_status}
            if not child_body.lstrip().startswith("#EXTM3U"):
                logger.warning("HLS_CHECK | etapa=playlist | host=%s | HTTP=%s | resultado=NO-HLS | %.0fms", _safe_host(child_url), child_status, child_elapsed)
                return False, {"estado": "playlist_no_hls", "status": child_status}
            segments = len(re.findall(r"#EXTINF:", child_body))
            media_segment = _first_media_segment(child_body, child_url)
            logger.info("HLS_CHECK | etapa=playlist | host=%s | HTTP=%s | segmentos=%s | content_type=%s | resultado=OK | %.0fms", _safe_host(child_url), child_status, segments, child_type[:80], child_elapsed)

            if media_segment:
                try:
                    seg_started = time.monotonic()
                    req_headers = HEADERS.copy()
                    seg = requests.get(media_segment, headers=req_headers, timeout=(3, 5), stream=True)
                    seg_elapsed = (time.monotonic() - seg_started) * 1000
                    seg_status = seg.status_code
                    seg_type = seg.headers.get("Content-Type", "")
                    seg_length = seg.headers.get("Content-Length", "?")
                    logger.info("HLS_CHECK | etapa=segmento1 | host=%s | HTTP=%s | content_type=%s | bytes=%s | resultado=%s | %.0fms",
                                _safe_host(media_segment), seg_status, seg_type[:80], seg_length,
                                "OK" if seg_status < 400 else "DESCARTAR", seg_elapsed)
                    seg.close()
                    if seg_status >= 400:
                        return False, {"estado": "segmento_http", "status": seg_status}
                except requests.Timeout:
                    logger.warning("HLS_CHECK | etapa=segmento1 | host=%s | resultado=NO-CONCLUSIVO | motivo=TIMEOUT", _safe_host(media_segment))
                except requests.RequestException as exc:
                    logger.warning("HLS_CHECK | etapa=segmento1 | host=%s | resultado=NO-CONCLUSIVO | motivo=%s", _safe_host(media_segment), type(exc).__name__)
            else:
                logger.warning("HLS_CHECK | etapa=segmento1 | host=%s | resultado=NO-SEGMENTO", _safe_host(child_url))
        except requests.Timeout:
            logger.warning("HLS_CHECK | etapa=playlist | host=%s | resultado=NO-CONCLUSIVO | motivo=TIMEOUT", _safe_host(child_url))
        except requests.RequestException as exc:
            logger.warning("HLS_CHECK | etapa=playlist | host=%s | resultado=NO-CONCLUSIVO | motivo=%s", _safe_host(child_url), type(exc).__name__)

        return True, {"estado": "ok", "status": status, "playlist_type": "master" if is_master else "media", "variants": len(variants), "max_resolution": max_resolution, "codecs": codecs}
    except requests.Timeout:
        logger.warning("HLS_CHECK | etapa=master | host=%s | resultado=NO-CONCLUSIVO | motivo=TIMEOUT | %.0fms", host, (time.monotonic()-started)*1000)
        return None, {"estado": "timeout"}
    except requests.RequestException as exc:
        logger.warning("HLS_CHECK | etapa=master | host=%s | resultado=NO-CONCLUSIVO | motivo=%s | %.0fms", host, type(exc).__name__, (time.monotonic()-started)*1000)
        return None, {"estado": "red", "error": type(exc).__name__}
    except Exception as exc:
        logger.warning("HLS_CHECK | etapa=master | host=%s | resultado=NO-CONCLUSIVO | motivo=%s | %.0fms", host, type(exc).__name__, (time.monotonic()-started)*1000)
        return None, {"estado": "error", "error": type(exc).__name__}


def obtener_peso_prioridad(embed: dict) -> int:
    raw_url = str(embed.get("url", "")).lower()
    for servidor, peso in SERVER_PRIORITY.items():
        if servidor in raw_url:
            return peso
    return 99


def procesar_un_embed(index: int, embed: dict):
    started = time.monotonic()
    raw_url = str(embed.get("url", "")).replace(r"\/", "/").strip()
    if not raw_url:
        logger.warning("EMBED %s | vacío | duración=0ms", index)
        return None

    servidor = embed.get("server", "Online")
    idioma = embed.get("lang", "Latino")
    calidad = embed.get("quality", "Full HD")
    host = _safe_host(raw_url)
    stream_url = None
    metodo = None

    try:
        if "vimeos" in raw_url:
            metodo = "vimeos"
            stream_url = resolver_vimeos(raw_url)
        elif "goodstream" in raw_url:
            metodo = "goodstream"
            stream_url = resolver_goodstream(raw_url)
        elif "voe.sx" in raw_url:
            metodo = "voe"
            stream_url = resolver_voe(raw_url)

        if stream_url:
            logger.info("EMBED %s | host=%s | método=%s | resultado=OK-específico | %.0fms", index, host, metodo, (time.monotonic()-started)*1000)
        else:
            metodo = "yt-dlp"
            stream_url = resolver_ytdlp(raw_url)
            if stream_url:
                logger.info("EMBED %s | host=%s | método=yt-dlp | resultado=OK | %.0fms", index, host, (time.monotonic()-started)*1000)
            else:
                logger.warning("EMBED %s | host=%s | método=yt-dlp | resultado=FALLO | %.0fms", index, host, (time.monotonic()-started)*1000)
    except Exception as exc:
        logger.exception("EMBED %s | host=%s | método=%s | excepción=%s | %.0fms", index, host, metodo or "desconocido", type(exc).__name__, (time.monotonic()-started)*1000)
        return None

    if not stream_url or not re.match(r"^https?://", stream_url):
        logger.warning("EMBED %s | host=%s | resultado=URL-inválida | %.0fms", index, host, (time.monotonic()-started)*1000)
        return None

    stream_lower = stream_url.lower()
    if ".m3u8" in stream_lower:
        stream_format = "hls"
    elif ".mp4" in stream_lower:
        stream_format = "mp4"
    else:
        stream_format = "mp4"

    if stream_format == "hls":
        hls_ok, hls_info = validar_hls_playlist(stream_url)
        if hls_ok is False:
            logger.warning(
                "EMBED %s | host=%s | formato=hls | resultado=DESCARTADO-POR-HLS | motivo=%s",
                index, host, hls_info.get("estado", "desconocido"),
            )
            return None
        if hls_ok is None:
            logger.info(
                "EMBED %s | host=%s | formato=hls | resultado=VALIDACIÓN-NO-CONCLUSIVA | se-conserva=SI",
                index, host,
            )

    logger.info("EMBED %s | host=%s | formato=%s | resultado=STREAM-VÁLIDO | %.0fms", index, host, stream_format, (time.monotonic()-started)*1000)
    return {
        "id": index,
        "peso": obtener_peso_prioridad(embed),
        "nombre": f"{servidor} ({idioma} - {calidad})",
        "stream_format": stream_format,
        "url": stream_url,
    }


def _resolve_post(post_id: int):
    started = time.monotonic()
    now = time.time()
    logger.info("RESOLVE_START | post_id=%s", post_id)

    cached = STREAM_CACHE.get(post_id)
    if cached and cached["expires_at"] > now:
        logger.info("RESOLVE_CACHE_HIT | post_id=%s | %.0fms", post_id, (time.monotonic()-started)*1000)
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
    logger.info("EMBEDS | post_id=%s | count=%s", post_id, len(embeds) if isinstance(embeds, list) else "invalid")
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
        logger.warning("RESOLVE_FAIL | post_id=%s | ningún stream válido | %.0fms", post_id, (time.monotonic()-started)*1000)
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

    logger.info("RESOLVE_OK | post_id=%s | streams=%s | seleccionado=%s | %.0fms", post_id, len(resultados), resultados[0].get("nombre"), (time.monotonic()-started)*1000)

    STREAM_CACHE[post_id] = {
        "data": respuesta,
        "expires_at": now + STREAM_CACHE_TTL,
    }
    return respuesta


@app.get("/")
def home():
    return {"status": "ok", "service": "roku-stream-resolver-v3-diagnostics"}


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
    started = time.monotonic()
    now = time.time()
    logger.info("TITLE_START | título=%r | año=%r", title, year)
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
            logger.warning("SEARCH_FAIL | título=%r | tipo=%s | detalle=%s", title, type(exc).__name__, str(exc)[:180])
            raise HTTPException(status_code=502, detail=f"Error buscando en la API: {exc}")
        except ValueError:
            logger.warning("SEARCH_FAIL | título=%r | JSON inválido", title)
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
    logger.info("TITLE_MATCH | solicitado=%r | encontrado=%r | post_id=%s | cache=%s", title, selected_post.get("original_title") or selected_post.get("title") or selected_post.get("post_title"), selected_post.get("_id"), bool(cached_search))

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
    selected = result.get("selected_stream", {})
    logger.info("TITLE_OK | título=%r | post_id=%s | seleccionado=%s | formato=%s | stream_host=%s | total=%s | %.0fms", title, selected_id, selected.get("nombre"), selected.get("stream_format"), _safe_host(selected.get("url", "")), result.get("total"), (time.monotonic()-started)*1000)
    return result
