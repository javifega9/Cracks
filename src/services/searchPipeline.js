const env = require("../config/env");
const MemoryCache = require("./cache/memoryCache");
const { interpretQuery } = require("./queryInterpreter");
const { normalizeProducts } = require("./normalize");
const { rankProducts } = require("./ranking");
const scrapeAmazon = require("../scrapers/amazon");
const logger = require("../utils/logger");
const { formatEuros, round } = require("../utils/price");

const searchCache = new MemoryCache(env.cacheTtlMs);
const SCRAPER_TIMEOUT_MS = env.scraperTimeoutMs;

const SCRAPERS = [
  { source: "amazon", handler: scrapeAmazon }
];

function uniqueQueries(queries) {
  return [...new Set((queries || []).map((query) => String(query || "").trim()).filter(Boolean))];
}

function normalizeText(value) {
  return String(value || "")
    .normalize("NFD")
    .replace(/[\u0300-\u036f]/g, "")
    .toLowerCase()
    .replace(/[^a-z0-9]+/g, " ")
    .replace(/\s+/g, " ")
    .trim();
}

function pickQueriesForSource(source, interpretation, userInput) {
  const genericBase = String(interpretation?.product || userInput || "")
    .replace(/\b(amazon|ebay|aliexpress)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  return uniqueQueries([genericBase, userInput]).slice(0, 2);
}

function withTimeout(promise, timeoutMs, label) {
  let timer = null;
  const timeoutPromise = new Promise((_, reject) => {
    timer = setTimeout(() => reject(new Error(`${label} ha superado el tiempo limite`)), timeoutMs);
  });

  return Promise.race([promise, timeoutPromise]).finally(() => {
    if (timer) {
      clearTimeout(timer);
    }
  });
}

async function mapWithConcurrency(items, limit, worker) {
  const results = [];
  let index = 0;

  async function runNext() {
    while (index < items.length) {
      const currentIndex = index;
      index += 1;
      results[currentIndex] = await worker(items[currentIndex], currentIndex);
    }
  }

  const workers = Array.from({ length: Math.max(1, Math.min(limit, items.length)) }, () => runNext());
  await Promise.all(workers);
  return results;
}

async function searchAllSources(interpretation, userInput) {
  // Cada marketplace intenta una query afinada y, si no devuelve nada, una version mas simple.
  const results = await mapWithConcurrency(SCRAPERS, env.maxConcurrentScrapers, async ({ source, handler }) => {
    const sourceQueries = pickQueriesForSource(source, interpretation, userInput);
    if (!sourceQueries.length) {
      return [];
    }

    for (const sourceQuery of sourceQueries) {
      try {
        const items = await withTimeout(handler(sourceQuery), SCRAPER_TIMEOUT_MS, `El scraper ${source}`);
        const relevantItems = filterProductsForIntent(items, userInput, interpretation);
        const fallbackItems = relevantItems.length ? relevantItems : fallbackProductsForSource(items, source, userInput, interpretation);
        if (items.length) {
          logger.info(
            `Muestra ${source}: ${items
              .slice(0, 2)
              .map((item) => String(item.title || "").slice(0, 120))
              .join(" | ")}`
          );
        }
        logger.info(
          `Scraper ${source}: ${items.length} resultados, ${relevantItems.length} relevantes, ${fallbackItems.length} utiles para "${sourceQuery}"`
        );
        if (fallbackItems.length) {
          return fallbackItems;
        }
      } catch (error) {
        logger.warn(`El scraper ${source} ha fallado para "${sourceQuery}".`, error.message);
      }
    }

    return [];
  });

  return results.flat();
}

function buildAmazonAffiliateUrl(url) {
  if (!env.amazonAffiliateTag || !/amazon\./i.test(url)) {
    return url;
  }

  const parsed = new URL(url);
  parsed.searchParams.set("tag", env.amazonAffiliateTag);
  return parsed.toString();
}

function buildAmazonSearchUrl(query, extraParams = {}) {
  const url = new URL("https://www.amazon.es/s");
  url.searchParams.set("k", query);
  Object.entries(extraParams).forEach(([key, value]) => {
    if (value) {
      url.searchParams.set(key, value);
    }
  });
  return buildAmazonAffiliateUrl(url.toString());
}

function buildSearchTokens(userInput, interpretation) {
  return uniqueQueries([
    interpretation?.product,
    userInput
  ])
    .map((item) => normalizeText(item))
    .join(" ")
    .split(/\s+/)
    .map((token) => token.trim())
    .filter((token) => token && token.length >= 2 && !["de", "la", "el", "en", "un", "una", "oferta", "barato", "premium", "mejor"].includes(token));
}

function filterProductsForIntent(products, userInput, interpretation) {
  const tokens = buildSearchTokens(userInput, interpretation);
  const accessoryKeywords = [
    "funda",
    "carcasa",
    "case",
    "cover",
    "protector",
    "glass",
    "tempered",
    "cable",
    "charger",
    "cargador",
    "stand",
    "holder",
    "hulle",
    "hülle",
    "abdeckung",
    "smart case"
  ];
  const queryText = normalizeText(`${interpretation?.product || ""} ${userInput}`);
  const userAskedForAccessory = accessoryKeywords.some((word) => queryText.includes(word));

  return products.filter((product) => {
    const title = normalizeText(product?.title || "");
    if (!title) {
      return false;
    }

    if (!userAskedForAccessory && accessoryKeywords.some((word) => title.includes(word))) {
      return false;
    }

    if (!tokens.length) {
      return true;
    }

    const matchingTokens = [...new Set(tokens.filter((token) => title.includes(token)))];
    return matchingTokens.length >= 1;
  });
}

function fallbackProductsForSource(products, source, userInput, interpretation) {
  if (!products.length) {
    return [];
  }

  const tokens = buildSearchTokens(userInput, interpretation);
  const scored = products
    .map((product) => {
      const title = normalizeText(product?.title || "");
      const matches = tokens.filter((token) => title.includes(token)).length;
      return {
        product,
        matches
      };
    })
    .sort((left, right) => right.matches - left.matches);

  const withMatches = scored.filter((item) => item.matches > 0).map((item) => item.product);
  if (withMatches.length) {
    return withMatches;
  }

  // Camino de seguridad: si Amazon devuelve productos pero ninguno pasa el filtro,
  // preferimos mostrar los primeros antes que dejar la busqueda vacia.
  if (source === "amazon") {
    logger.warn(`Se usa fallback permisivo para Amazon en "${userInput}".`);
    return products.slice(0, Math.min(3, products.length));
  }

  return [];
}

function computeAveragePrice(products) {
  const prices = products.map((product) => product.price).filter(Number.isFinite);
  if (!prices.length) {
    return null;
  }
  return round(prices.reduce((sum, price) => sum + price, 0) / prices.length);
}

function markBargains(products, averagePrice) {
  return products.map((product) => ({
    ...product,
    es_chollo: Number.isFinite(averagePrice) ? product.price <= averagePrice * env.bargainThreshold : false,
    es_amazon: product.source === "amazon",
    es_afiliado_amazon: product.source === "amazon" && Boolean(env.amazonAffiliateTag),
    url: product.source === "amazon" ? buildAmazonAffiliateUrl(product.url) : product.url
  }));
}

function withSpanishShape(product) {
  const storeLabels = {
    amazon: "Amazon",
    ebay: "eBay",
    aliexpress: "AliExpress"
  };

  return {
    titulo: product.title,
    precio: formatEuros(product.price),
    precio_numerico: product.price,
    precio_original: Number.isFinite(product.original_price) ? formatEuros(product.original_price) : null,
    descuento: Number.isFinite(product.discount) ? round(product.discount) : null,
    rating: Number.isFinite(product.rating) ? round(product.rating, 1) : null,
    tienda: storeLabels[product.source] || product.source,
    source: product.source,
    link: product.url,
    url: product.url,
    es_chollo: Boolean(product.es_chollo),
    es_amazon: Boolean(product.es_amazon),
    es_afiliado_amazon: Boolean(product.es_afiliado_amazon),
    categoria_destacada: product.categoria_destacada || null,
    etiqueta: product.etiqueta || null,
    score: product.score,
    priceScore: product.priceScore
  };
}

function pickFeaturedProducts(products) {
  if (!products.length) {
    return [];
  }

  const available = [...products];
  const featured = [];

  const takeOne = (predicate, category, label) => {
    const candidate = predicate(available);
    if (!candidate) {
      return;
    }
    featured.push({
      ...candidate,
      categoria_destacada: category,
      etiqueta: label
    });
    const index = available.findIndex((item) => item.url === candidate.url);
    if (index >= 0) {
      available.splice(index, 1);
    }
  };

  takeOne((items) => items[0], "Mejor opcion", "Mejor opcion");

  takeOne(
    (items) =>
      [...items].sort((left, right) => {
        const leftScore = (left.discount || 0) + (left.rating || 0) * 10;
        const rightScore = (right.discount || 0) + (right.rating || 0) * 10;
        return rightScore - leftScore;
      })[0],
    "Mejor calidad-precio",
    "Mejor calidad-precio"
  );

  takeOne(
    (items) => [...items].sort((left, right) => left.price - right.price)[0],
    "Opcion mas barata",
    "Mas barato"
  );

  return featured;
}

function buildFallbackResponse(userInput) {
  const safeQuery = String(userInput || "").trim();
  const cards = [
    {
      titulo: `${safeQuery} en Amazon`,
      precio: "Ver resultados",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(safeQuery),
      url: buildAmazonSearchUrl(safeQuery),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Mejor opcion",
      etiqueta: "Mejor opcion",
      explicacion: "Abre los resultados principales de Amazon para esta busqueda."
    },
    {
      titulo: `${safeQuery} mas baratos en Amazon`,
      precio: "Ver ofertas ordenadas",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(safeQuery, { s: "price-asc-rank" }),
      url: buildAmazonSearchUrl(safeQuery, { s: "price-asc-rank" }),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Opcion mas barata",
      etiqueta: "Mas barato",
      explicacion: "Muestra primero las opciones mas economicas dentro de Amazon."
    },
    {
      titulo: `${safeQuery} mejor valorados en Amazon`,
      precio: "Ver mejor valorados",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(safeQuery, { s: "review-rank" }),
      url: buildAmazonSearchUrl(safeQuery, { s: "review-rank" }),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Mejor calidad-precio",
      etiqueta: "Mejor calidad-precio",
      explicacion: "Prioriza resultados con mejor valoracion para decidir mas rapido."
    }
  ];

  return {
    query_original: safeQuery,
    query_mejorada: safeQuery,
    interpretation: null,
    cached: false,
    precio_medio: null,
    productos: cards,
    top_3_mejores_opciones: cards,
    message: "No hemos podido extraer productos exactos ahora mismo. Te mostramos accesos directos utiles a Amazon."
  };
}

async function executeSearch(userInput) {
  const cacheKey = userInput.trim().toLowerCase();
  const cached = searchCache.get(cacheKey);
  if (cached) {
    return { ...cached, cached: true };
  }

  const interpretation = await interpretQuery(userInput);
  const rawProducts = await searchAllSources(interpretation, userInput);
  const normalizedProducts = normalizeProducts(rawProducts);
  const emergencyProducts =
    normalizedProducts.length || !rawProducts.length
      ? normalizedProducts
      : normalizeProducts(
          rawProducts.filter((product) => String(product?.source || "").toLowerCase() === "amazon").slice(0, 3)
        );
  const finalInputProducts = emergencyProducts.length ? emergencyProducts : normalizedProducts;
  const averagePrice = computeAveragePrice(finalInputProducts);
  const enrichedProducts = markBargains(finalInputProducts, averagePrice);
  const rankedProducts = rankProducts(enrichedProducts);

  logger.info(
    `Pipeline de busqueda: ${rawProducts.length} utiles, ${finalInputProducts.length} normalizados, ${rankedProducts.length} finales.`
  );

  if (!rankedProducts.length) {
    return buildFallbackResponse(userInput);
  }

  const featuredProducts = pickFeaturedProducts(rankedProducts).map(withSpanishShape);
  const allProducts = rankedProducts.map(withSpanishShape);

  const payload = {
    query_original: userInput,
    query_mejorada: interpretation.queries[0] || interpretation.product || userInput,
    interpretation,
    cached: false,
    precio_medio: averagePrice,
    productos: allProducts,
    top_3_mejores_opciones: featuredProducts,
    message: null
  };

  searchCache.set(cacheKey, payload);
  return payload;
}

module.exports = {
  executeSearch,
  searchAllSources,
  normalizeProducts,
  rankProducts,
  searchCache
};
