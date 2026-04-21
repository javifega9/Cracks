const env = require("../config/env");
const MemoryCache = require("./cache/memoryCache");
const { interpretQuery } = require("./queryInterpreter");

const searchCache = new MemoryCache(env.cacheTtlMs);

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

function buildStableCards(query) {
  return [
    {
      titulo: `${query} en Amazon`,
      precio: "Abrir busqueda",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(query),
      url: buildAmazonSearchUrl(query),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Mejor opcion",
      etiqueta: "Mejor opcion",
      explicacion: "Abre la busqueda principal en Amazon para ver las opciones mas relevantes."
    },
    {
      titulo: `${query} mas baratos en Amazon`,
      precio: "Ordenar por precio",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(query, { s: "price-asc-rank" }),
      url: buildAmazonSearchUrl(query, { s: "price-asc-rank" }),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Opcion mas barata",
      etiqueta: "Mas barato",
      explicacion: "Muestra primero las opciones mas economicas para decidir rapido."
    },
    {
      titulo: `${query} mejor valorados en Amazon`,
      precio: "Ordenar por valoracion",
      precio_numerico: null,
      precio_original: null,
      descuento: null,
      rating: null,
      tienda: "Amazon",
      source: "amazon",
      link: buildAmazonSearchUrl(query, { s: "review-rank" }),
      url: buildAmazonSearchUrl(query, { s: "review-rank" }),
      es_chollo: false,
      es_amazon: true,
      es_afiliado_amazon: Boolean(env.amazonAffiliateTag),
      categoria_destacada: "Mejor calidad-precio",
      etiqueta: "Mejor calidad-precio",
      explicacion: "Prioriza productos mejor valorados para encontrar opciones mas fiables."
    }
  ];
}

function buildStableAmazonResponse(userInput, interpretation) {
  const baseQuery =
    String(interpretation?.product || userInput || "")
      .replace(/\s+/g, " ")
      .trim() || String(userInput || "").trim();
  const cards = buildStableCards(baseQuery);

  return {
    query_original: String(userInput || "").trim(),
    query_mejorada: baseQuery,
    interpretation,
    cached: false,
    precio_medio: null,
    productos: cards,
    top_3_mejores_opciones: cards,
    message: "Mostramos accesos directos estables a Amazon para que puedas decidir rapido sin depender de scraping fragil."
  };
}

async function executeSearch(userInput) {
  const trimmed = String(userInput || "").trim();
  const cacheKey = trimmed.toLowerCase();
  const cached = searchCache.get(cacheKey);
  if (cached) {
    return { ...cached, cached: true };
  }

  const interpretation = await interpretQuery(trimmed);
  const payload = buildStableAmazonResponse(trimmed, interpretation);
  searchCache.set(cacheKey, payload);
  return payload;
}

module.exports = {
  executeSearch,
  searchCache
};
