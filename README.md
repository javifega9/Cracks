# Cracks

Aplicacion web para buscar ofertas de productos usando FastAPI, OpenAI y SerpAPI.

Web publica:

[https://cracks-lpm0.onrender.com/](https://cracks-lpm0.onrender.com/)

## Que hace

- Busca productos en Google Shopping
- Mejora la consulta con OpenAI
- Muestra resultados y top 3 mejores opciones
- Detecta posibles chollos
- Guarda busquedas en Postgres si `DATABASE_URL` esta configurada
- Registra clics en enlaces de Amazon para medir conversiones
- Si no hay base de datos, sigue funcionando con guardado local en navegador

## Tecnologias

- FastAPI
- OpenAI API
- SerpAPI
- Render
- Render Postgres (opcional)

## Variables recomendadas

- OPENAI_API_KEY
- SERPAPI_KEY
- AMAZON_AFFILIATE_TAG
- AMAZON_DOMAIN
- DATABASE_URL
- MAX_SHOPPING_RESULTS
- AMAZON_LOOKUP_MAX_PRODUCTS

## Base de datos opcional

Si anades una base de datos Postgres en Render y configuras `DATABASE_URL`, Cracks:

- guarda las busquedas por navegador
- mantiene las busquedas aunque Render reinicie la web
- registra los clics salientes a Amazon

Si no configuras `DATABASE_URL`, la web sigue funcionando y usa solo almacenamiento local en el navegador.
