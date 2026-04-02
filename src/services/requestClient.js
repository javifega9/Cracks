const { request } = require("playwright");

let requestContextPromise = null;

async function getRequestContext() {
  if (!requestContextPromise) {
    requestContextPromise = request.newContext({
      extraHTTPHeaders: {
        "accept-language": "es-ES,es;q=0.9,en;q=0.8",
        "user-agent":
          "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36"
      }
    });
  }

  return requestContextPromise;
}

async function fetchHtml(url, timeout = 15000) {
  const client = await getRequestContext();
  const response = await client.get(url, {
    failOnStatusCode: false,
    timeout
  });

  if (!response.ok()) {
    throw new Error(`HTTP ${response.status()} al pedir ${url}`);
  }

  return response.text();
}

module.exports = {
  fetchHtml
};
