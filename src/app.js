const express = require("express");
const searchRouter = require("./routes/search");
const frontendRouter = require("./routes/frontend");
const logger = require("./utils/logger");

const app = express();

app.disable("x-powered-by");
app.use(express.json({ limit: "512kb" }));

app.get("/health", (req, res) => {
  res.json({ ok: true, service: "cracks-node" });
});

app.use(searchRouter);
app.use(frontendRouter);

app.use((err, req, res, next) => {
  logger.error("Error no controlado en la API.", err.stack || err.message);
  res.status(500).json({
    detail: "Error interno del servidor. Intenta de nuevo en unos segundos."
  });
});

module.exports = app;
