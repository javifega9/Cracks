import json
import os
import re
import threading
import time
from typing import Any
from urllib.parse import parse_qsl, urlencode, urlparse, urlunparse

import requests
import uvicorn
from fastapi import FastAPI, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from openai import OpenAI
from pydantic import BaseModel


SERPAPI_URL = "https://serpapi.com/search.json"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
SHOPPING_GL = os.getenv("SHOPPING_GL", "es")
SHOPPING_HL = os.getenv("SHOPPING_HL", "es")
CHOLLO_THRESHOLD = float(os.getenv("CHOLLO_THRESHOLD", "0.75"))
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "").strip()
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.es").strip().lower()
AMAZON_CACHE: dict[str, str | None] = {}
SEARCH_CACHE_TTL_SECONDS = int(os.getenv("SEARCH_CACHE_TTL_SECONDS", "900"))
SEARCH_CACHE_LOCK = threading.Lock()
SEARCH_CACHE: dict[str, dict[str, Any]] = {}
RATE_LIMIT_WINDOW_SECONDS = int(os.getenv("RATE_LIMIT_WINDOW_SECONDS", "300"))
RATE_LIMIT_MAX_REQUESTS = int(os.getenv("RATE_LIMIT_MAX_REQUESTS", "30"))
RATE_LIMIT_LOCK = threading.Lock()
RATE_LIMIT_BUCKETS: dict[str, list[float]] = {}

app = FastAPI(
    title="Cracks",
    description="Aplicacion web para buscar ofertas de productos con FastAPI, SerpAPI y OpenAI.",
    version="1.0.0",
)


class Product(BaseModel):
    titulo: str
    precio: str | None = None
    tienda: str | None = None
    link: str
    es_chollo: bool = False
    imagen: str | None = None
    link_original: str | None = None
    es_amazon: bool = False
    es_afiliado_amazon: bool = False


class SearchResponse(BaseModel):
    query_original: str
    query_mejorada: str
    incluir_palabras: list[str] = []
    modo_inclusion: str = "all"
    excluir_palabras: list[str] = []
    precio_medio: float | None = None
    productos: list[Product]
    top_3_mejores_opciones: list[Product]


def get_env(name: str) -> str:
    value = os.getenv(name)
    if not value:
        raise HTTPException(
            status_code=500,
            detail=f"Falta la variable de entorno {name}",
        )
    return value


def get_openai_client() -> OpenAI:
    return OpenAI(api_key=get_env("OPENAI_API_KEY"))


def get_client_identifier(request: Request) -> str:
    forwarded_for = request.headers.get("x-forwarded-for", "").split(",")[0].strip()
    if forwarded_for:
        return forwarded_for

    if request.client and request.client.host:
        return request.client.host

    return "unknown"


def enforce_rate_limit(request: Request) -> None:
    identifier = get_client_identifier(request)
    if identifier in {"127.0.0.1", "::1", "localhost"}:
        return

    now = time.time()
    window_start = now - RATE_LIMIT_WINDOW_SECONDS

    with RATE_LIMIT_LOCK:
        bucket = RATE_LIMIT_BUCKETS.get(identifier, [])
        bucket = [timestamp for timestamp in bucket if timestamp >= window_start]

        if len(bucket) >= RATE_LIMIT_MAX_REQUESTS:
            raise HTTPException(
                status_code=429,
                detail="Demasiadas busquedas en poco tiempo. Espera un momento e intentalo de nuevo.",
            )

        bucket.append(now)
        RATE_LIMIT_BUCKETS[identifier] = bucket


def extract_json(text: str) -> dict[str, Any]:
    cleaned = text.strip()
    cleaned = re.sub(r"^```json\s*", "", cleaned)
    cleaned = re.sub(r"^```\s*", "", cleaned)
    cleaned = re.sub(r"\s*```$", "", cleaned)
    return json.loads(cleaned)


def improve_query_with_openai(query: str) -> str:
    client = get_openai_client()

    prompt = f"""
Eres un asistente que mejora busquedas de productos para Google Shopping.

Quiero que tomes la busqueda del usuario y la conviertas en una busqueda mas clara y util para encontrar productos.

Reglas:
- Manten la intencion original.
- No inventes marcas ni detalles que el usuario no pidio.
- Devuelve solo JSON valido.
- Formato exacto:
  {{"improved_query": "texto"}}

Busqueda del usuario: {query}
""".strip()

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        data = extract_json(response.output_text or "")
        improved_query = data.get("improved_query", "").strip()
        return improved_query or query
    except Exception:
        return query


def extract_numeric_price(price_text: str | None) -> float | None:
    if not price_text:
        return None

    cleaned = re.sub(r"[^\d,\.]", "", str(price_text))
    if not cleaned:
        return None

    last_dot = cleaned.rfind(".")
    last_comma = cleaned.rfind(",")

    if last_dot > last_comma:
        normalized = cleaned.replace(",", "")
    elif last_comma > last_dot:
        normalized = cleaned.replace(".", "").replace(",", ".")
    else:
        normalized = cleaned.replace(",", ".")

    try:
        return float(normalized)
    except ValueError:
        return None


def split_words(text: str | None) -> list[str]:
    if not text:
        return []

    normalized = text.replace(";", ",")
    parts = [part.strip().lower() for part in normalized.split(",")]
    return [part for part in parts if part]


def filter_products_by_title(
    products: list[dict[str, Any]],
    include_words: list[str] | None = None,
    include_mode: str = "all",
    exclude_words: list[str] | None = None,
) -> list[dict[str, Any]]:
    include_words = include_words or []
    exclude_words = exclude_words or []
    include_mode = "any" if include_mode == "any" else "all"

    if not include_words and not exclude_words:
        return products

    filtered = []
    for product in products:
        title = str(product.get("titulo", "")).lower()

        if include_words:
            if include_mode == "all" and not all(word in title for word in include_words):
                continue
            if include_mode == "any" and not any(word in title for word in include_words):
                continue

        if exclude_words and any(word in title for word in exclude_words):
            continue

        filtered.append(product)

    return filtered


