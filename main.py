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
from fastapi.responses import JSONResponse
from fastapi.responses import HTMLResponse
from starlette.exceptions import HTTPException as StarletteHTTPException
from openai import OpenAI
from pydantic import BaseModel


SERPAPI_URL = "https://serpapi.com/search.json"
OPENAI_MODEL = os.getenv("OPENAI_MODEL", "gpt-5.4-mini")
SHOPPING_GL = os.getenv("SHOPPING_GL", "es")
SHOPPING_HL = os.getenv("SHOPPING_HL", "es")
CHOLLO_THRESHOLD = float(os.getenv("CHOLLO_THRESHOLD", "0.75"))
AMAZON_AFFILIATE_TAG = os.getenv("AMAZON_AFFILIATE_TAG", "").strip()
AMAZON_DOMAIN = os.getenv("AMAZON_DOMAIN", "amazon.es").strip().lower()
MAX_SHOPPING_RESULTS = int(os.getenv("MAX_SHOPPING_RESULTS", "10"))
AMAZON_LOOKUP_MAX_PRODUCTS = int(os.getenv("AMAZON_LOOKUP_MAX_PRODUCTS", "4"))
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


@app.exception_handler(StarletteHTTPException)
async def handle_http_exception(
    request: Request,
    exc: StarletteHTTPException,
) -> JSONResponse:
    return JSONResponse(status_code=exc.status_code, content={"detail": exc.detail})


@app.exception_handler(Exception)
async def handle_unexpected_exception(request: Request, exc: Exception) -> JSONResponse:
    return JSONResponse(
        status_code=500,
        content={"detail": "Error interno del servidor. Intenta de nuevo en unos segundos."},
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
    categoria_destacada: str | None = None
    etiqueta: str | None = None
    explicacion: str | None = None


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
        client = get_openai_client()
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        data = extract_json(response.output_text or "")
        improved_query = data.get("improved_query", "").strip()
        return improved_query or query
    except Exception:
        return query


def build_serpapi_error_detail(response: requests.Response | None) -> str:
    if response is None:
        return "Error al consultar SerpAPI."

    raw_text = response.text.strip()
    message = raw_text

    try:
        payload = response.json()
        if isinstance(payload, dict):
            message = str(payload.get("error") or payload.get("message") or raw_text).strip()
    except ValueError:
        pass

    normalized = message.lower()
    if response.status_code in {401, 403}:
        return "SerpAPI ha rechazado la clave API. Revisa SERPAPI_KEY."
    if response.status_code == 429 or "limit" in normalized or "quota" in normalized:
        return "Has alcanzado el limite de busquedas de SerpAPI."

    if message:
        return f"Error al consultar SerpAPI: {message}"

    return "Error al consultar SerpAPI."


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

    try:
        response = requests.get(SERPAPI_URL, params=params, timeout=30)
        response.raise_for_status()
        data = response.json()
    except requests.RequestException as exc:
        raise HTTPException(
            status_code=502,
            detail=build_serpapi_error_detail(getattr(exc, "response", None)),
        ) from exc
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="SerpAPI ha devuelto una respuesta no valida.",
        ) from exc

    if isinstance(data, dict) and data.get("error"):
        raise HTTPException(status_code=502, detail=build_serpapi_error_detail(response))

    return data


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
    except HTTPException:
        pass
    except requests.RequestException:
        pass

    AMAZON_CACHE[cache_key] = None
    return None


def initialize_amazon_metadata(products: list[dict[str, Any]]) -> None:
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


def select_amazon_lookup_candidates(
    products: list[dict[str, Any]],
    average_price: float | None,
) -> list[dict[str, Any]]:
    if AMAZON_LOOKUP_MAX_PRODUCTS <= 0:
        return []

    candidates = [
        product
        for product in products
        if not product.get("es_amazon") and str(product.get("titulo") or "").strip()
    ]
    candidates.sort(key=lambda product: score_overall_choice(product, average_price))
    return candidates[:AMAZON_LOOKUP_MAX_PRODUCTS]


