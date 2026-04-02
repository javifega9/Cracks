const env = require("../config/env");
const MemoryCache = require("./cache/memoryCache");
const { interpretQuery } = require("./queryInterpreter");
const { normalizeProducts } = require("./normalize");
const { rankProducts } = require("./ranking");
const scrapeAmazon = require("../scrapers/amazon");
const scrapeEbay = require("../scrapers/ebay");
const scrapeAliExpress = require("../scrapers/aliexpress");
const logger = require("../utils/logger");
const { formatEuros, round } = require("../utils/price");

const searchCache = new MemoryCache(env.cacheTtlMs);
const SCRAPER_TIMEOUT_MS = 12000;

const SCRAPERS = [
  { source: "amazon", handler: scrapeAmazon },
  { source: "ebay", handler: scrapeEbay },
  { source: "aliexpress", handler: scrapeAliExpress }
];

function pickQueryForSource(source, queries) {
  const normalized = queries.map((query) => String(query || "").trim()).filter(Boolean);
  return (
    normalized.find((query) => query.toLowerCase().includes(source)) ||
    normalized[0] ||
    ""
  );
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

async function searchAllSources(queries) {
  // Cada marketplace usa solo la query más relevante para mantener coste y latencia bajos.
  const tasks = SCRAPERS.map(async ({ source, handler }) => {
    const sourceQuery = pickQueryForSource(source, queries);
    if (!sourceQuery) {
      return [];
    }

    try {
      return await withTimeout(handler(sourceQuery), SCRAPER_TIMEOUT_MS, `El scraper ${source}`);
    } catch (error) {
      logger.warn(`El scraper ${source} ha fallado.`, error.message);
      return [];
    }
  });

  const results = await Promise.all(tasks);
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

function computeAveragePrice(products) {
  const prices = products.map((product) => product.price).filter(Number.isFinite);
  if (!prices.length) {
    return null;
  }
  const average = prices.reduce((sum, price) => sum + price, 0) / prices.length;
  return round(average);
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

  takeOne(
    (items) => items[0],
    "Mejor opcion",
    "Mejor opcion"
  );

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
    (items) =>
      [...items].sort((left, right) => left.price - right.price)[0],
    "Opcion mas barata",
    "Mas barato"
  );

  return featured;
}

function buildFallbackResponse(userInput) {
  return {
    query_original: userInput,
    query_mejorada: userInput,
    interpretation: null,
    cached: false,
    precio_medio: null,
    productos: [],
    top_3_mejores_opciones: [],
    message:
      "No hemos podido recuperar ofertas ahora mismo. Intenta de nuevo en unos segundos."
  };
}

async function executeSearch(userInput) {
  const cacheKey = userInput.trim().toLowerCase();
  const cached = searchCache.get(cacheKey);
  if (cached) {
    return { ...cached, cached: true };
  }

  const interpretation = await interpretQuery(userInput);
  const rawProducts = await searchAllSources(interpretation.queries);
  const normalizedProducts = normalizeProducts(rawProducts);
  const averagePrice = computeAveragePrice(normalizedProducts);
  const enrichedProducts = markBargains(normalizedProducts, averagePrice);
  const rankedProducts = rankProducts(enrichedProducts);

  if (!rankedProducts.length) {
    return buildFallbackResponse(userInput);
  }

  const featuredProducts = pickFeaturedProducts(rankedProducts).map(withSpanishShape);
  const allProducts = rankedProducts.map(withSpanishShape);

  // Mantenemos la forma de respuesta que el frontend actual ya entiende.
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
