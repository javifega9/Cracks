const app = require("./app");
const env = require("./config/env");
const logger = require("./utils/logger");
const { shutdownBrowser } = require("./services/browser");
const { searchCache } = require("./services/searchPipeline");

const server = app.listen(env.port, () => {
  logger.info(`Cracks Node escuchando en el puerto ${env.port}`);
});

const cleanupInterval = setInterval(() => {
  searchCache.cleanup();
}, 15 * 60 * 1000);

async function shutdown(signal) {
  logger.info(`Apagando servidor por ${signal}...`);
  clearInterval(cleanupInterval);
  server.close(async () => {
    await shutdownBrowser().catch(() => {});
    process.exit(0);
  });
}

process.on("SIGINT", () => shutdown("SIGINT"));
process.on("SIGTERM", () => shutdown("SIGTERM"));
