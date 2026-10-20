# Simulador what-if de recolección de residuos — Suba, Bogotá

App Dash que construye un grafo vial dirigido de Suba desde OpenStreetMap y reoptimiza
rutas de recolección (CVRP con límite de jornada: Clarke-Wright + 2-opt + reinserción)
al mover los parámetros del escenario.

```bash
python -m venv .venv && source .venv/bin/activate
pip install -r requirements.txt
python app.py   # http://127.0.0.1:8051
```

## Piloto en Render Free

- Sube un CSV o Excel con `latitud`, `longitud` y `demanda` en kg y confirma el mapeo de
  columnas. **La primera fila es el patio**; se admiten hasta **50 puntos de recolección**.
- Todos los puntos deben estar dentro de **15 km en línea recta desde el patio**. El
  recorrido por calles puede ser más largo. No hace falta descargar un círculo completo:
  se consulta la caja que contiene los puntos, con 500 m de margen para accesos.
- Las coordenadas deben ser finitas y las demandas no negativas. Si no hay una vía
  conectada de ida y vuelta a menos de 500 m, se pide corregir el punto; no se inventan
  distancias para puntos inaccesibles.
- La red dinámica requiere internet y consulta dos servidores Overpass con un
  timeout de 25 s por petición y sin reintentos ilimitados ante saturación. La caché de
  descargas está en `/tmp/osmnx_cache`; Render la pierde al reiniciar la instancia.
- Las descargas se procesan de una en una. La matriz usa una búsqueda por origen y solo
  conserva los caminos entre los puntos del archivo y sus geometrías. La caché en memoria
  retiene un único archivo reciente; las sesiones se guardan en `/tmp/dash_cache`.
- Usa **un solo proceso** para este piloto. El arranque disponible es `python app.py`
  (escucha en `0.0.0.0` y usa el `PORT` de Render). La instalación es
  `pip install -r requirements.txt`. El motor usa OSMnx 2.x.
- El límite de 15 km acota los datos de entrada, pero el consumo durante la construcción
  depende de la densidad vial. Empieza con pocos puntos y aumenta gradualmente; no es una
  garantía de que cualquier zona de 15 km quepa en la memoria de Render Free.

Ejemplo de CSV (demanda del patio ignorada):

```csv
latitud,longitud,demanda
4.7413,-74.0837,0
4.7423,-74.0837,120
4.7423,-74.0827,180
```

`data/suba_osm.json` es un extracto histórico pequeño de Suba (licencia ODbL), utilizado
para pruebas sin internet. No cubre un radio de 15 km y no sustituye automáticamente las
consultas dinámicas. `data/suba_instancia.json` pertenece a la versión anterior del motor.

Pruebas locales, sin llamar a servicios externos:

```bash
python -m unittest discover -s tests -v
```

## Asistente de IA

Debajo de los conductores hay un chat que responde preguntas sobre el escenario en pantalla
(rutas, horarios, cargas, costos, puntos sin recoger). Usa cualquier API compatible con OpenAI
(OpenRouter, Groq, DeepSeek, ...). Crea un archivo `.env` en la raíz o configura estas
variables en Render (el mapeo inicial de columnas también usa esta API):

- `LLM_API_KEY`: tu clave (no se sube a git: `.env` está en `.gitignore`).
- `LLM_MODEL`: el modelo, con el nombre que usa tu proveedor.
- `LLM_BASE_URL`: la URL de la API (por defecto `https://openrouter.ai/api/v1`).
- `LLM_INCLUIR_CODIGO`: `1` (por defecto) adjunta el código de `src/routing/vrp.py`,
  `src/graph/osm_network.py` y `src/config.py` para que pueda explicar cómo funciona el motor;
  `0` lo omite y cada pregunta gasta unas 4 veces menos tokens.

También se pueden definir como variables de entorno; estas tienen prioridad sobre `.env`.
Cada pregunta envía al proveedor una ficha técnica del sistema, el resumen del escenario actual
(parámetros, rutas y puntos) y, si está activado, el código del motor.
