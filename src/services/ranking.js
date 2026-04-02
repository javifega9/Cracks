function computePriceScores(products) {
  const prices = products.map((product) => product.price).filter(Number.isFinite);
  const minPrice = Math.min(...prices);
  const maxPrice = Math.max(...prices);

  return products.map((product) => {
    let priceScore = 100;
    if (maxPrice > minPrice) {
      priceScore = ((maxPrice - product.price) / (maxPrice - minPrice)) * 100;
    }

    const discount = Number.isFinite(product.discount) ? product.discount : 0;
    const ratingScore = Number.isFinite(product.rating) ? product.rating * 20 : 0;
    const score = discount * 0.5 + ratingScore * 0.3 + priceScore * 0.2;

    return {
      ...product,
      score: Math.round(score * 100) / 100,
      priceScore: Math.round(priceScore * 100) / 100
    };
  });
}

function rankProducts(products) {
  return computePriceScores(products).sort((left, right) => right.score - left.score);
}

module.exports = {
  rankProducts
};
