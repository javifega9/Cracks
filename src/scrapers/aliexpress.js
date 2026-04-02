const env = require("../config/env");
const { withPage } = require("../services/browser");

async function scrapeAliExpress(query) {
  return withPage(async (page) => {
    const searchUrl = `https://www.aliexpress.com/wholesale?SearchText=${encodeURIComponent(query)}`;
    await page.goto(searchUrl, { waitUntil: "domcontentloaded" });
    await page.waitForLoadState("networkidle", { timeout: 5000 }).catch(() => {});
    await page
      .waitForSelector('a[href*="/item/"], a[href*="aliexpress.com/item/"]', {
        timeout: 6000
      })
      .catch(() => {});

    return page
      .evaluate((limit) => {
        const cards = Array.from(
          document.querySelectorAll('a[href*="/item/"], a[href*="aliexpress.com/item/"]')
        );
        const seen = new Set();
        const results = [];

        for (const card of cards) {
          const container = card.closest("div")?.parentElement || card.parentElement || card;
          const text = container?.textContent || "";
          const href = card.getAttribute("href") || "";
          const title = card.getAttribute("title") || card.textContent?.trim() || "";
          const priceMatch =
            text.match(/€\s*([\d,.]+)/) ||
            text.match(/([\d,.]+)\s*€/) ||
            text.match(/\$\s*([\d,.]+)/) ||
            text.match(/([\d,.]+)\s*USD/i);
          const oldPriceMatch =
            text.match(/€\s*([\d,.]+).*?(\d+)\s*%/) ||
            text.match(/([\d,.]+)\s*€\s*.*?(\d+)\s*%/) ||
            null;
          const discountMatch = text.match(/(\d+)\s*%/);
          const ratingMatch = text.match(/([\d,.]+)\s*(?:de 5|\/5|stars?)/i);

          const key = `${title}|${href}`;
          if (!title || !href || seen.has(key)) {
            continue;
          }

          seen.add(key);
          results.push({
            title: title.trim(),
            price: priceMatch ? priceMatch[1] : null,
            original_price: oldPriceMatch ? oldPriceMatch[1] : null,
            discount: discountMatch ? discountMatch[1] : null,
            rating: ratingMatch ? ratingMatch[1].replace(",", ".") : null,
            source: "aliexpress",
            url: href.startsWith("http") ? href : `https:${href}`
          });

          if (results.length >= limit) {
            break;
          }
        }

        return results;
      }, env.maxResultsPerSource * 3)
      .then((items) =>
        items.filter((item) => item.title && item.url && item.price).slice(0, env.maxResultsPerSource)
      );
  });
}

module.exports = scrapeAliExpress;