def is_amazon_link(url: str | None) -> bool:
    if not url:
        return False

    hostname = urlparse(url).netloc.lower()
    return "amazon." in hostname


def build_amazon_affiliate_link(url: str) -> str:
    if not AMAZON_AFFILIATE_TAG or not is_amazon_link(url):
        return url

    parsed = urlparse(url)
    query_params = dict(parse_qsl(parsed.query, keep_blank_values=True))
    query_params["tag"] = AMAZON_AFFILIATE_TAG
    new_query = urlencode(query_params, doseq=True)
    return urlunparse(parsed._replace(query=new_query))


def looks_like_amazon_product(product: dict[str, Any]) -> bool:
    store = str(product.get("tienda") or "").lower()
    link = str(product.get("link") or "")
    return "amazon" in store or is_amazon_link(link)


def search_google_web(query: str) -> dict[str, Any]:
    params = {
        "engine": "google",
        "q": query,
        "api_key": get_env("SERPAPI_KEY"),
        "gl": SHOPPING_GL,
        "hl": SHOPPING_HL,
        "num": 5,
    }

    response = requests.get(SERPAPI_URL, params=params, timeout=30)
    response.raise_for_status()
    return response.json()


def find_amazon_link_for_title(title: str) -> str | None:
    cleaned_title = " ".join(str(title).split()).strip()
    if not cleaned_title:
        return None

    cache_key = cleaned_title.lower()
    if cache_key in AMAZON_CACHE:
        return AMAZON_CACHE[cache_key]

    try:
        queries = [
            f'site:{AMAZON_DOMAIN} "{cleaned_title}"',
            f'site:{AMAZON_DOMAIN} {cleaned_title}',
        ]

        for query in queries:
            data = search_google_web(query)
            organic_results = data.get("organic_results", [])
            for item in organic_results:
                link = item.get("link")
                if isinstance(link, str) and AMAZON_DOMAIN in link.lower():
                    final_link = build_amazon_affiliate_link(link)
                    AMAZON_CACHE[cache_key] = final_link
                    return final_link
    except requests.RequestException:
        pass

    AMAZON_CACHE[cache_key] = None
    return None


def enrich_products_with_amazon(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    if not products or not AMAZON_AFFILIATE_TAG:
        return products

    for product in products:
        original_link = str(product.get("link") or "")
        product["link_original"] = original_link
        product["es_amazon"] = False
        product["es_afiliado_amazon"] = False

        if looks_like_amazon_product(product):
            affiliate_link = build_amazon_affiliate_link(original_link)
            product["link"] = affiliate_link
            product["tienda"] = "Amazon"
            product["es_amazon"] = True
            product["es_afiliado_amazon"] = affiliate_link != original_link
            continue

        amazon_link = find_amazon_link_for_title(product.get("titulo", ""))
        if amazon_link:
            product["link"] = amazon_link
            product["tienda"] = "Amazon"
            product["es_amazon"] = True
            product["es_afiliado_amazon"] = True

    return products


def search_google_shopping(query: str) -> list[dict[str, Any]]:
    params = {
        "engine": "google_shopping",
        "q": query,
        "api_key": get_env("SERPAPI_KEY"),
        "gl": SHOPPING_GL,
        "hl": SHOPPING_HL,
    }

    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail="Error al consultar SerpAPI",
        ) from exc

    data = response.json()
    raw_products = data.get("shopping_results", [])

    products = []
    for item in raw_products[:12]:
        title = item.get("title")
        price = item.get("price")
        store = item.get("source") or item.get("merchant_name")
        link = item.get("link") or item.get("product_link")
        numeric_price = item.get("extracted_price")
        image = item.get("thumbnail") or item.get("product_thumbnail")

        if not isinstance(numeric_price, (int, float)):
            numeric_price = extract_numeric_price(price)

        if not title or not link:
            continue

        products.append(
            {
                "titulo": title,
                "precio": price,
                "tienda": store,
                "link": link,
                "precio_numerico": numeric_price,
                "es_chollo": False,
                "imagen": image,
                "link_original": link,
                "es_amazon": False,
                "es_afiliado_amazon": False,
            }
        )

    return products


def mark_bargains(products: list[dict[str, Any]]) -> float | None:
    numeric_prices = [
        float(product["precio_numerico"])
        for product in products
        if isinstance(product.get("precio_numerico"), (int, float))
    ]

    if not numeric_prices:
        return None

    average_price = round(sum(numeric_prices) / len(numeric_prices), 2)
    bargain_limit = average_price * CHOLLO_THRESHOLD

    for product in products:
        numeric_price = product.get("precio_numerico")
        product["es_chollo"] = bool(
            isinstance(numeric_price, (int, float)) and numeric_price <= bargain_limit
        )

    return average_price


def fallback_top_3(products: list[dict[str, Any]]) -> list[dict[str, Any]]:
    def sort_key(product: dict[str, Any]) -> tuple[Any, Any, Any]:
        bargain_score = 0 if product.get("es_chollo") else 1
        price = product.get("precio_numerico")
        if isinstance(price, (int, float)):
            return (bargain_score, 0, price)
        return (bargain_score, 1, float("inf"))

    return sorted(products, key=sort_key)[:3]


