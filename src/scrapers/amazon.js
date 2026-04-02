const cheerio = require("cheerio");
const env = require("../config/env");
const { fetchHtml } = require("../services/requestClient");

function absoluteAmazonUrl(url) {
  if (!url) {
    return null;
  }
  return url.startsWith("http") ? url : `https://www.amazon.es${url}`;
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

    const title = $(element).find("h2 a span").first().text().trim();
    const url = absoluteAmazonUrl($(element).find("h2 a").attr("href"));
    const price =
      $(element).find(".a-price .a-offscreen").first().text().trim() ||
      "";
    const originalPrice =
      $(element).find(".a-price.a-text-price .a-offscreen").first().text().trim() ||
      $(element).find(".a-text-price .a-offscreen").first().text().trim() ||
      "";
    const ratingText =
      $(element).find(".a-icon-star-small .a-icon-alt").first().text().trim() ||
      $(element).find("[aria-label*='de 5 estrellas']").first().attr("aria-label") ||
      "";
    const ratingMatch = ratingText.match(/([\d,.]+)/);

    if (!title || !url || !price) {
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

  return items;
}

module.exports = scrapeAmazon;
