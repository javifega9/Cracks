const cheerio = require("cheerio");
const env = require("../config/env");
const { fetchHtml } = require("../services/requestClient");
const logger = require("../utils/logger");

async function scrapeEbay(query) {
  const searchUrl = `https://www.ebay.es/sch/i.html?_nkw=${encodeURIComponent(query)}&_ipg=${env.maxResultsPerSource}&rt=nc`;
  const html = await fetchHtml(searchUrl);
  const $ = cheerio.load(html);
  const items = [];

  $(".s-item").each((_, element) => {
    if (items.length >= env.maxResultsPerSource) {
      return false;
    }

    const title = $(element).find(".s-item__title").first().text().trim();
    const url = $(element).find(".s-item__link").first().attr("href") || "";
    const price = $(element).find(".s-item__price").first().text().trim();
    const originalPrice =
      $(element).find(".s-item__trending-price .STRIKETHROUGH").first().text().trim() ||
      "";
    const discountText =
      $(element).find(".s-item__discount").first().text().trim() ||
      $(element).find(".s-item__dynamic.s-item__saving").first().text().trim() ||
      "";
    const ratingText = $(element).find(".x-star-rating span.clipped").first().text().trim();
    const ratingMatch = ratingText.match(/([\d,.]+)/);
    const discountMatch = discountText.match(/(\d+)/);

    if (!title || !url || !price || title.toLowerCase().includes("shop on ebay")) {
      return;
    }

    items.push({
      title,
      price,
      original_price: originalPrice || null,
      discount: discountMatch ? discountMatch[1] : null,
      rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
      source: "ebay",
      url
    });
  });

  if (!items.length) {
    $('a[href*="/itm/"]').each((_, element) => {
      if (items.length >= env.maxResultsPerSource) {
        return false;
      }

      const container = $(element).closest("li, div");
      const title =
        $(element).attr("aria-label") ||
        $(element).find("span").first().text().trim() ||
        $(element).text().trim();
      const url = $(element).attr("href") || "";
      const price =
        container.find(".s-item__price").first().text().trim() ||
        container.text().match(/(\d+[.,]\d+)\s*\u20AC/i)?.[0] ||
        "";

      if (!title || !url || !price || title.toLowerCase().includes("shop on ebay")) {
        return;
      }

      items.push({
        title,
        price,
        original_price: null,
        discount: null,
        rating: null,
        source: "ebay",
        url
      });
    });
  }

  if (!items.length) {
    const title = $("title").first().text().trim();
    logger.warn(`eBay ha devuelto 0 resultados para "${query}".`, title || "Sin titulo de pagina");
  }

  return items;
}

module.exports = scrapeEbay;
