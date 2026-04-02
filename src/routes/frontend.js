const fs = require("fs");
const path = require("path");
const express = require("express");
const env = require("../config/env");

const router = express.Router();
const templatePath = path.join(env.publicDir, "index.template.html");

function renderTemplate() {
  const rawHtml = fs.readFileSync(templatePath, "utf-8");
  return rawHtml.replaceAll("__SERVER_STORAGE_ENABLED__", "false");
}

router.get("/", (req, res) => {
  res.type("html").send(renderTemplate());
});

router.get("/logo.svg", (req, res) => {
  res.sendFile(path.join(env.rootDir, "logo.svg"));
});

router.get("/logo.png", (req, res) => {
  res.sendFile(path.join(env.rootDir, "logo.png"));
});

router.get("/saved-searches", (req, res) => {
  res.json({ items: [] });
});

router.post("/save-search", express.json(), (req, res) => {
  res.json({
    detail: "Guardado en servidor desactivado en esta version.",
    visitor_id: req.body?.visitor_id || null
  });
});

router.get("/out", (req, res) => {
  const destination = String(req.query.url || "").trim();
  if (!/^https?:\/\//i.test(destination)) {
    return res.status(400).json({ detail: "El enlace de salida no es valido." });
  }

  return res.redirect(302, destination);
});

router.use(express.static(env.publicDir, { index: false, maxAge: "7d" }));

module.exports = router;
