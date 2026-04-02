const OpenAI = require("openai");
const env = require("../config/env");
const logger = require("../utils/logger");

let client = null;
let disabledUntilTs = 0;

function getClient() {
  if (!env.openAiApiKey) {
    return null;
  }

  if (disabledUntilTs && Date.now() < disabledUntilTs) {
    return null;
  }

  if (!client) {
    client = new OpenAI({ apiKey: env.openAiApiKey });
  }

  return client;
}

function uniqueQueries(queries) {
  return [...new Set((queries || []).map((item) => String(item || "").trim()).filter(Boolean))].slice(
    0,
    env.maxQueriesGenerated
  );
}

function inferIntentHeuristically(userInput) {
  const text = userInput.toLowerCase();
  if (/(barato|economico|oferta|chollo|rebaja|low cost)/.test(text)) {
    return "cheap";
  }
  if (/(premium|mejor|top|gama alta|pro)/.test(text)) {
    return "premium";
  }
  if (/(reacondicionado|usado|segunda mano)/.test(text)) {
    return "refurbished";
  }
  return "general";
}

function extractProductHeuristically(userInput) {
  return userInput
    .replace(/\b(barato|economico|oferta|chollo|premium|mejor|reacondicionado|usado|segunda mano)\b/gi, "")
    .replace(/\s+/g, " ")
    .trim();
}

function buildFallbackQueries(userInput) {
  const product = extractProductHeuristically(userInput) || userInput.trim();
  const intent = inferIntentHeuristically(userInput);
  const suffixByIntent = {
    cheap: "barato",
    premium: "premium",
    refurbished: "reacondicionado",
    general: "oferta"
  };
  const suffix = suffixByIntent[intent] || "oferta";

  return {
    product,
    intent,
    queries: uniqueQueries([
      `${product} ${suffix} amazon`,
      `${product} ${suffix} ebay`,
      `${product} ${suffix} aliexpress`,
      `${product} oferta espana`,
      `${product} mejor precio`
    ])
  };
}

async function interpretQuery(userInput) {
  const trimmed = String(userInput || "").trim();
  if (!trimmed) {
    throw new Error("La consulta no puede estar vacia.");
  }

  const fallback = buildFallbackQueries(trimmed);
  const openai = getClient();
  if (!openai) {
    return fallback;
  }

  try {
    // OpenAI solo se usa aqui: interpretacion y expansion minima de la consulta.
    const response = await openai.responses.create({
      model: env.openAiModel,
      input: [
        {
          role: "system",
          content: [
            {
              type: "input_text",
              text: "Extrae producto, detecta intencion de compra y genera de 3 a 5 consultas cortas optimizadas para ecommerce. Devuelve solo JSON."
            }
          ]
        },
        {
          role: "user",
          content: [
            {
              type: "input_text",
              text: `Consulta del usuario: ${trimmed}

Devuelve exactamente este JSON:
{
  "product": "string",
  "intent": "cheap|premium|refurbished|general",
  "queries": ["q1", "q2", "q3"]
}`
            }
          ]
        }
      ]
    });

    const rawText = response.output_text || "";
    const cleaned = rawText.replace(/^```json\s*/i, "").replace(/^```\s*/i, "").replace(/\s*```$/i, "").trim();
    const parsed = JSON.parse(cleaned);
    const product = String(parsed.product || fallback.product).trim() || fallback.product;
    const intent = String(parsed.intent || fallback.intent).trim() || fallback.intent;
    const queries = uniqueQueries(parsed.queries);

    if (!queries.length) {
      return fallback;
    }

    return {
      product,
      intent,
      queries
    };
  } catch (error) {
    if (error?.status === 429) {
      disabledUntilTs = Date.now() + 30 * 60 * 1000;
    }
    logger.warn("OpenAI no pudo interpretar la consulta. Se usa fallback heuristico.", error.message);
    return fallback;
  }
}

module.exports = {
  interpretQuery
};
