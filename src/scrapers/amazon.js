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
    await page.waitForSelector('[data-component-type="s-search-result"]', { timeout: 5000 }).catch(() => {});

    return page.$$eval(
      '[data-component-type="s-search-result"]',
      (nodes, limit) =>
        nodes.slice(0, limit).map((node) => {
          const title = node.querySelector("h2 span")?.textContent?.trim() || "";
          const whole = node.querySelector(".a-price .a-price-whole")?.textContent || "";
          const fraction = node.querySelector(".a-price .a-price-fraction")?.textContent || "";
          const priceText = `${whole}${fraction ? `.${fraction}` : ""}`;
          const oldPriceText =
            node.querySelector(".a-price.a-text-price .a-offscreen")?.textContent ||
            node.querySelector(".a-text-price .a-offscreen")?.textContent ||
            "";
          const ratingText =
            node.querySelector("[aria-label*='de 5 estrellas']")?.getAttribute("aria-label") || "";
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
      env.maxResultsPerSource
    ).then((items) =>
      items
        .map((item) => ({ ...item, url: absoluteAmazonUrl(item.url) }))
        .filter((item) => item.title && item.url)
    );
  });
}

module.exports = scrapeAmazon;