def enrich_products_with_amazon(
    products: list[dict[str, Any]],
    average_price: float | None,
) -> list[dict[str, Any]]:
    if not products:
        return products

    initialize_amazon_metadata(products)

    if not AMAZON_AFFILIATE_TAG:
        return products

    for product in select_amazon_lookup_candidates(products, average_price):
        if product.get("es_amazon"):
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
            detail=build_serpapi_error_detail(getattr(exc, "response", None)),
        ) from exc

    try:
        data = response.json()
    except ValueError as exc:
        raise HTTPException(
            status_code=502,
            detail="SerpAPI ha devuelto una respuesta no valida.",
        ) from exc

    if isinstance(data, dict) and data.get("error"):
        raise HTTPException(status_code=502, detail=build_serpapi_error_detail(response))

    raw_products = data.get("shopping_results", [])

    products = []
    for item in raw_products[:MAX_SHOPPING_RESULTS]:
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


def score_overall_choice(product: dict[str, Any], average_price: float | None) -> tuple[Any, ...]:
    price = product.get("precio_numerico")
    amazon_score = 0 if product.get("es_amazon") else 1
    bargain_score = 0 if product.get("es_chollo") else 1
    if isinstance(price, (int, float)):
        distance = abs(price - average_price) if isinstance(average_price, (int, float)) else price
        return (amazon_score, bargain_score, distance, price)
    return (amazon_score, bargain_score, float("inf"), float("inf"))


def score_value_choice(product: dict[str, Any], average_price: float | None) -> tuple[Any, ...]:
    price = product.get("precio_numerico")
    amazon_score = 0 if product.get("es_amazon") else 1
    bargain_score = 0 if product.get("es_chollo") else 1
    if isinstance(price, (int, float)):
        savings = (average_price - price) if isinstance(average_price, (int, float)) else 0
        return (amazon_score, bargain_score, -savings, price)
    return (amazon_score, 1, float("inf"), float("inf"))


def score_cheapest_choice(product: dict[str, Any]) -> tuple[Any, ...]:
    price = product.get("precio_numerico")
    amazon_score = 0 if product.get("es_amazon") else 1
    if isinstance(price, (int, float)):
        return (amazon_score, price)
    return (amazon_score, float("inf"))


def label_featured_products(
    products: list[dict[str, Any]],
    average_price: float | None,
) -> list[dict[str, Any]]:
    if not products:
        return []

    available = list(products)
    featured = []

    def take_best(score_fn, category: str, label_fn) -> None:
        if not available:
            return
        best = min(available, key=score_fn)
        available.remove(best)
        best["categoria_destacada"] = category
        best["etiqueta"] = label_fn(best)
        featured.append(best)

    take_best(
        lambda product: score_overall_choice(product, average_price),
        "Mejor opción",
        lambda product: "🔥 Mejor opción",
    )
    take_best(
        lambda product: score_value_choice(product, average_price),
        "Mejor calidad-precio",
        lambda product: "💸 Mejor calidad-precio",
    )
    take_best(
        score_cheapest_choice,
        "Opción más barata",
        lambda product: "🟢 Más barato",
    )

    while len(featured) < 3 and available:
        extra = min(available, key=lambda product: score_overall_choice(product, average_price))
        available.remove(extra)
        extra["categoria_destacada"] = "Opción destacada"
        extra["etiqueta"] = "🔥 Mejor opción"
        featured.append(extra)

    return featured


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
        categoria_destacada=product.get("categoria_destacada"),
        etiqueta=product.get("etiqueta"),
        explicacion=product.get("explicacion"),
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


def fallback_explanation_for_product(product: dict[str, Any], average_price: float | None) -> str:
    category = product.get("categoria_destacada")
    is_bargain = bool(product.get("es_chollo"))
    is_amazon = bool(product.get("es_amazon"))
    price = product.get("precio_numerico")

    if category == "Mejor opción":
        if is_amazon and is_bargain:
            return "Equilibrio ideal entre ahorro, confianza y compra rápida."
        if is_amazon:
            return "La opción más sólida para comprar rápido en Amazon."
        return "La alternativa más equilibrada para decidir sin complicarte."

    if category == "Mejor calidad-precio":
        if is_bargain:
            return "Ofrece mucho por menos y cae bajo la media."
        return "Buen equilibrio entre coste, valor y compra sencilla."

    if category == "Opción más barata":
        if is_bargain:
            return "La opción más barata y por debajo del precio medio."
        return "La opción más barata dentro de esta comparativa."

    if isinstance(price, (int, float)) and isinstance(average_price, (int, float)) and price < average_price:
        return "Precio atractivo frente al promedio de esta búsqueda."

    return "Selección pensada para decidir y hacer clic rápido."