def choose_top_3_with_openai(
    original_query: str,
    improved_query: str,
    average_price: float | None,
    products: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    if not products:
        return []

    client = get_openai_client()

    simplified_products = []
    for index, product in enumerate(products):
        simplified_products.append(
            {
                "index": index,
                "titulo": product["titulo"],
                "precio": product["precio"],
                "tienda": product["tienda"],
                "es_chollo": product.get("es_chollo", False),
                "es_amazon": product.get("es_amazon", False),
            }
        )

    prompt = f"""
Eres un asistente que elige las 3 mejores opciones de compra.

Debes priorizar:
- relevancia con la busqueda
- relacion calidad/precio
- claridad del resultado
- si un producto esta marcado como chollo, dale valor extra

Devuelve solo JSON valido con este formato exacto:
{{"top_indices": [0, 1, 2]}}

Busqueda original: {original_query}
Busqueda mejorada: {improved_query}
Precio medio aproximado: {average_price}
Productos: {json.dumps(simplified_products, ensure_ascii=False)}
""".strip()

    try:
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        data = extract_json(response.output_text or "")
        indices = data.get("top_indices", [])

        selected = []
        seen = set()
        for index in indices:
            if isinstance(index, int) and 0 <= index < len(products) and index not in seen:
                selected.append(products[index])
                seen.add(index)

        if selected:
            return selected[:3]
    except Exception:
        pass

    return fallback_top_3(products)


def clean_product(product: dict[str, Any]) -> Product:
    return Product(
        titulo=product["titulo"],
        precio=product.get("precio"),
        tienda=product.get("tienda"),
        link=product["link"],
        es_chollo=bool(product.get("es_chollo", False)),
        imagen=product.get("imagen"),
        link_original=product.get("link_original"),
        es_amazon=bool(product.get("es_amazon", False)),
        es_afiliado_amazon=bool(product.get("es_afiliado_amazon", False)),
    )


def model_to_dict(model: BaseModel) -> dict[str, Any]:
    if hasattr(model, "model_dump"):
        return model.model_dump()
    return dict(model.dict())


def build_search_cache_key(
    query: str,
    include_words: list[str],
    include_mode: str,
    exclude_words: list[str],
) -> str:
    payload = {
        "query": query.strip().lower(),
        "include_words": include_words,
        "include_mode": include_mode,
        "exclude_words": exclude_words,
    }
    return json.dumps(payload, ensure_ascii=True, sort_keys=True)


def get_cached_search_result(cache_key: str) -> SearchResponse | None:
    now = time.time()
    with SEARCH_CACHE_LOCK:
        cached = SEARCH_CACHE.get(cache_key)
        if not cached:
            return None

        if now - cached["timestamp"] > SEARCH_CACHE_TTL_SECONDS:
            SEARCH_CACHE.pop(cache_key, None)
            return None

        return SearchResponse(**cached["data"])


def set_cached_search_result(cache_key: str, result: SearchResponse) -> None:
    with SEARCH_CACHE_LOCK:
        SEARCH_CACHE[cache_key] = {
            "timestamp": time.time(),
            "data": model_to_dict(result),
        }


def run_search_logic(
    query: str,
    include_words: list[str] | None = None,
    include_mode: str = "all",
    exclude_words: list[str] | None = None,
) -> SearchResponse:
    include_words = include_words or []
    exclude_words = exclude_words or []
    include_mode = "any" if include_mode == "any" else "all"
    cache_key = build_search_cache_key(query, include_words, include_mode, exclude_words)
    cached = get_cached_search_result(cache_key)
    if cached:
        return cached

    improved_query = improve_query_with_openai(query)
    products = search_google_shopping(improved_query)
    products = filter_products_by_title(products, include_words, include_mode, exclude_words)
    products = enrich_products_with_amazon(products)
    average_price = mark_bargains(products)
    top_3 = choose_top_3_with_openai(query, improved_query, average_price, products)

    result = SearchResponse(
        query_original=query,
        query_mejorada=improved_query,
        incluir_palabras=include_words,
        modo_inclusion=include_mode,
        excluir_palabras=exclude_words,
        precio_medio=average_price,
        productos=[clean_product(product) for product in products],
        top_3_mejores_opciones=[clean_product(product) for product in top_3],
    )
    set_cached_search_result(cache_key, result)
    return result


def build_home_page() -> str:
    return """
<!DOCTYPE html>
<html lang="es">
<head>
    <meta charset="UTF-8">
    <meta name="viewport" content="width=device-width, initial-scale=1.0">
    <title>Cracks</title>
    <style>
        :root {
            --bg: #f4efe7;
            --panel: rgba(255, 252, 247, 0.82);
            --panel-strong: #fffdf9;
            --text: #18202b;
            --muted: #677283;
            --line: rgba(142, 111, 73, 0.18);
            --primary: #c76d2b;
            --primary-dark: #9c4f18;
            --primary-soft: #fff0df;
            --accent: #103d60;
            --accent-soft: #e8f2fb;
            --highlight: #fff4e7;
            --soft-green: #e8f7ea;
            --soft-green-border: #9fd2a7;
            --shadow: 0 24px 60px rgba(31, 27, 22, 0.10);
            --shadow-soft: 0 16px 34px rgba(31, 27, 22, 0.06);
        }

        * {
            box-sizing: border-box;
        }

        body {
            margin: 0;
            font-family: "Segoe UI", Tahoma, Geneva, Verdana, sans-serif;
            color: var(--text);
            background:
                radial-gradient(circle at top left, rgba(255, 214, 171, 0.45) 0, transparent 28%),
                radial-gradient(circle at 85% 10%, rgba(188, 219, 255, 0.55) 0, transparent 22%),
                linear-gradient(180deg, #fbf7f2 0%, var(--bg) 100%);
            min-height: 100vh;
        }

        .wrap {
            width: min(1180px, calc(100% - 32px));
            margin: 0 auto;
            padding: 28px 0 72px;
        }

        .topbar {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 22px;
        }

        .brand {
            display: flex;
            align-items: center;
            gap: 14px;
        }

        .brand-mark {
            width: 52px;
            height: 52px;
            border-radius: 16px;
            display: grid;
            place-items: center;
            color: white;
            font-weight: 800;
            font-size: 1.25rem;
            background:
                linear-gradient(135deg, #0c3a59 0%, #1b5f8f 55%, #f0893d 100%);
            box-shadow: 0 12px 24px rgba(16, 61, 96, 0.24);
        }

        .brand-copy strong {
            display: block;
            font-size: 1.05rem;
            letter-spacing: -0.02em;
        }

        .brand-copy span {
            color: var(--muted);
            font-size: 0.95rem;
        }

        .topbar-note {
            color: var(--muted);
            font-size: 0.95rem;
            background: rgba(255, 255, 255, 0.58);
            border: 1px solid var(--line);
            border-radius: 999px;
            padding: 10px 14px;
            backdrop-filter: blur(10px);
        }

        .hero {
            position: relative;
            overflow: hidden;
            background: linear-gradient(180deg, rgba(255, 254, 251, 0.9) 0%, rgba(255, 250, 243, 0.95) 100%);
            border: 1px solid var(--line);
            border-radius: 34px;
            padding: 34px;
            box-shadow: var(--shadow);
            backdrop-filter: blur(14px);
        }

        .hero::before {
            content: "";
            position: absolute;
            width: 340px;
            height: 340px;
            right: -90px;
            top: -90px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(14, 73, 115, 0.14) 0%, transparent 65%);
            pointer-events: none;
        }

        .hero::after {
            content: "";
            position: absolute;
            width: 240px;
            height: 240px;
            left: -60px;
            bottom: -120px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(199, 109, 43, 0.12) 0%, transparent 65%);
            pointer-events: none;
        }

        .hero-grid {
            position: relative;
            z-index: 1;
            display: grid;
            grid-template-columns: minmax(0, 1.25fr) minmax(300px, 0.75fr);
            gap: 24px;
            align-items: start;
        }

        .eyebrow {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 8px 12px;
            border-radius: 999px;
            background: var(--accent-soft);
            color: var(--accent);
            font-size: 0.86rem;
            font-weight: 700;
            letter-spacing: 0.01em;
            margin-bottom: 16px;
        }

        h1 {
            margin: 0 0 14px;
            font-size: clamp(2.6rem, 5vw, 4.9rem);
            line-height: 0.92;
            letter-spacing: -0.04em;
            max-width: 700px;
        }

        .subtitle {
            margin: 0 0 22px;
            color: var(--muted);
            font-size: 1.08rem;
            line-height: 1.6;
            max-width: 700px;
        }

        .hero-points {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
            margin-bottom: 24px;
        }

        .hero-point {
            background: rgba(255, 255, 255, 0.66);
            border: 1px solid var(--line);
            border-radius: 18px;
            padding: 14px 16px;
            box-shadow: var(--shadow-soft);
        }

        .hero-point strong {
            display: block;
            font-size: 0.96rem;
            margin-bottom: 6px;
        }

        .hero-point span {
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.45;
        }

        .search-shell {
            background: rgba(255, 255, 255, 0.72);
            border: 1px solid var(--line);
            border-radius: 26px;
            padding: 18px;
            box-shadow: var(--shadow-soft);
            backdrop-filter: blur(10px);
        }

        .search-bar {
            display: grid;
            grid-template-columns: 1fr auto auto;
            gap: 12px;
        }

        .filters {
            display: grid;
            grid-template-columns: 1fr 220px 1fr;
            gap: 12px;
            margin-top: 12px;
        }

        select {
            width: 100%;
            padding: 16px 18px;
            border-radius: 18px;
            border: 1px solid var(--line);
            font-size: 1rem;
            outline: none;
            background: rgba(255, 255, 255, 0.96);
        }

        select:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 4px rgba(196, 107, 45, 0.12);
        }

        input[type="text"] {
            width: 100%;
            padding: 16px 18px;
            border-radius: 18px;
            border: 1px solid var(--line);
            font-size: 1rem;
            outline: none;
            background: rgba(255, 255, 255, 0.96);
        }

        input[type="text"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 4px rgba(196, 107, 45, 0.12);
        }

        button {
            border: 0;
            border-radius: 18px;
            padding: 0 22px;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            min-height: 56px;
            transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
        }

        .primary-button {
            background: linear-gradient(135deg, var(--primary) 0%, #e88d45 100%);
            color: white;
            box-shadow: 0 14px 24px rgba(199, 109, 43, 0.22);
        }

        .primary-button:hover {
            background: linear-gradient(135deg, var(--primary-dark) 0%, #d97831 100%);
            transform: translateY(-1px);
        }

        .secondary-button {
            background: rgba(255, 255, 255, 0.92);
            color: var(--text);
            border: 1px solid var(--line);
        }

        .secondary-button:hover {
            background: white;
            transform: translateY(-1px);
        }

        .examples {
            display: flex;
            flex-wrap: wrap;
            gap: 10px;
            margin-top: 16px;
        }

        .example-chip {
            border: 1px solid var(--line);
            background: rgba(255, 255, 255, 0.9);
            color: var(--text);
            padding: 10px 14px;
            border-radius: 999px;
            cursor: pointer;
            font-size: 0.95rem;
            transition: transform 0.18s ease, background 0.18s ease, border-color 0.18s ease;
        }

        .example-chip:hover {
            transform: translateY(-1px);
            background: white;
            border-color: rgba(199, 109, 43, 0.28);
        }

        .status {
            margin-top: 18px;
            color: var(--muted);
            min-height: 24px;
            font-size: 0.95rem;
        }

        .meta {
            display: none;
            margin-top: 24px;
            background: linear-gradient(180deg, var(--highlight) 0%, #fffaf2 100%);
            border: 1px solid #f2d5a4;
            border-radius: 22px;
            padding: 18px 20px;
            box-shadow: var(--shadow-soft);
        }

        .meta strong {
            display: block;
            margin-bottom: 8px;
            color: #8a4416;
        }

        .hero-side {
            display: grid;
            gap: 14px;
        }

        .info-card {
            background: linear-gradient(180deg, rgba(15, 47, 74, 0.96) 0%, rgba(23, 71, 106, 0.94) 100%);
            color: white;
            border-radius: 28px;
            padding: 24px;
            box-shadow: 0 20px 46px rgba(16, 61, 96, 0.22);
        }

        .info-card small {
            display: inline-block;
            margin-bottom: 12px;
            padding: 7px 10px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.12);
            font-weight: 700;
            letter-spacing: 0.01em;
        }

        .info-card h3 {
            margin: 0 0 10px;
            font-size: 1.45rem;
            letter-spacing: -0.03em;
        }

        .info-card p {
            margin: 0;
            color: rgba(255, 255, 255, 0.78);
            line-height: 1.6;
            font-size: 0.97rem;
        }

        .mini-stats {
            display: grid;
            grid-template-columns: repeat(2, minmax(0, 1fr));
            gap: 12px;
        }

        .mini-stat {
            background: rgba(255, 255, 255, 0.76);
            border: 1px solid var(--line);
            border-radius: 22px;
            padding: 18px;
            box-shadow: var(--shadow-soft);
        }

        .mini-stat strong {
            display: block;
            font-size: 1.45rem;
            letter-spacing: -0.03em;
            margin-bottom: 6px;
        }

        .mini-stat span {
            color: var(--muted);
            font-size: 0.92rem;
            line-height: 1.45;
        }

        .section {
            margin-top: 30px;
            display: none;
        }

        .section-header {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            margin-bottom: 14px;
        }

        .section h2 {
            margin: 0;
            font-size: 1.45rem;
            letter-spacing: -0.03em;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
        }

        .saved-grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(280px, 1fr));
            gap: 16px;
        }

        .card {
            background: rgba(255, 255, 255, 0.96);
            border: 1px solid var(--line);
            border-radius: 24px;
            padding: 20px;
            box-shadow: var(--shadow-soft);
            display: flex;
            flex-direction: column;
            gap: 14px;
        }

        .top-card {
            border-color: rgba(239, 179, 109, 0.95);
            background: linear-gradient(180deg, #fff6eb 0%, #fffdf9 100%);
        }

        .badge-row {
            display: flex;
            flex-wrap: wrap;
            gap: 8px;
            margin-bottom: 10px;
        }

        .badge {
            display: inline-block;
            padding: 6px 10px;
            border-radius: 999px;
            background: #ffedd5;
            color: #9a3412;
            font-size: 0.78rem;
            font-weight: 700;
        }

        .bargain-badge {
            background: var(--soft-green);
            color: #166534;
            border: 1px solid var(--soft-green-border);
        }

        .title {
            font-size: 1.02rem;
            font-weight: 700;
            line-height: 1.45;
        }

        .price {
            font-size: 1.2rem;
            font-weight: 800;
        }

        .store,
        .saved-meta {
            color: var(--muted);
            margin-bottom: 0;
        }

        .card a {
            color: var(--primary-dark);
            text-decoration: none;
            font-weight: 700;
        }

        .card a:hover {
            text-decoration: underline;
        }

        .product-media {
            aspect-ratio: 4 / 3;
            border-radius: 18px;
            overflow: hidden;
            background:
                linear-gradient(135deg, rgba(16, 61, 96, 0.08) 0%, rgba(199, 109, 43, 0.08) 100%),
                #f8f3ec;
            border: 1px solid rgba(142, 111, 73, 0.12);
            display: grid;
            place-items: center;
        }

        .product-media img {
            width: 100%;
            height: 100%;
            object-fit: cover;
            display: block;
        }

        .product-media-fallback {
            color: var(--muted);
            font-size: 0.92rem;
            text-align: center;
            padding: 18px;
            line-height: 1.5;
        }

        .card-content {
            display: flex;
            flex-direction: column;
            gap: 8px;
            flex: 1;
        }

        .card-footer {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 14px;
            margin-top: auto;
        }

        .card-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 44px;
            padding: 0 16px;
            border-radius: 14px;
            background: var(--primary-soft);
            border: 1px solid rgba(199, 109, 43, 0.18);
            text-decoration: none;
            white-space: nowrap;
        }

        .card-link:hover {
            text-decoration: none;
            background: #ffe8cd;
        }

        .link-note {
            color: var(--muted);
            font-size: 0.82rem;
            line-height: 1.45;
        }

        .saved-actions {
            display: flex;
            gap: 10px;
            flex-wrap: wrap;
        }

        .small-button {
            min-height: auto;
            padding: 10px 14px;
            border-radius: 12px;
            font-size: 0.9rem;
        }

        @media (max-width: 860px) {
            .topbar {
                flex-direction: column;
                align-items: flex-start;
            }

            .hero-grid {
                grid-template-columns: 1fr;
            }

            .hero-points {
                grid-template-columns: 1fr;
            }

            .mini-stats {
                grid-template-columns: 1fr 1fr;
            }

            .search-bar {
                grid-template-columns: 1fr;
            }

            .filters {
                grid-template-columns: 1fr;
            }

            button {
                width: 100%;
            }

            .section-header {
                align-items: stretch;
                flex-direction: column;
            }
        }

        @media (max-width: 640px) {
            .wrap {
                width: min(100% - 18px, 1180px);
                padding-top: 18px;
            }

            .hero {
                padding: 22px;
                border-radius: 26px;
            }

            .mini-stats {
                grid-template-columns: 1fr;
            }

            h1 {
                font-size: clamp(2.3rem, 10vw, 3.4rem);
            }
        }
    </style>
</head>
<body>
    <div class="wrap">
        <div class="topbar">
            <div class="brand">
                <div class="brand-mark">C</div>
                <div class="brand-copy">
                    <strong>Cracks</strong>
                    <span>Buscador inteligente de productos y ofertas</span>
                </div>
            </div>
            <div class="topbar-note">Google Shopping + OpenAI + filtros utiles en una sola pantalla</div>
        </div>

        <section class="hero">
            <div class="hero-grid">
                <div>
                    <div class="eyebrow">Comparador agil para encontrar producto, precio y tienda</div>
                    <h1>Encuentra compras inteligentes sin perder tiempo.</h1>
                    <p class="subtitle">
                        Cracks busca productos en Google Shopping, mejora la consulta con OpenAI,
                        aplica filtros por palabras, detecta posibles chollos y te deja guardar tus
                        busquedas favoritas en el navegador para volver a revisarlas cuando quieras.
                    </p>

                    <div class="hero-points">
                        <div class="hero-point">
                            <strong>Busqueda afinada</strong>
                            <span>Incluye o excluye palabras del titulo para quitar ruido y ver solo lo que importa.</span>
                        </div>
                        <div class="hero-point">
                            <strong>Mejores opciones</strong>
                            <span>Top 3 priorizado por relevancia, claridad y relacion calidad precio.</span>
                        </div>
                        <div class="hero-point">
                            <strong>Guardado simple</strong>
                            <span>Tus busquedas se quedan en este navegador, ideal para Render gratis.</span>
                        </div>
                    </div>

                    <div class="search-shell">
                        <div class="search-bar">
                            <input id="query" type="text" placeholder="Ejemplo: iphone 15, cafetera nespresso, portatil lenovo barato">
                            <button id="searchButton" class="primary-button">Buscar</button>
                            <button id="saveButton" class="secondary-button" type="button">Guardar busqueda</button>
                        </div>

                        <div class="filters">
                            <input id="includeWords" type="text" placeholder="Incluir palabras. Ejemplo: 128gb, pro">
                            <select id="includeMode">
                                <option value="all">Debe tener todas</option>
                                <option value="any">Puede tener cualquiera</option>
                            </select>
                            <input id="excludeWords" type="text" placeholder="Excluir palabras. Ejemplo: funda, reacondicionado">
                        </div>

                        <div class="examples">
                            <button class="example-chip" type="button" data-query="iphone 15">iphone 15</button>
                            <button class="example-chip" type="button" data-query="portatil lenovo barato">portatil lenovo barato</button>
                            <button class="example-chip" type="button" data-query="cafetera nespresso">cafetera nespresso</button>
                        </div>

                        <div class="status" id="status">Listo para buscar.</div>
                        <div class="meta" id="meta"></div>
                    </div>
                </div>

                <div class="hero-side">
                    <div class="info-card">
                        <small>Cracks selecciona mejor</small>
                        <h3>Una portada de comparador, no de experimento.</h3>
                        <p>
                            Pensada para abrir, escribir una busqueda y encontrar opciones claras en segundos.
                            Sin menus confusos, sin pasos innecesarios y con una lectura muy rapida de precio,
                            tienda y oportunidad.
                        </p>
                    </div>

                    <div class="mini-stats">
                        <div class="mini-stat">
                            <strong>Top 3</strong>
                            <span>Las mejores opciones aparecen destacadas para decidir mas rapido.</span>
                        </div>
                        <div class="mini-stat">
                            <strong>Chollos</strong>
                            <span>Se marcan las ofertas que caen claramente por debajo del precio medio.</span>
                        </div>
                        <div class="mini-stat">
                            <strong>Filtros</strong>
                            <span>Controla el titulo del producto con incluir, excluir y modo flexible.</span>
                        </div>
                        <div class="mini-stat">
                            <strong>Gratis</strong>
                            <span>Las busquedas guardadas viven en tu navegador, perfecto para este despliegue.</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <section class="section" id="topSection">
            <div class="section-header">
                <h2>Top 3 mejores opciones</h2>
            </div>
            <div class="grid" id="topGrid"></div>
        </section>

        <section class="section" id="allSection">
            <div class="section-header">
                <h2>Todos los productos encontrados</h2>
            </div>
            <div class="grid" id="allGrid"></div>
        </section>

        <section class="section" id="savedSection" style="display:block;">
            <div class="section-header">
                <h2>Busquedas guardadas en este navegador</h2>
                <button id="reviewSavedButton" class="secondary-button" type="button">Revisar ahora</button>
            </div>
            <div class="saved-grid" id="savedGrid"></div>
        </section>

        <div class="status" style="margin-top: 22px;">
            Transparencia: algunos enlaces pueden ser enlaces de afiliado. Si compras a traves de ellos, Cracks podria recibir una comision sin coste extra para ti.
        </div>
    </div>

    <script>
        const LOCAL_STORAGE_KEY = "cracks_saved_searches";
        const queryInput = document.getElementById("query");
        const includeWordsInput = document.getElementById("includeWords");
        const includeModeInput = document.getElementById("includeMode");
        const excludeWordsInput = document.getElementById("excludeWords");
        const searchButton = document.getElementById("searchButton");
        const saveButton = document.getElementById("saveButton");
        const reviewSavedButton = document.getElementById("reviewSavedButton");
        const statusBox = document.getElementById("status");
        const metaBox = document.getElementById("meta");
        const topSection = document.getElementById("topSection");
        const allSection = document.getElementById("allSection");
        const topGrid = document.getElementById("topGrid");
        const allGrid = document.getElementById("allGrid");
        const savedGrid = document.getElementById("savedGrid");
        let latestSearchData = null;

        function escapeHtml(text) {
            return String(text ?? "")
                .replaceAll("&", "&amp;")
                .replaceAll("<", "&lt;")
                .replaceAll(">", "&gt;")
                .replaceAll('"', "&quot;")
                .replaceAll("'", "&#39;");
        }

        function todayKey() {
            return new Date().toISOString().slice(0, 10);
        }

        function nowLocalIso() {
            return new Date().toLocaleString();
        }

        function getLocalSavedSearches() {
            try {
                const raw = localStorage.getItem(LOCAL_STORAGE_KEY);
                const parsed = raw ? JSON.parse(raw) : [];
                return Array.isArray(parsed) ? parsed : [];
            } catch (error) {
                return [];
            }
        }

        function setLocalSavedSearches(items) {
            localStorage.setItem(LOCAL_STORAGE_KEY, JSON.stringify(items));
        }

        function upsertLocalSavedSearch(item) {
            const items = getLocalSavedSearches();
            const key = JSON.stringify([
                item.query_original || "",
                (item.incluir_palabras || []).join(","),
                item.modo_inclusion || "all",
                (item.excluir_palabras || []).join(",")
            ]);
            const nextItems = items.filter((saved) => {
                const savedKey = JSON.stringify([
                    saved.query_original || "",
                    (saved.incluir_palabras || []).join(","),
                    saved.modo_inclusion || "all",
                    (saved.excluir_palabras || []).join(",")
                ]);
                return savedKey !== key;
            });
            nextItems.push(item);
            nextItems.sort((a, b) => (a.query_original || "").localeCompare(b.query_original || ""));
            setLocalSavedSearches(nextItems);
        }

        function renderCard(product, rank) {
            const badges = [];
            if (rank) {
                badges.push(`<div class="badge">Top ${rank}</div>`);
            }
            if (product.es_chollo) {
                badges.push(`<div class="badge bargain-badge">Chollo</div>`);
            }
            if (product.es_amazon) {
                badges.push(`<div class="badge">Amazon</div>`);
            }
            if (product.es_afiliado_amazon) {
                badges.push(`<div class="badge">Afiliado</div>`);
            }

            const imageMarkup = product.imagen
                ? `<img src="${escapeHtml(product.imagen)}" alt="${escapeHtml(product.titulo)}">`
                : `<div class="product-media-fallback">Vista previa no disponible</div>`;

            const linkLabel = product.es_amazon ? "Ver en Amazon" : "Ver producto";
            const linkNote = product.es_afiliado_amazon
                ? "Enlace de Amazon con tu tag de afiliado."
                : product.link_original && product.link_original !== product.link
                    ? "Se encontro una opcion equivalente en Amazon."
                    : "Enlace directo al producto encontrado.";

            return `
                <article class="card ${rank ? "top-card" : ""}">
                    <div class="badge-row">${badges.join("")}</div>
                    <div class="product-media">${imageMarkup}</div>
                    <div class="card-content">
                        <div class="title">${escapeHtml(product.titulo)}</div>
                        <div class="price">${escapeHtml(product.precio || "Precio no disponible")}</div>
                        <div class="store">${escapeHtml(product.tienda || "Tienda no disponible")}</div>
                    </div>
                    <div class="card-footer">
                        <div class="link-note">${escapeHtml(linkNote)}</div>
                        <a class="card-link" href="${escapeHtml(product.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(linkLabel)}</a>
                    </div>
                </article>
            `;
        }

        function renderSavedSearch(item) {
            const queryJs = JSON.stringify(item.query_original || "");
            const includeJs = JSON.stringify((item.incluir_palabras || []).join(", "));
            const includeModeJs = JSON.stringify(item.modo_inclusion || "all");
            const excludeJs = JSON.stringify((item.excluir_palabras || []).join(", "));

            return `
                <article class="card">
                    <div class="title">${escapeHtml(item.query_original)}</div>
                    <div class="saved-meta">Mejorada: ${escapeHtml(item.query_mejorada)}</div>
                    <div class="saved-meta">Incluir: ${escapeHtml((item.incluir_palabras || []).join(", ") || "Nada")}</div>
                    <div class="saved-meta">Modo incluir: ${escapeHtml(item.modo_inclusion === "any" ? "Cualquiera" : "Todas")}</div>
                    <div class="saved-meta">Excluir: ${escapeHtml((item.excluir_palabras || []).join(", ") || "Nada")}</div>
                    <div class="saved-meta">Ultima revision: ${escapeHtml(item.ultima_revision)}</div>
                    <div class="saved-meta">Productos: ${escapeHtml(item.total_productos)}</div>
                    <div class="saved-meta">Chollos detectados: ${escapeHtml(item.chollos_detectados)}</div>
                    <div class="saved-actions">
                        <button class="secondary-button small-button" type="button" onclick='repeatSearch(${queryJs}, ${includeJs}, ${includeModeJs}, ${excludeJs})'>Buscar</button>
                        <button class="secondary-button small-button" type="button" onclick='saveCurrentQuery(${queryJs}, ${includeJs}, ${includeModeJs}, ${excludeJs})'>Actualizar guardada</button>
                    </div>
                </article>
            `;
        }

        async function loadSavedSearches() {
            const items = getLocalSavedSearches();

            if (!items.length) {
                savedGrid.innerHTML = `<article class="card"><div class="title">No hay busquedas guardadas todavia.</div><div class="saved-meta">Haz una busqueda y pulsa Guardar busqueda.</div><div class="saved-meta">Se guardan en este navegador, asi que funcionan bien en Render gratis.</div></article>`;
                return;
            }

            savedGrid.innerHTML = items.map(renderSavedSearch).join("");
        }

        async function doSearch() {
            const query = queryInput.value.trim();
            const includeWords = includeWordsInput.value.trim();
            const includeMode = includeModeInput.value;
            const excludeWords = excludeWordsInput.value.trim();

            if (!query) {
                statusBox.textContent = "Escribe algo para buscar.";
                return;
            }

            statusBox.textContent = "Buscando productos...";
            metaBox.style.display = "none";
            topSection.style.display = "none";
            allSection.style.display = "none";
            topGrid.innerHTML = "";
            allGrid.innerHTML = "";

            try {
                const params = new URLSearchParams({
                    query: query,
                    include_words: includeWords,
                    include_mode: includeMode,
                    exclude_words: excludeWords
                });
                const response = await fetch(`/search?${params.toString()}`);
                const data = await response.json();

                if (!response.ok) {
                    throw new Error(data.detail || "Ha ocurrido un error.");
                }

                latestSearchData = data;
                const chollos = data.productos.filter((product) => product.es_chollo).length;
                const average = data.precio_medio ? `${data.precio_medio} EUR` : "No disponible";

                metaBox.innerHTML = `
                    <strong>Resumen de la busqueda</strong>
                    <div><b>Original:</b> ${escapeHtml(data.query_original)}</div>
                    <div><b>Mejorada:</b> ${escapeHtml(data.query_mejorada)}</div>
                    <div><b>Incluir:</b> ${escapeHtml((data.incluir_palabras || []).join(", ") || "Nada")}</div>
                    <div><b>Modo incluir:</b> ${escapeHtml(data.modo_inclusion === "any" ? "Cualquiera" : "Todas")}</div>
                    <div><b>Excluir:</b> ${escapeHtml((data.excluir_palabras || []).join(", ") || "Nada")}</div>
                    <div><b>Precio medio aproximado:</b> ${escapeHtml(average)}</div>
                    <div><b>Chollos detectados:</b> ${escapeHtml(chollos)}</div>
                `;
                metaBox.style.display = "block";

                topGrid.innerHTML = data.top_3_mejores_opciones
                    .map((product, index) => renderCard(product, index + 1))
                    .join("");

                allGrid.innerHTML = data.productos
                    .map((product) => renderCard(product))
                    .join("");

                topSection.style.display = data.top_3_mejores_opciones.length ? "block" : "none";
                allSection.style.display = data.productos.length ? "block" : "none";

                if (!data.productos.length) {
                    statusBox.textContent = "No se encontraron productos para esa busqueda.";
                } else {
                    statusBox.textContent = `Busqueda completada. Productos encontrados: ${data.productos.length}`;
                }
            } catch (error) {
                statusBox.textContent = `Error: ${error.message}`;
            }
        }

        async function saveCurrentQuery(customQuery, customIncludeWords, customIncludeMode, customExcludeWords) {
            const query = (customQuery || queryInput.value).trim();
            const includeWords = (customIncludeWords ?? includeWordsInput.value).trim();
            const includeMode = customIncludeMode ?? includeModeInput.value;
            const excludeWords = (customExcludeWords ?? excludeWordsInput.value).trim();

            if (!query) {
                statusBox.textContent = "Primero escribe algo para guardar.";
                return;
            }

            statusBox.textContent = "Guardando busqueda...";

            try {
                let data = latestSearchData;

                const latestMatchesCurrent =
                    data &&
                    data.query_original === query &&
                    (data.incluir_palabras || []).join(", ") === includeWords &&
                    (data.modo_inclusion || "all") === includeMode &&
                    (data.excluir_palabras || []).join(", ") === excludeWords;

                if (!latestMatchesCurrent) {
                    const params = new URLSearchParams({
                        query: query,
                        include_words: includeWords,
                        include_mode: includeMode,
                        exclude_words: excludeWords
                    });
                    const response = await fetch(`/search?${params.toString()}`);
                    data = await response.json();

                    if (!response.ok) {
                        throw new Error(data.detail || "No se pudo guardar la busqueda.");
                    }
                }

                const item = {
                    query_original: data.query_original,
                    query_mejorada: data.query_mejorada,
                    incluir_palabras: data.incluir_palabras || [],
                    modo_inclusion: data.modo_inclusion || "all",
                    excluir_palabras: data.excluir_palabras || [],
                    guardada_en: nowLocalIso(),
                    ultima_revision: nowLocalIso(),
                    ultima_revision_dia: todayKey(),
                    total_productos: (data.productos || []).length,
                    chollos_detectados: (data.productos || []).filter((product) => product.es_chollo).length
                };

                upsertLocalSavedSearch(item);
                latestSearchData = data;
                statusBox.textContent = `Busqueda guardada en este navegador. Ultima revision: ${item.ultima_revision}`;
                await loadSavedSearches();
            } catch (error) {
                statusBox.textContent = `Error: ${error.message}`;
            }
        }

        async function reviewSavedSearchesNow() {
            statusBox.textContent = "Revisando busquedas guardadas...";

            try {
                const items = getLocalSavedSearches();

                for (const item of items) {
                    const params = new URLSearchParams({
                        query: item.query_original || "",
                        include_words: (item.incluir_palabras || []).join(", "),
                        include_mode: item.modo_inclusion || "all",
                        exclude_words: (item.excluir_palabras || []).join(", ")
                    });
                    const response = await fetch(`/search?${params.toString()}`);
                    const data = await response.json();

                    if (!response.ok) {
                        throw new Error(data.detail || "No se pudieron revisar las busquedas guardadas.");
                    }

                    upsertLocalSavedSearch({
                        query_original: data.query_original,
                        query_mejorada: data.query_mejorada,
                        incluir_palabras: data.incluir_palabras || [],
                        modo_inclusion: data.modo_inclusion || "all",
                        excluir_palabras: data.excluir_palabras || [],
                        guardada_en: item.guardada_en || nowLocalIso(),
                        ultima_revision: nowLocalIso(),
                        ultima_revision_dia: todayKey(),
                        total_productos: (data.productos || []).length,
                        chollos_detectados: (data.productos || []).filter((product) => product.es_chollo).length
                    });
                }

                statusBox.textContent = "Revision local completada.";
                await loadSavedSearches();
            } catch (error) {
                statusBox.textContent = `Error: ${error.message}`;
            }
        }

        async function runAutomaticLocalReviewIfNeeded() {
            const items = getLocalSavedSearches();

            if (!items.length) {
                return;
            }

            const needsReview = items.some((item) => item.ultima_revision_dia !== todayKey());
            if (!needsReview) {
                return;
            }

            await reviewSavedSearchesNow();
        }

        function repeatSearch(text, includeWords = "", includeMode = "all", excludeWords = "") {
            queryInput.value = text;
            includeWordsInput.value = includeWords;
            includeModeInput.value = includeMode;
            excludeWordsInput.value = excludeWords;
            doSearch();
        }

        window.repeatSearch = repeatSearch;
        window.saveCurrentQuery = saveCurrentQuery;

        searchButton.addEventListener("click", doSearch);
        saveButton.addEventListener("click", () => saveCurrentQuery());
        reviewSavedButton.addEventListener("click", reviewSavedSearchesNow);

        queryInput.addEventListener("keydown", (event) => {
            if (event.key === "Enter") {
                doSearch();
            }
        });

        document.querySelectorAll(".example-chip").forEach((button) => {
            button.addEventListener("click", () => {
                queryInput.value = button.dataset.query || "";
                doSearch();
            });
        });

        loadSavedSearches();
        runAutomaticLocalReviewIfNeeded();
    </script>
</body>
</html>
""".strip()


@app.get("/", response_class=HTMLResponse)
def home() -> HTMLResponse:
    return HTMLResponse(build_home_page())


@app.get("/search", response_model=SearchResponse)
def search(
    request: Request,
    query: str = Query(..., min_length=2, description="Texto a buscar"),
    include_words: str = Query("", description="Palabras que deben aparecer en el titulo"),
    include_mode: str = Query("all", description="all o any para las palabras a incluir"),
    exclude_words: str = Query("", description="Palabras que no deben aparecer en el titulo"),
) -> SearchResponse:
    enforce_rate_limit(request)
    return run_search_logic(
        query,
        split_words(include_words),
        include_mode,
        split_words(exclude_words),
    )


if __name__ == "__main__":
    uvicorn.run(
        "main:app",
        host="0.0.0.0",
        port=int(os.getenv("PORT", "8000")),
        reload=False,
    )
