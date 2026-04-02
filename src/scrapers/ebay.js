const env = require("../config/env");
const { withPage } = require("../services/browser");

async function scrapeEbay(query) {
  return withPage(async (page) => {
    const searchUrl = `https://www.ebay.es/sch/i.html?_nkw=${encodeURIComponent(query)}`;
    await page.goto(searchUrl, { waitUntil: "domcontentloaded" });
    await page.waitForSelector(".s-item", { timeout: 5000 }).catch(() => {});

    return page.$$eval(
      ".s-item",
      (nodes, limit) =>
        nodes.slice(0, limit).map((node) => {
          const title = node.querySelector(".s-item__title")?.textContent?.trim() || "";
          const priceText = node.querySelector(".s-item__price")?.textContent?.trim() || "";
          const oldPriceText =
            node.querySelector(".s-item__trending-price .STRIKETHROUGH")?.textContent?.trim() ||
            "";
          const discountText =
            node.querySelector(".s-item__discount")?.textContent?.trim() ||
            node.querySelector(".s-item__dynamic.s-item__saving")?.textContent?.trim() ||
            "";
          const ratingText = node.querySelector(".x-star-rating span.clipped")?.textContent?.trim() || "";
          const link = node.querySelector(".s-item__link")?.getAttribute("href") || "";

          const ratingMatch = ratingText.match(/([\d,.]+)/);
          const discountMatch = discountText.match(/(\d+)/);

          return {
            title,
            price: priceText,
            original_price: oldPriceText,
            discount: discountMatch ? discountMatch[1] : null,
            rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
            source: "ebay",
            url: link
          };
        }),
      env.maxResultsPerSource
    ).then((items) =>
      items.filter((item) => item.title && item.url && !item.title.toLowerCase().includes("shop on ebay"))
    );
  });
}

module.exports = scrapeEbay;
