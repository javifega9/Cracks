const env = require("../config/env");
const { withPage } = require("../services/browser");

function absoluteAmazonUrl(url) {
  if (!url) {
    return null;
  }
  return url.startsWith("http") ? url : `https://www.amazon.es${url}`;
}

async function scrapeAmazon(query) {
  return withPage(async (page) => {
    const searchUrl = `https://www.amazon.es/s?k=${encodeURIComponent(query)}`;
    await page.goto(searchUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 4000 }).catch(() => {});
    await page.waitForSelector('[data-component-type="s-search-result"]', { timeout: 5000 }).catch(() => {});

    const items = await page.$$eval(
      '[data-component-type="s-search-result"]',
      (nodes, limit) =>
        nodes.map((node) => {
          const title = node.querySelector("h2 span")?.textContent?.trim() || "";
          const priceText =
            node.querySelector(".a-price .a-offscreen")?.textContent?.trim() ||
            "";
          const oldPriceText =
            node.querySelector(".a-price.a-text-price .a-offscreen")?.textContent?.trim() ||
            node.querySelector(".a-text-price .a-offscreen")?.textContent?.trim() ||
            "";
          const ratingText =
            node.querySelector(".a-icon-star-small .a-icon-alt")?.textContent?.trim() ||
            node.querySelector("[aria-label*='de 5 estrellas']")?.getAttribute("aria-label") ||
            "";
          const link = node.querySelector("h2 a")?.getAttribute("href") || "";

          const ratingMatch = ratingText.match(/([\d,.]+)/);

          return {
            title,
            price: priceText,
            original_price: oldPriceText,
            discount: null,
            rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
            source: "amazon",
            url: link
          };
        }),
      env.maxResultsPerSource * 3
    );

    return items
      .map((item) => ({ ...item, url: absoluteAmazonUrl(item.url) }))
      .filter((item) => item.title && item.url && item.price)
      .slice(0, env.maxResultsPerSource);
  });
}

module.exports = scrapeAmazon;
