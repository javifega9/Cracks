const cheerio = require("cheerio");
const env = require("../config/env");
const { fetchHtml } = require("../services/requestClient");
const { withPage } = require("../services/browser");
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
    /^\d+[.,]\d+\s*u20ac/,
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

function sanitizeItems(items, query) {
  const seen = new Set();
  return (items || [])
    .map((item) => ({
      title: cleanAmazonTitle(item.title),
      price: String(item.price || "").trim(),
      original_price: item.original_price ? String(item.original_price).trim() : null,
      discount: item.discount ?? null,
      rating: item.rating ?? null,
      source: "amazon",
      url: absoluteAmazonUrl(item.url)
    }))
    .filter((item) => {
      if (!item.title || !isLikelyAmazonProductTitle(item.title) || !titleMatchesQuery(item.title, query) || !item.url || !item.price) {
        return false;
      }
      const key = `${item.title}|${item.url}`;
      if (seen.has(key)) {
        return false;
      }
      seen.add(key);
      return true;
    })
    .slice(0, env.maxResultsPerSource);
}

async function scrapeAmazonWithBrowser(query) {
  const searchUrl = `https://www.amazon.es/s?k=${encodeURIComponent(query)}`;
  return withPage(async (page) => {
    await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 20000 });
    await page.waitForSelector('div[data-asin]:not([data-asin=""]) h2 a', { timeout: 8000 }).catch(() => {});

    const items = await page.$$eval('div[data-asin]:not([data-asin=""])', (nodes, limit) => {
      const results = [];

      for (const node of nodes) {
        if (results.length >= limit) {
          break;
        }

        const anchor = node.querySelector("h2 a");
        const title =
          anchor?.textContent?.trim() ||
          node.querySelector("h2")?.textContent?.trim() ||
          "";
        const href = anchor?.getAttribute("href") || "";
        const price =
          node.querySelector(".a-price .a-offscreen")?.textContent?.trim() ||
          "";
        const originalPrice =
          node.querySelector(".a-price.a-text-price .a-offscreen")?.textContent?.trim() ||
          node.querySelector(".a-text-price .a-offscreen")?.textContent?.trim() ||
          "";
        const ratingText =
          node.querySelector(".a-icon-star-small .a-icon-alt")?.textContent?.trim() ||
          node.querySelector("[aria-label*='de 5 estrellas']")?.getAttribute("aria-label") ||
          "";
        const ratingMatch = ratingText.match(/([\d,.]+)/);

        results.push({
          title,
          price,
          original_price: originalPrice,
          discount: null,
          rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
          url: href
        });
      }

      return results;
    }, env.maxResultsPerSource * 4);

    return sanitizeItems(items, query);
  });
}

function extractPrice($, container) {
  const offscreen = container.find(".a-price .a-offscreen").first().text().trim();
  if (offscreen) {
    return offscreen;
  }

  const whole = container.find(".a-price-whole").first().text().trim().replace(/\./g, "");
  const fraction = container.find(".a-price-fraction").first().text().trim();
  if (whole && fraction) {
    return `${whole},${fraction} EUR`;
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

async function scrapeAmazonWithHtml(query) {
  const searchUrl = `https://www.amazon.es/s?k=${encodeURIComponent(query)}`;
  const html = await fetchHtml(searchUrl);
  const $ = cheerio.load(html);
  const rawItems = [];

  $('div[data-asin]:not([data-asin=""])').each((_, element) => {
    if (rawItems.length >= env.maxResultsPerSource * 4) {
      return false;
    }

    const container = $(element);
    const anchor = container.find("h2 a").first();
    rawItems.push({
      title: anchor.text().trim() || container.find("h2").first().text().trim() || "",
      price: extractPrice($, container),
      original_price: extractOriginalPrice($, container) || null,
      discount: null,
      rating: extractRating($, container),
      url: anchor.attr("href")
    });
  });

  const items = sanitizeItems(rawItems, query);
  if (!items.length) {
    const title = $("title").first().text().trim();
    logger.warn(`Amazon ha devuelto 0 resultados para "${query}".`, title || "Sin titulo de pagina");
  }

  return items;
}

async function scrapeAmazon(query) {
  try {
    const browserItems = await scrapeAmazonWithBrowser(query);
    if (browserItems.length) {
      return browserItems;
    }
  } catch (error) {
    logger.warn(`Amazon browser fallback para "${query}".`, error.message);
  }

  return scrapeAmazonWithHtml(query);
}

module.exports = scrapeAmazon;
