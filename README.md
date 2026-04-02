# Cracks

Aplicacion web para buscar ofertas con una arquitectura hibrida:

- Node.js para la API
- OpenAI solo para interpretar la consulta
- Playwright para scraping en Amazon, eBay y AliExpress

Web publica:

[https://cracks-lpm0.onrender.com/](https://cracks-lpm0.onrender.com/)

## Que hace

- Interpreta la consulta del usuario con OpenAI
- Ejecuta scraping paralelo con Playwright
- Normaliza y rankea resultados sin IA
- Cachea busquedas para reducir coste y latencia
- Mantiene la portada actual sin romper el frontend

## Tecnologias

- Node.js
- Express
- OpenAI API
- Playwright
- Render

## Variables recomendadas

- OPENAI_API_KEY
- AMAZON_AFFILIATE_TAG
- OPENAI_MODEL
- SEARCH_CACHE_TTL_MS
- MAX_RESULTS_PER_SOURCE
- BROWSER_TIMEOUT_MS
- BROWSER_NAVIGATION_TIMEOUT_MS
- PROXY_SERVER

## Estructura

- `src/server.js`: arranque del servidor
- `src/routes/search.js`: endpoint `GET /search`
- `src/services/queryInterpreter.js`: unico uso de OpenAI
- `src/scrapers/*.js`: scrapers Playwright por marketplace
- `src/services/searchPipeline.js`: cache, scraping, normalizacion y ranking
- `public/index.template.html`: frontend actual servido por Node

## Ejecutarlo en local

Necesitas tener instalado Node.js 20 o superior.

1. Entra en la carpeta del proyecto:

```powershell
cd "C:\Users\usuario\Documents\buscador-ofertas-app"
```

2. Instala dependencias:

```powershell
npm install
```

3. Instala Chromium para Playwright:

```powershell
npm run install:browsers
```

4. Pon tu clave de OpenAI en la terminal:

```powershell
$env:OPENAI_API_KEY="TU_CLAVE"
```

5. Arranca la app:

```powershell
npm start
```

6. Abre:

- `http://127.0.0.1:8000`
- `http://127.0.0.1:8000/health`

## Despliegue en Render

Esta version ya esta preparada para Node + Playwright.

Render usara:

- `buildCommand`: `npm install && npx playwright install chromium`
- `startCommand`: `node src/server.js`

Solo tienes que:

1. Subir estos cambios a GitHub
2. En Render, usar el repositorio actualizado
3. Confirmar que el servicio usa runtime `Node`
4. Anadir `OPENAI_API_KEY` en `Environment`
5. Hacer redeploy

## Si Playwright falla en Render

Lo mas comun es que falle por el navegador o por dependencias del sistema.

La configuracion actual instala solo Chromium:

```text
npx playwright install chromium
```

No usamos `--with-deps` en Render porque intenta elevar privilegios del sistema y suele fallar.

Si aun asi falla mas adelante por librerias del sistema que falten, el siguiente paso recomendable seria desplegar con Docker para tener un entorno mas controlado.
