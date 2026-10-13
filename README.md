# Simulador what-if de recolección de residuos — Suba, Bogotá

App Dash que construye un grafo vial dirigido de Suba desde OpenStreetMap y reoptimiza
rutas de recolección (CVRP con límite de jornada: Clarke-Wright + 2-opt + reinserción)
al mover los parámetros del escenario.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py   # http://127.0.0.1:8051
```

- `data/suba_osm.json` (extracto OSM, licencia ODbL) va versionado para que la app funcione sin
  internet. Si se borra, se vuelve a descargar (API oficial de OSM y, de respaldo, Overpass).
- `data/suba_instancia.json` es una caché derivada (grafo, puntos, matriz de distancias) y se
  regenera sola en el primer arranque.
- El patio y los 28 puntos de acumulación son sintéticos (semilla fija), no datos operativos reales.

## Asistente de IA

Debajo de los conductores hay un chat que responde preguntas sobre el escenario en pantalla
(rutas, horarios, cargas, costos, puntos sin recoger). Usa cualquier API compatible con OpenAI
(OpenRouter, Groq, DeepSeek, ...). Copia `.env.example` como `.env` y completa:

- `LLM_API_KEY`: tu clave (no se sube a git: `.env` está en `.gitignore`).
- `LLM_MODEL`: el modelo, con el nombre que usa tu proveedor.
- `LLM_BASE_URL`: la URL de la API (por defecto `https://openrouter.ai/api/v1`).
- `LLM_INCLUIR_CODIGO`: `1` (por defecto) adjunta el código de `src/routing/vrp.py`,
  `src/graph/osm_network.py` y `src/config.py` para que pueda explicar cómo funciona el motor;
  `0` lo omite y cada pregunta gasta unas 4 veces menos tokens.

También se pueden definir como variables de entorno; estas tienen prioridad sobre `.env`.
Cada pregunta envía al proveedor una ficha técnica del sistema, el resumen del escenario actual
(parámetros, rutas y puntos) y, si está activado, el código del motor.
