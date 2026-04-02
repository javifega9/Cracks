const cheerio = require("cheerio");
const env = require("../config/env");
const { fetchHtml } = require("../services/requestClient");
const logger = require("../utils/logger");

function absoluteAmazonUrl(url) {
  if (!url) {
    return null;
  }
  return url.startsWith("http") ? url : `https://www.amazon.es${url}`;
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

function cleanAmazonTitle(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/^\s+|\s+$/g, "")
    .replace(/^\(?\d+\+?\s+ofertas?.*?\)?$/i, "")
    .replace(/^precio,\s*pagina del producto$/i, "")
    .replace(/^recibelo mas rapido$/i, "")
    .replace(/^\d+[.,]?\d*$/i, "")
    .replace(/^\d+[.,]\d+\s*\u20AC.*$/i, "")
    .trim();
}

function isLikelyAmazonProductTitle(title) {
  const cleaned = cleanAmazonTitle(title);
  if (!cleaned) {
    return false;
  }

  const normalized = normalizeText(cleaned);
  if (cleaned.length < 12) {
    return false;
  }
  if (!/[a-z]/i.test(cleaned)) {
    return false;
  }

  const blockedPatterns = [
    /^\(?\d+\+?\s+ofertas?/i,
    /^\d+[.,]?\d*$/,
    /^\d+[.,]\d+\s*\u20ac/,
    /^precio pagina del producto$/,
    /^recibelo mas rapido$/
  ];

  return !blockedPatterns.some((pattern) => pattern.test(normalized));
}

function titleMatchesQuery(title, query) {
  const normalizedTitle = normalizeText(title);
  const tokens = normalizeText(query)
    .split(" ")
    .filter((token) => token && token.length >= 2 && !["oferta", "amazon", "barato", "mejor", "precio"].includes(token));

  if (!tokens.length) {
    return true;
  }

  const alphaTokens = tokens.filter((token) => /[a-z]/.test(token));
  const numericTokens = tokens.filter((token) => /\d/.test(token));

  const hasAlpha = !alphaTokens.length || alphaTokens.some((token) => normalizedTitle.includes(token));
  const hasNumeric = !numericTokens.length || numericTokens.some((token) => normalizedTitle.includes(token));

  return hasAlpha && hasNumeric;
}

function extractPrice($, container) {
  const offscreen = container.find(".a-price .a-offscreen").first().text().trim();
  if (offscreen) {
    return offscreen;
  }

  const whole = container.find(".a-price-whole").first().text().trim().replace(/\./g, "");
  const fraction = container.find(".a-price-fraction").first().text().trim();
  if (whole && fraction) {
    return `${whole},${fraction} €`;
  }

  return "";
}

function extractOriginalPrice($, container) {
  return (
    container.find(".a-price.a-text-price .a-offscreen").first().text().trim() ||
    container.find(".a-text-price .a-offscreen").first().text().trim() ||
    ""
  );
}

function extractRating($, container) {
  const ratingText =
    container.find(".a-icon-star-small .a-icon-alt").first().text().trim() ||
    container.find("[aria-label*='de 5 estrellas']").first().attr("aria-label") ||
    "";
  const ratingMatch = ratingText.match(/([\d,.]+)/);
  return ratingMatch ? ratingMatch[1].replace(",", ".") : null;
}

function pushIfValid(items, candidate) {
  if (!candidate.title || !candidate.url || !candidate.price) {
    return;
  }

  items.push(candidate);
}

async function scrapeAmazon(query) {
  const searchUrl = `https://www.amazon.es/s?k=${encodeURIComponent(query)}`;
  const html = await fetchHtml(searchUrl);
  const $ = cheerio.load(html);
  const items = [];
  const seen = new Set();

  $('div[data-asin]:not([data-asin=""])').each((_, element) => {
    if (items.length >= env.maxResultsPerSource) {
      return false;
    }

    const container = $(element);
    const anchor = container.find("h2 a").first();
    const rawTitle =
      anchor.text().trim() ||
      container.find("h2").first().text().trim() ||
      "";
    const title = cleanAmazonTitle(rawTitle);
    const url = absoluteAmazonUrl(anchor.attr("href"));
    const price = extractPrice($, container);
    const originalPrice = extractOriginalPrice($, container);
    const rating = extractRating($, container);

    if (!title || !isLikelyAmazonProductTitle(title) || !titleMatchesQuery(title, query) || !url || !price) {
      return;
    }

    const key = `${title}|${url}`;
    if (seen.has(key)) {
      return;
    }

    seen.add(key);
    pushIfValid(items, {
      title,
      price,
      original_price: originalPrice || null,
      discount: null,
      rating,
      source: "amazon",
      url
    });
  });

  if (!items.length) {
    $('h2 a[href*="/dp/"], h2 a[href*="/gp/"]').each((_, element) => {
      if (items.length >= env.maxResultsPerSource) {
        return false;
      }

      const anchor = $(element);
      const container = anchor.closest('div[data-asin], [data-component-type="s-search-result"], .s-result-item, .sg-col-inner, .a-section');
      const rawTitle = anchor.text().trim() || "";
      const title = cleanAmazonTitle(rawTitle);
      const url = absoluteAmazonUrl(anchor.attr("href"));
      const price = extractPrice($, container);
      const originalPrice = extractOriginalPrice($, container);
      const rating = extractRating($, container);

      if (!title || !isLikelyAmazonProductTitle(title) || !titleMatchesQuery(title, query) || !url || !price) {
        return;
      }

      const key = `${title}|${url}`;
      if (seen.has(key)) {
        return;
      }

      seen.add(key);
      pushIfValid(items, {
        title,
        price,
        original_price: originalPrice || null,
        discount: null,
        rating,
        source: "amazon",
        url
      });
    });
  }

  if (!items.length) {
    const title = $("title").first().text().trim();
    logger.warn(`Amazon ha devuelto 0 resultados para "${query}".`, title || "Sin titulo de pagina");
  }

  return items;
}

module.exports = scrapeAmazon;
