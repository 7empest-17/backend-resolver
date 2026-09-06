*** Begin Patch
*** Update File: server.py
@@
 import os
 import re
 import time
+import unicodedata
+from difflib import SequenceMatcher
 from concurrent.futures import ThreadPoolExecutor, as_completed
 import requests
 import yt_dlp
@@
 CACHE_TTL = 7200
 
+def normalizar_titulo(texto: str) -> str:
+    """Normaliza títulos para comparar TMDB/LaMovie sin depender de acentos o puntuación."""
+    if not texto:
+        return ""
+    texto = unicodedata.normalize("NFKD", str(texto))
+    texto = "".join(c for c in texto if not unicodedata.combining(c))
+    texto = texto.lower()
+    texto = texto.replace("&", " and ")
+    texto = re.sub(r"[^a-z0-9]+", " ", texto)
+    return re.sub(r"\s+", " ", texto).strip()
+
+
+def obtener_anio(post: dict) -> str:
+    """Obtiene el año de release_date o de campos equivalentes."""
+    for campo in ("release_date", "first_air_date", "year"):
+        valor = post.get(campo)
+        if valor:
+            match = re.search(r"\b(19|20)\d{2}\b", str(valor))
+            if match:
+                return match.group(0)
+    return ""
+
+
+def seleccionar_post_lamovie(posts: list, title: str, year: str = None):
+    """
+    Selecciona un post de LaMovie de forma determinista.
+    Prioridad:
+      1) título original exacto + año
+      2) título mostrado exacto + año
+      3) título exacto
+      4) coincidencia difusa alta + año
+    Si se recibió año, una coincidencia difusa sin el año NO se acepta.
+    """
+    objetivo = normalizar_titulo(title)
+    objetivo_anio = str(year or "").strip()[:4]
+    candidatos = []
+
+    for post in posts:
+        post_id = post.get("_id")
+        if post_id in (None, ""):
+            continue
+
+        titulos = []
+        for campo in ("original_title", "title", "name"):
+            valor = post.get(campo)
+            if valor:
+                norm = normalizar_titulo(valor)
+                if norm and norm not in titulos:
+                    titulos.append(norm)
+
+        if not titulos:
+            continue
+
+        anio = obtener_anio(post)
+        exacto = objetivo in titulos
+        ratio = max(SequenceMatcher(None, objetivo, t).ratio() for t in titulos)
+        mismo_anio = bool(objetivo_anio and anio == objetivo_anio)
+
+        if exacto and mismo_anio:
+            score = 100
+        elif exacto and not objetivo_anio:
+            score = 90
+        elif exacto:
+            score = 70
+        elif mismo_anio and ratio >= 0.90:
+            score = 85
+        elif not objetivo_anio and ratio >= 0.94:
+            score = 75
+        else:
+            continue
+
+        candidatos.append((score, ratio, post))
+
+    if not candidatos:
+        return None
+
+    candidatos.sort(
+        key=lambda item: (item[0], item[1], str(item[2].get("_id", ""))),
+        reverse=True,
+    )
+    return candidatos[0][2]
+
@@
 @app.get("/api/resolve_by_title")
 def resolve_by_title(
     title: str = Query(..., description="Título de la película o serie"),
     year: str = Query(None, description="Año de estreno opcional")
 ):
-    """Permite a Roku pasar el nombre directamente sin conocer el post_id previamente."""
-    search_url = f"{LAMOVIE_API_BASE}/search?postType=movies&q={requests.utils.quote(title)}&postsPerPage=5"
+    """Resuelve automáticamente título+año -> post_id y luego usa el flujo existente."""
+    search_url = (
+        f"{LAMOVIE_API_BASE}/search?postType=movies"
+        f"&q={requests.utils.quote(title)}&postsPerPage=20"
+    )
@@
     if not posts:
         raise HTTPException(status_code=404, detail="Título no encontrado en la base de datos")
-    selected_id = None
-    if year:
-        for post in posts:
-            if str(post.get("release_date", "")).startswith(year):
-                selected_id = post.get("_id")
-                break
-
-    if not selected_id:
-        selected_id = posts[0].get("_id")
-
-    return get_stream(post_id=selected_id)
+
+    selected = seleccionar_post_lamovie(posts, title, year)
+    if not selected:
+        raise HTTPException(
+            status_code=404,
+            detail=f"No hubo una coincidencia segura para '{title}'"
+                   + (f" ({year})" if year else "")
+        )
+
+    selected_id = selected.get("_id")
+    if selected_id in (None, ""):
+        raise HTTPException(status_code=404, detail="La coincidencia no tiene _id válido")
+
+    respuesta = get_stream(post_id=int(selected_id))
+
+    if isinstance(respuesta, dict):
+        respuesta["match"] = {
+            "post_id": int(selected_id),
+            "title": selected.get("original_title") or selected.get("title") or selected.get("name"),
+            "release_date": selected.get("release_date"),
+        }
+    return respuesta
+
+
+@app.get("/api/stream_by_title")
+def stream_by_title(
+    title: str = Query(..., description="Título de la película"),
+    year: str = Query(None, description="Año de estreno opcional")
+):
+    """Alias explícito para que Roku no tenga que conocer ningún post_id."""
+    return resolve_by_title(title=title, year=year)
*** End Patch
