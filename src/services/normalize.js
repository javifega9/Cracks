const { calculateDiscount, round, toNumber } = require("../utils/price");

function normalizeProducts(products) {
  return (products || [])
    .map((product) => {
      const title = String(product?.title || "").trim();
      const url = String(product?.url || "").trim();
      const price = toNumber(product?.price);
      const originalPrice = toNumber(product?.original_price);
      const discount =
        toNumber(product?.discount) ?? calculateDiscount(price, originalPrice);
      const rating = toNumber(product?.rating);

      if (!title || !url || !Number.isFinite(price)) {
        return null;
      }

      return {
        title,
        price: round(price),
        original_price: Number.isFinite(originalPrice) ? round(originalPrice) : null,
        discount: Number.isFinite(discount) ? round(discount) : null,
        rating: Number.isFinite(rating) ? round(rating, 1) : null,
        source: String(product?.source || "").trim().toLowerCase() || "unknown",
        url
      };
    })
    .filter(Boolean);
}

module.exports = {
  normalizeProducts
};
