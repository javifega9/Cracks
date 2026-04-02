const env = require("../config/env");
const { withPage } = require("../services/browser");

function cleanAliExpressTitle(value) {
  return String(value || "")
    .replace(/\s+/g, " ")
    .replace(/^\s+|\s+$/g, "")
    .replace(/\b(?:\d+[,.]\d+\s*(?:€|eur|usd)|\d+\s*%|sold|verkauft|vorschau anzeigen|ahnliche artikel)\b/gi, "")
    .replace(/\s{2,}/g, " ")
    .trim();
}

async function scrapeAliExpress(query) {
  return withPage(async (page) => {
    const searchUrl = `https://www.aliexpress.com/wholesale?SearchText=${encodeURIComponent(query)}`;
    await page.goto(searchUrl, { waitUntil: "domcontentloaded", timeout: 12000 });
    await page
      .waitForSelector('a[href*="/item/"], a[href*="aliexpress.com/item/"]', {
        timeout: 4000
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
          const rawTitle =
            card.getAttribute("title") ||
            card.getAttribute("aria-label") ||
            card.querySelector("img")?.getAttribute("alt") ||
            container?.querySelector("img")?.getAttribute("alt") ||
            container?.querySelector("h1, h2, h3, h4")?.textContent ||
            card.textContent ||
            "";
          const title = cleanAliExpressTitle(rawTitle);
          const priceMatch =
            text.match(/\u20AC\s*([\d,.]+)/) ||
            text.match(/([\d,.]+)\s*\u20AC/) ||
            text.match(/\$\s*([\d,.]+)/) ||
            text.match(/([\d,.]+)\s*USD/i);
          const oldPriceMatch =
            text.match(/\u20AC\s*([\d,.]+).*?(\d+)\s*%/) ||
            text.match(/([\d,.]+)\s*\u20AC\s*.*?(\d+)\s*%/) ||
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
