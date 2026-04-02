const { chromium } = require("playwright");
const env = require("../config/env");
const logger = require("../utils/logger");

let browserPromise = null;

async function getBrowser() {
  if (!browserPromise) {
    browserPromise = chromium.launch({
      headless: env.browserHeadless,
      proxy: env.proxyServer ? { server: env.proxyServer } : undefined
    }).catch((error) => {
      browserPromise = null;
      throw error;
    });
  }

  return browserPromise;
}

async function createPage() {
  const browser = await getBrowser();
  const context = await browser.newContext({
    locale: "es-ES",
    userAgent:
      "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
  });

  // Reducimos peso bloqueando recursos no necesarios para extracción de datos.
  await context.route("**/*", (route) => {
    const type = route.request().resourceType();
    if (["image", "font", "media"].includes(type)) {
      return route.abort();
    }
    return route.continue();
  });

  const page = await context.newPage();
  page.setDefaultTimeout(env.browserTimeoutMs);
  page.setDefaultNavigationTimeout(env.browserNavigationTimeoutMs);

  return { context, page };
}

async function withPage(handler) {
  const { context, page } = await createPage();
  try {
    return await handler(page);
  } finally {
    await context.close().catch((error) => {
      logger.warn("No se pudo cerrar el contexto de Playwright.", error.message);
    });
  }
}

async function shutdownBrowser() {
  if (!browserPromise) {
    return;
  }

  const browser = await browserPromise;
  browserPromise = null;
  await browser.close();
}

module.exports = {
  withPage,
  shutdownBrowser
};
