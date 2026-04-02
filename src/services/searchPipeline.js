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
const SCRAPER_TIMEOUT_MS = env.scraperTimeoutMs;

const SCRAPERS = [
  { source: "amazon", handler: scrapeAmazon },
  { source: "ebay", handler: scrapeEbay },
  { source: "aliexpress", handler: scrapeAliExpress }
];

function uniqueQueries(queries) {
  return [...new Set((queries || []).map((query) => String(query || "").trim()).filter(Boolean))];
}

function pickQueriesForSource(source, interpretation, userInput) {
  const normalized = uniqueQueries(interpretation?.queries || []);
  const sourceSpecific = normalized.filter((query) => query.toLowerCase().includes(source));
  const genericBase = String(interpretation?.product || userInput || "")
    .replace(/\b(amazon|ebay|aliexpress)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
  const generic = [genericBase, userInput];

  return uniqueQueries([sourceSpecific[0], ...generic]).slice(0, 2);
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
        logger.info(`Scraper ${source}: ${items.length} resultados para "${sourceQuery}"`);
        if (items.length) {
          return items;
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

function buildSearchTokens(userInput, interpretation) {
  return uniqueQueries([
    interpretation?.product,
    userInput
  ])
    .join(" ")
    .toLowerCase()
    .split(/[^a-z0-9]+/i)
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
  const queryText = `${interpretation?.product || ""} ${userInput}`.toLowerCase();
  const userAskedForAccessory = accessoryKeywords.some((word) => queryText.includes(word));

  return products.filter((product) => {
    const title = String(product?.title || "").toLowerCase();
    if (!title) {
      return false;
    }

    if (!userAskedForAccessory && accessoryKeywords.some((word) => title.includes(word))) {
      return false;
    }

    if (!tokens.length) {
      return true;
    }

    const matchingTokens = tokens.filter((token) => title.includes(token));
    return matchingTokens.length >= Math.min(2, tokens.length);
  });
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
  return {
    query_original: userInput,
    query_mejorada: userInput,
    interpretation: null,
    cached: false,
    precio_medio: null,
    productos: [],
    top_3_mejores_opciones: [],
    message: "No hemos podido recuperar ofertas ahora mismo. Intenta de nuevo en unos segundos."
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
  const intentFilteredProducts = filterProductsForIntent(rawProducts, userInput, interpretation);
  const normalizedProducts = normalizeProducts(intentFilteredProducts);
  const averagePrice = computeAveragePrice(normalizedProducts);
  const enrichedProducts = markBargains(normalizedProducts, averagePrice);
  const rankedProducts = rankProducts(enrichedProducts);

  logger.info(
    `Pipeline de busqueda: ${rawProducts.length} raw, ${intentFilteredProducts.length} filtrados, ${normalizedProducts.length} normalizados, ${rankedProducts.length} finales.`
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
