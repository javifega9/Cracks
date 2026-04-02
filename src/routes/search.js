const express = require("express");
const { executeSearch } = require("../services/searchPipeline");

const router = express.Router();

router.get("/search", async (req, res, next) => {
  try {
    const query = String(req.query.q || req.query.query || "").trim();
    if (!query) {
      return res.status(400).json({ detail: "Debes indicar ?q=tu busqueda" });
    }

    const result = await executeSearch(query);
    if (!result.productos.length) {
      return res.status(200).json(result);
    }

    return res.json(result);
  } catch (error) {
    return next(error);
  }
});

module.exports = router;
