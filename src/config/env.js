const path = require("path");

function toNumber(value, fallback) {
  const parsed = Number(value);
  return Number.isFinite(parsed) ? parsed : fallback;
}

const rootDir = path.resolve(__dirname, "..", "..");

module.exports = {
  rootDir,
  publicDir: path.join(rootDir, "public"),
  port: toNumber(process.env.PORT, 8000),
  nodeEnv: process.env.NODE_ENV || "development",
  openAiApiKey: process.env.OPENAI_API_KEY || "",
  openAiModel: process.env.OPENAI_MODEL || "gpt-5.4-mini",
  amazonAffiliateTag: process.env.AMAZON_AFFILIATE_TAG || "",
  bargainThreshold: toNumber(process.env.BARGAIN_THRESHOLD, 0.82),
  cacheTtlMs: toNumber(process.env.SEARCH_CACHE_TTL_MS, 8 * 60 * 60 * 1000),
  browserTimeoutMs: toNumber(process.env.BROWSER_TIMEOUT_MS, 10000),
  browserNavigationTimeoutMs: toNumber(process.env.BROWSER_NAVIGATION_TIMEOUT_MS, 10000),
  scraperTimeoutMs: toNumber(process.env.SCRAPER_TIMEOUT_MS, 25000),
  maxResultsPerSource: toNumber(process.env.MAX_RESULTS_PER_SOURCE, 6),
  maxQueriesGenerated: toNumber(process.env.MAX_QUERIES_GENERATED, 5),
  maxConcurrentScrapers: toNumber(process.env.MAX_CONCURRENT_SCRAPERS, 2),
  browserHeadless: process.env.BROWSER_HEADLESS !== "false",
  logLevel: process.env.LOG_LEVEL || "info",
  proxyServer: process.env.PROXY_SERVER || "",
  appBaseUrl: process.env.APP_BASE_URL || ""
};