def enrich_featured_products_with_openai(
    products: list[dict[str, Any]],
    average_price: float | None,
) -> list[dict[str, Any]]:
    if not products:
        return products

    payload = []
    for index, product in enumerate(products):
        payload.append(
            {
                "index": index,
                "titulo": product.get("titulo"),
                "precio": product.get("precio"),
                "categoria_destacada": product.get("categoria_destacada"),
                "etiqueta": product.get("etiqueta"),
                "es_chollo": product.get("es_chollo"),
                "es_amazon": product.get("es_amazon"),
            }
        )

    schema = {
        "type": "object",
        "properties": {
            "items": {
                "type": "array",
                "items": {
                    "type": "object",
                    "properties": {
                        "index": {"type": "integer"},
                        "explicacion": {"type": "string"},
                    },
                    "required": ["index", "explicacion"],
                    "additionalProperties": False,
                },
            }
        },
        "required": ["items"],
        "additionalProperties": False,
    }

    prompt = f"""
Eres un asistente de ecommerce orientado a conversion.

Debes escribir una explicacion muy corta para cada producto destacado.

Reglas:
- Maximo 10 palabras por explicacion.
- Debe sonar util para decidir rapido.
- No inventes especificaciones tecnicas.
- Enfocate en precio, valor, ahorro o conveniencia.
- Devuelve solo JSON valido.
- Formato exacto:
  {{"items":[{{"index":0,"explicacion":"texto"}}]}}

Precio medio aproximado: {average_price}
Productos destacados: {json.dumps(payload, ensure_ascii=False)}
""".strip()

    try:
        client = get_openai_client()
        response = client.responses.create(
            model=OPENAI_MODEL,
            input=prompt,
        )
        data = extract_json(response.output_text or "")
        items = data.get("items", [])
        for item in items:
            index = item.get("index")
            explanation = str(item.get("explicacion", "")).strip()
            if isinstance(index, int) and 0 <= index < len(products) and explanation:
                products[index]["explicacion"] = explanation
    except Exception:
        pass

    for product in products:
        if not product.get("explicacion"):
            product["explicacion"] = fallback_explanation_for_product(product, average_price)

    return products


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
    average_price = mark_bargains(products)
    products = enrich_products_with_amazon(products, average_price)
    products = sorted(products, key=lambda product: score_overall_choice(product, average_price))
    top_3 = label_featured_products(products, average_price)
    top_3 = enrich_featured_products_with_openai(top_3, average_price)

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
            padding: 28px 0 88px;
        }

        .topbar {
            display: flex;
            justify-content: center;
            margin-bottom: 24px;
        }

        .brand {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 18px;
            text-align: left;
        }

        .brand-mark {
            width: 78px;
            height: 78px;
            border-radius: 24px;
            display: grid;
            place-items: center;
            color: white;
            font-weight: 800;
            font-size: 2rem;
            background:
                linear-gradient(135deg, #0c3a59 0%, #1b5f8f 55%, #f0893d 100%);
            box-shadow: 0 18px 32px rgba(16, 61, 96, 0.22);
        }

        .brand-copy strong {
            display: block;
            font-size: clamp(3rem, 8vw, 5.4rem);
            line-height: 0.9;
            letter-spacing: -0.07em;
            text-transform: uppercase;
        }

        .brand-copy span {
            color: var(--muted);
            font-size: 0.95rem;
            opacity: 0.78;
            letter-spacing: 0.01em;
        }

        .hero {
            position: relative;
            overflow: hidden;
            background: transparent;
            border: 0;
            border-radius: 0;
            padding: 10px 0 28px;
            box-shadow: none;
            min-height: calc(100vh - 140px);
            display: grid;
            align-items: center;
        }

        .hero::before {
            content: "";
            position: absolute;
            width: 420px;
            height: 420px;
            right: -120px;
            top: -120px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(14, 73, 115, 0.1) 0%, transparent 66%);
            pointer-events: none;
        }

        .hero::after {
            content: "";
            position: absolute;
            width: 300px;
            height: 300px;
            left: -90px;
            bottom: -140px;
            border-radius: 50%;
            background: radial-gradient(circle, rgba(199, 109, 43, 0.1) 0%, transparent 68%);
            pointer-events: none;
        }

        .hero-grid {
            position: relative;
            z-index: 1;
            display: grid;
            grid-template-columns: minmax(0, 1fr);
            gap: 26px;
            align-items: center;
            justify-items: center;
            width: min(980px, 100%);
            margin: 0 auto;
        }

        .hero-main {
            width: min(100%, 960px);
            display: grid;
            justify-items: center;
            gap: 24px;
            text-align: center;
        }

        h1 {
            margin: 0;
            font-size: 1px;
            line-height: 1;
            opacity: 0;
            position: absolute;
            pointer-events: none;
        }

        .subtitle {
            margin: 0;
            color: var(--muted);
            font-size: 0.98rem;
            line-height: 1.65;
            max-width: 760px;
            opacity: 0.82;
        }

        .hero-points {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 14px;
            width: min(100%, 920px);
        }

        .hero-point {
            background: rgba(255, 255, 255, 0.78);
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 16px 18px;
            box-shadow: 0 12px 24px rgba(31, 27, 22, 0.05);
            text-align: left;
        }

        .hero-point strong {
            display: block;
            font-size: 0.9rem;
            margin-bottom: 5px;
        }

        .hero-point span {
            color: var(--muted);
            font-size: 0.88rem;
            line-height: 1.45;
        }

        .search-shell {
            width: min(100%, 860px);
            background: rgba(255, 255, 255, 0.9);
            border: 1px solid var(--line);
            border-radius: 34px;
            padding: 22px;
            box-shadow: 0 26px 70px rgba(31, 27, 22, 0.12);
            backdrop-filter: blur(16px);
        }

        .search-bar {
            display: grid;
            grid-template-columns: minmax(0, 1fr) auto;
            gap: 14px;
        }

        input[type="text"] {
            width: 100%;
            padding: 20px 22px;
            border-radius: 22px;
            border: 1px solid var(--line);
            font-size: 1.08rem;
            outline: none;
            background: rgba(255, 255, 255, 0.96);
        }

        input[type="text"]::placeholder {
            color: rgba(103, 114, 131, 0.62);
        }

        input[type="text"]:focus {
            border-color: var(--primary);
            box-shadow: 0 0 0 4px rgba(196, 107, 45, 0.12);
        }

        button {
            border: 0;
            border-radius: 20px;
            padding: 0 24px;
            font-size: 1rem;
            font-weight: 700;
            cursor: pointer;
            min-height: 62px;
            transition: transform 0.18s ease, box-shadow 0.18s ease, background 0.18s ease;
        }

        .primary-button {
            background: linear-gradient(135deg, var(--primary) 0%, #e88d45 100%);
            color: white;
            box-shadow: 0 18px 30px rgba(199, 109, 43, 0.24);
            min-width: 170px;
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

        .search-utility-row {
            display: flex;
            align-items: center;
            justify-content: center;
            gap: 12px;
            flex-wrap: wrap;
            margin-top: 16px;
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

        .section {
            margin-top: 40px;
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

        .section-intro {
            color: var(--muted);
            font-size: 0.98rem;
            line-height: 1.55;
            max-width: 720px;
            margin-top: 8px;
        }

        .spotlight {
            display: none;
            margin-top: 22px;
            background: linear-gradient(135deg, rgba(15, 58, 89, 0.96) 0%, rgba(22, 83, 122, 0.94) 100%);
            color: white;
            border-radius: 28px;
            padding: 22px 24px;
            box-shadow: 0 22px 44px rgba(16, 61, 96, 0.20);
        }

        .spotlight-label {
            display: inline-flex;
            align-items: center;
            gap: 8px;
            padding: 7px 11px;
            border-radius: 999px;
            background: rgba(255, 255, 255, 0.12);
            font-size: 0.8rem;
            font-weight: 800;
            letter-spacing: 0.05em;
            text-transform: uppercase;
            margin-bottom: 12px;
        }

        .spotlight-title {
            font-size: clamp(1.4rem, 3vw, 2rem);
            letter-spacing: -0.03em;
            font-weight: 800;
            margin-bottom: 8px;
        }

        .spotlight-copy {
            color: rgba(255, 255, 255, 0.78);
            font-size: 0.98rem;
            line-height: 1.6;
            margin-bottom: 16px;
        }

        .spotlight-actions {
            display: flex;
            align-items: center;
            justify-content: space-between;
            gap: 16px;
            flex-wrap: wrap;
        }

        .spotlight-price {
            font-size: 2rem;
            font-weight: 800;
            letter-spacing: -0.04em;
        }

        .spotlight-button {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            min-height: 52px;
            padding: 0 22px;
            border-radius: 16px;
            background: linear-gradient(135deg, #ffb34f 0%, #f08a31 100%);
            color: #4a2107;
            text-decoration: none;
            font-weight: 800;
            box-shadow: 0 14px 24px rgba(240, 138, 49, 0.24);
        }

        .spotlight-button:hover {
            background: linear-gradient(135deg, #ffc361 0%, #f59b45 100%);
            text-decoration: none;
        }

        .compare-strip {
            display: none;
            margin-top: 20px;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 12px;
        }

        .compare-item {
            background: rgba(255, 255, 255, 0.92);
            border: 1px solid var(--line);
            border-radius: 20px;
            padding: 16px;
            box-shadow: var(--shadow-soft);
        }

        .compare-item strong {
            display: block;
            font-size: 0.88rem;
            color: var(--accent);
            margin-bottom: 8px;
            text-transform: uppercase;
            letter-spacing: 0.06em;
        }

        .compare-item-title {
            font-size: 0.96rem;
            font-weight: 700;
            line-height: 1.45;
            margin-bottom: 8px;
        }

        .compare-item-price {
            font-size: 1.35rem;
            font-weight: 800;
            letter-spacing: -0.03em;
        }

        .grid {
            display: grid;
            grid-template-columns: repeat(auto-fit, minmax(240px, 1fr));
            gap: 16px;
        }

        .featured-grid {
            display: grid;
            grid-template-columns: repeat(3, minmax(0, 1fr));
            gap: 18px;
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

        .featured-card {
            position: relative;
        }

        .featured-card::after {
            content: "";
            position: absolute;
            inset: 0;
            border-radius: 24px;
            pointer-events: none;
            box-shadow: inset 0 0 0 1px rgba(255, 255, 255, 0.55);
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
            font-size: 2rem;
            font-weight: 800;
            letter-spacing: -0.04em;
            line-height: 1;
        }

        .store,
        .saved-meta {
            color: var(--muted);
            margin-bottom: 0;
        }

        .product-explanation {
            color: var(--text);
            font-size: 0.96rem;
            line-height: 1.55;
            background: rgba(16, 61, 96, 0.05);
            border: 1px solid rgba(16, 61, 96, 0.08);
            border-radius: 16px;
            padding: 12px 14px;
        }

        .featured-kicker {
            color: var(--accent);
            font-size: 0.86rem;
            font-weight: 800;
            text-transform: uppercase;
            letter-spacing: 0.08em;
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
            display: grid;
            gap: 12px;
            margin-top: auto;
        }

        .card-link {
            display: inline-flex;
            align-items: center;
            justify-content: center;
            width: 100%;
            min-height: 52px;
            padding: 0 18px;
            border-radius: 16px;
            background: linear-gradient(135deg, #ffb34f 0%, #f08a31 100%);
            border: 1px solid rgba(199, 109, 43, 0.22);
            color: #4a2107;
            text-decoration: none;
            white-space: nowrap;
            font-weight: 800;
            box-shadow: 0 12px 20px rgba(240, 138, 49, 0.20);
        }

        .card-link:hover {
            text-decoration: none;
            background: linear-gradient(135deg, #ffbf63 0%, #f4933f 100%);
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
                margin-bottom: 18px;
            }

            .brand {
                gap: 14px;
            }

            .brand-mark {
                width: 62px;
                height: 62px;
                font-size: 1.6rem;
            }

            .brand-copy strong {
                font-size: clamp(2.6rem, 12vw, 4rem);
            }

            .featured-grid {
                grid-template-columns: 1fr;
            }

            .search-bar {
                grid-template-columns: 1fr;
            }

            .search-shell {
                width: 100%;
                padding: 18px;
            }

            .search-utility-row {
                align-items: stretch;
                flex-direction: column;
            }

            .hero-points {
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
                min-height: auto;
                padding: 0 0 18px;
            }

            .wrap {
                padding-bottom: 72px;
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
                    <strong>CRACKS</strong>
                    <span>Encuentra ofertas claras y decide rapido</span>
                </div>
            </div>
        </div>

        <section class="hero">
            <div class="hero-grid">
                <div class="hero-main">
                    <h1>CRACKS</h1>
                    <div class="search-shell">
                        <div class="search-bar">
                            <input id="query" type="text" placeholder="iphone 15, cafetera nespresso, portatil lenovo barato">
                            <button id="searchButton" class="primary-button">Buscar</button>
                        </div>

                        <div class="search-utility-row">
                            <button id="saveButton" class="secondary-button" type="button">Guardar busqueda</button>
                        </div>

                        <div class="status" id="status">Listo para buscar.</div>
                        <div class="meta" id="meta"></div>
                    </div>

                    <p class="subtitle">
                        Encuentra las mejores ofertas de internet en segundos
                    </p>

                    <div class="hero-points">
                        <div class="hero-point">
                            <strong>Busqueda directa</strong>
                            <span>Escribe lo que quieres comprar y ve solo tres opciones claras.</span>
                        </div>
                        <div class="hero-point">
                            <strong>Top 3 util</strong>
                            <span>Cracks destaca mejor opcion, calidad-precio y la mas barata.</span>
                        </div>
                        <div class="hero-point">
                            <strong>Guardado simple</strong>
                            <span>Guarda tus busquedas en este navegador y vuelve cuando quieras.</span>
                        </div>
                    </div>
                </div>
            </div>
        </section>

        <section class="section" id="topSection">
            <div class="section-header">
                <div>
                    <h2>3 productos destacados</h2>
                    <div class="section-intro">
                        Cracks solo te muestra tres opciones listas para decidir: la mejor opcion general,
                        la mejor calidad-precio y la mas barata.
                    </div>
                </div>
            </div>
            <div class="spotlight" id="spotlightBox"></div>
            <div class="compare-strip" id="compareStrip"></div>
            <div class="featured-grid" id="topGrid"></div>
        </section>

        <section class="section" id="savedSection" style="display:block;">
            <div class="section-header">
                <h2>Busquedas guardadas en este navegador</h2>
                <button id="reviewSavedButton" class="secondary-button" type="button">Revisar ahora</button>
            </div>
            <div class="saved-grid" id="savedGrid"></div>
        </section>

        <div class="status" style="margin-top: 22px;">
            Como afiliado de Amazon, obtengo ingresos por compras adscritas
        </div>
    </div>

    <script>
        const LOCAL_STORAGE_KEY = "cracks_saved_searches";
        const queryInput = document.getElementById("query");
        const searchButton = document.getElementById("searchButton");
        const saveButton = document.getElementById("saveButton");
        const reviewSavedButton = document.getElementById("reviewSavedButton");
        const statusBox = document.getElementById("status");
        const metaBox = document.getElementById("meta");
        const topSection = document.getElementById("topSection");
        const spotlightBox = document.getElementById("spotlightBox");
        const compareStrip = document.getElementById("compareStrip");
        const topGrid = document.getElementById("topGrid");
        const savedGrid = document.getElementById("savedGrid");
        const rotatingPlaceholders = [
            "iphone 15",
            "cafetera nespresso",
            "portatil lenovo barato",
            "zapatillas running nike",
            "aspiradora sin cable"
        ];
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

        function startPlaceholderRotation() {
            let placeholderIndex = 0;

            const applyPlaceholder = () => {
                if (document.activeElement === queryInput || queryInput.value.trim()) {
                    return;
                }
                queryInput.setAttribute("placeholder", rotatingPlaceholders[placeholderIndex]);
            };

            applyPlaceholder();

            setInterval(() => {
                placeholderIndex = (placeholderIndex + 1) % rotatingPlaceholders.length;
                applyPlaceholder();
            }, 2400);

            queryInput.addEventListener("blur", applyPlaceholder);
        }

        function scrollResultsIntoView() {
            requestAnimationFrame(() => {
                const top = Math.max(topSection.getBoundingClientRect().top + window.scrollY - 18, 0);
                window.scrollTo({ top, behavior: "smooth" });
            });
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

        async function readJsonResponse(response) {
            const rawText = await response.text();

            try {
                return rawText ? JSON.parse(rawText) : {};
            } catch (error) {
                if (!response.ok) {
                    throw new Error("Error interno del servidor. Intenta de nuevo en unos segundos.");
                }
                throw new Error("La respuesta del servidor no tiene un formato valido.");
            }
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
            if (product.etiqueta) {
                badges.push(`<div class="badge">${escapeHtml(product.etiqueta)}</div>`);
            }
            if (product.es_chollo) {
                badges.push(`<div class="badge bargain-badge">🔥 Chollo detectado</div>`);
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

            const explanation = product.explicacion || "Selección pensada para decidir rápido.";
            const linkLabel = product.es_amazon ? "🟡 Ver en Amazon" : "🟡 Ver oferta";
            const linkNote = product.es_afiliado_amazon
                ? "Enlace de Amazon con tu tag de afiliado."
                : product.link_original && product.link_original !== product.link
                    ? "Se encontro una opcion equivalente en Amazon."
                    : "Enlace directo al producto encontrado.";

            return `
                <article class="card featured-card ${rank ? "top-card" : ""}">
                    <div class="badge-row">${badges.join("")}</div>
                    <div class="product-media">${imageMarkup}</div>
                    <div class="card-content">
                        <div class="featured-kicker">${escapeHtml(product.categoria_destacada || `Destacado ${rank}`)}</div>
                        <div class="title">${escapeHtml(product.titulo)}</div>
                        <div class="price">${escapeHtml(product.precio || "Precio no disponible")}</div>
                        <div class="store">${escapeHtml(product.tienda || "Tienda no disponible")}</div>
                        <div class="product-explanation">${escapeHtml(explanation)}</div>
                    </div>
                    <div class="card-footer">
                        <a class="card-link" href="${escapeHtml(product.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(linkLabel)}</a>
                        <div class="link-note">${escapeHtml(linkNote)}</div>
                    </div>
                </article>
            `;
        }

        function renderSavedSearch(item) {
            const queryJs = JSON.stringify(item.query_original || "");

            return `
                <article class="card">
                    <div class="title">${escapeHtml(item.query_original)}</div>
                    <div class="saved-meta">Mejorada: ${escapeHtml(item.query_mejorada)}</div>
                    <div class="saved-meta">Ultima revision: ${escapeHtml(item.ultima_revision)}</div>
                    <div class="saved-meta">Productos: ${escapeHtml(item.total_productos)}</div>
                    <div class="saved-meta">Chollos detectados: ${escapeHtml(item.chollos_detectados)}</div>
                    <div class="saved-actions">
                        <button class="secondary-button small-button" type="button" onclick='repeatSearch(${queryJs})'>Buscar</button>
                        <button class="secondary-button small-button" type="button" onclick='saveCurrentQuery(${queryJs})'>Actualizar guardada</button>
                    </div>
                </article>
            `;
        }

        function renderSpotlight(product) {
            if (!product) {
                spotlightBox.style.display = "none";
                spotlightBox.innerHTML = "";
                return;
            }

            spotlightBox.innerHTML = `
                <div class="spotlight-label">Recomendacion principal</div>
                <div class="spotlight-title">${escapeHtml(product.titulo)}</div>
                <div class="spotlight-copy">
                    ${escapeHtml(product.explicacion || product.etiqueta || "La seleccion mas equilibrada para decidir rapido.")}
                </div>
                <div class="spotlight-actions">
                    <div>
                        <div class="spotlight-price">${escapeHtml(product.precio || "Precio no disponible")}</div>
                        <div class="link-note" style="color: rgba(255,255,255,0.72);">${escapeHtml(product.tienda || "Tienda no disponible")}</div>
                    </div>
                    <a class="spotlight-button" href="${escapeHtml(product.link)}" target="_blank" rel="noopener noreferrer">${escapeHtml(product.es_amazon ? "🟡 Ver en Amazon" : "🟡 Ver oferta")}</a>
                </div>
            `;
            spotlightBox.style.display = "block";
        }

        function renderCompareStrip(products) {
            if (!products || !products.length) {
                compareStrip.style.display = "none";
                compareStrip.innerHTML = "";
                return;
            }

            compareStrip.innerHTML = products.map((product) => `
                <article class="compare-item">
                    <strong>${escapeHtml(product.categoria_destacada || "Destacado")}</strong>
                    <div class="compare-item-title">${escapeHtml(product.titulo)}</div>
                    <div class="compare-item-price">${escapeHtml(product.precio || "Precio no disponible")}</div>
                </article>
            `).join("");
            compareStrip.style.display = "grid";
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

            if (!query) {
                statusBox.textContent = "Escribe algo para buscar.";
                return;
            }

            statusBox.textContent = "Buscando productos...";
            metaBox.style.display = "none";
            topSection.style.display = "none";
            spotlightBox.style.display = "none";
            compareStrip.style.display = "none";
            topGrid.innerHTML = "";

            try {
                const params = new URLSearchParams({
                    query: query
                });
                const response = await fetch(`/search?${params.toString()}`);
                const data = await readJsonResponse(response);

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
                    <div><b>Precio medio aproximado:</b> ${escapeHtml(average)}</div>
                    <div><b>Chollos detectados:</b> ${escapeHtml(chollos)}</div>
                `;
                metaBox.style.display = "block";

                topGrid.innerHTML = data.top_3_mejores_opciones
                    .map((product, index) => renderCard(product, index + 1))
                    .join("");

                renderSpotlight(data.top_3_mejores_opciones[0]);
                renderCompareStrip(data.top_3_mejores_opciones);

                topSection.style.display = data.top_3_mejores_opciones.length ? "block" : "none";

                if (!data.productos.length) {
                    statusBox.textContent = "No se encontraron productos para esa busqueda.";
                } else {
                    statusBox.textContent = `Busqueda completada. Mostrando 3 opciones destacadas para decidir mas rapido.`;
                    scrollResultsIntoView();
                }
            } catch (error) {
                statusBox.textContent = `Error: ${error.message}`;
            }
        }

        async function saveCurrentQuery(customQuery) {
            const query = (customQuery || queryInput.value).trim();

            if (!query) {
                statusBox.textContent = "Primero escribe algo para guardar.";
                return;
            }

            statusBox.textContent = "Guardando busqueda...";

            try {
                let data = latestSearchData;

                const latestMatchesCurrent =
                    data &&
                    data.query_original === query;

                if (!latestMatchesCurrent) {
                    const params = new URLSearchParams({
                        query: query
                    });
                    const response = await fetch(`/search?${params.toString()}`);
                    data = await readJsonResponse(response);

                    if (!response.ok) {
                        throw new Error(data.detail || "No se pudo guardar la busqueda.");
                    }
                }

                const item = {
                    query_original: data.query_original,
                    query_mejorada: data.query_mejorada,
                    incluir_palabras: [],
                    modo_inclusion: "all",
                    excluir_palabras: [],
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
                        query: item.query_original || ""
                    });
                    const response = await fetch(`/search?${params.toString()}`);
                    const data = await readJsonResponse(response);

                    if (!response.ok) {
                        throw new Error(data.detail || "No se pudieron revisar las busquedas guardadas.");
                    }

                    upsertLocalSavedSearch({
                        query_original: data.query_original,
                        query_mejorada: data.query_mejorada,
                        incluir_palabras: [],
                        modo_inclusion: "all",
                        excluir_palabras: [],
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

        function repeatSearch(text) {
            queryInput.value = text;
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

        loadSavedSearches();
        startPlaceholderRotation();
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
