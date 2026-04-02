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

function cleanAmazonTitle(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/^\s+|\s+$/g, "")
    .replace(/^\(?\d+\+?\s+ofertas?.*?\)?$/i, "")
    .replace(/^\d+[.,]?\d*$/i, "")
    .replace(/^\d+[.,]\d+\s*\u20AC.*$/i, "")
    .trim();
}

function isLikelyAmazonProductTitle(title) {
  const cleaned = cleanAmazonTitle(title);
  if (!cleaned) {
    return false;
  }

  const normalized = cleaned.toLowerCase();
  if (cleaned.length < 12) {
    return false;
  }
  if (!/[a-z]/i.test(cleaned)) {
    return false;
  }
  if (/^\(?\d+\+?\s+ofertas?/i.test(normalized)) {
    return false;
  }
  if (/^\d+[.,]?\d*$/.test(normalized)) {
    return false;
  }
  if (/^\d+[.,]\d+\s*\u20ac/.test(normalized)) {
    return false;
  }

  return true;
}

async function scrapeAmazon(query) {
  const searchUrl = `https://www.amazon.es/s?k=${encodeURIComponent(query)}`;
  const html = await fetchHtml(searchUrl);
  const $ = cheerio.load(html);
  const items = [];

  $('[data-component-type="s-search-result"]').each((_, element) => {
    if (items.length >= env.maxResultsPerSource) {
      return false;
    }

    const rawTitle =
      $(element).find("h2 a span").first().text().trim() ||
      $(element).find("h2 span").first().text().trim() ||
      $(element).find("a h2 span").first().text().trim() ||
      "";
    const title = cleanAmazonTitle(rawTitle);
    const url = absoluteAmazonUrl($(element).find("h2 a").attr("href"));
    const price = $(element).find(".a-price .a-offscreen").first().text().trim() || "";
    const originalPrice =
      $(element).find(".a-price.a-text-price .a-offscreen").first().text().trim() ||
      $(element).find(".a-text-price .a-offscreen").first().text().trim() ||
      "";
    const ratingText =
      $(element).find(".a-icon-star-small .a-icon-alt").first().text().trim() ||
      $(element).find("[aria-label*='de 5 estrellas']").first().attr("aria-label") ||
      "";
    const ratingMatch = ratingText.match(/([\d,.]+)/);

    if (!title || !isLikelyAmazonProductTitle(title) || !url || !price) {
      return;
    }

    items.push({
      title,
      price,
      original_price: originalPrice || null,
      discount: null,
      rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
      source: "amazon",
      url
    });
  });

  if (!items.length) {
    $('a[href*="/dp/"], a[href*="/gp/"]').each((_, element) => {
      if (items.length >= env.maxResultsPerSource) {
        return false;
      }

      const container = $(element).closest("div");
      const rawTitle =
        $(element).find("span").first().text().trim() ||
        $(element).attr("aria-label") ||
        $(element).text().trim();
      const title = cleanAmazonTitle(rawTitle);
      const url = absoluteAmazonUrl($(element).attr("href"));
      const price =
        container.find(".a-offscreen").first().text().trim() ||
        container.text().match(/(\d+[.,]\d+)\s*\u20AC/i)?.[0] ||
        "";

      if (!title || !isLikelyAmazonProductTitle(title) || !url || !price) {
        return;
      }

      items.push({
        title,
        price,
        original_price: null,
        discount: null,
        rating: null,
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
