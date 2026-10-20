# Simulador what-if de recolección de residuos — Suba, Bogotá

App Dash que obtiene distancias y recorridos por calles mediante OSRM y reoptimiza
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
  recorrido por calles puede ser más largo. Las coordenadas deben ser finitas y las demandas
  no negativas. Los puntos se ajustan a una vía a un máximo de 500 m.
- El motor consulta una matriz dirigida a **OSRM** y calcula la distribución entre vehículos
  localmente. Después solicita la geometría de cada ruta elegida. Render guarda los puntos y
  esos recorridos; ya no descarga ni construye toda la red de calles mediante Overpass.
- OSRM utiliza datos de OpenStreetMap y su perfil de automóvil. Las distancias corresponden
  a las rutas elegidas por ese perfil, que prioriza tiempo de viaje; no necesariamente son
  los caminos de menor distancia ni incluyen restricciones particulares de un camión.
  La jornada del simulador sigue usando la velocidad que selecciones en el control.
- La aplicación limita las consultas a una por segundo por proceso. Guarda hasta cuatro
  matrices y 32 geometrías de rutas en memoria para reutilizarlas. Un fallo del servicio se
  muestra en pantalla; no se reemplazan calles por líneas rectas o distancias inventadas.
- Por defecto se usa `https://router.project-osrm.org`, el servidor público de demostración
  de OSRM. Su [política](https://github.com/Project-OSRM/osrm-backend/wiki/Demo-server)
  admite uso razonable y no comercial, con un máximo de una petición por segundo, sin garantía
  de disponibilidad. Es adecuado para probar el piloto; para operación comercial o mayor
  tráfico configura `OSRM_URL` con un servidor OSRM dedicado compatible.
- Usa **un solo proceso** para este piloto y para respetar el límite del servicio público.
  El arranque es `python app.py` (escucha en `0.0.0.0` y usa el `PORT` de Render).
  La instalación es `pip install -r requirements.txt`.
- Un CSV con columnas `latitud`, `longitud` y `demanda` se reconoce sin llamar a la IA. Si
  las cabeceras son distintas y la IA no está disponible, puedes elegirlas manualmente.
- Las sesiones están en `/tmp/dash_cache` y las cachés se pierden al reiniciar Render.
  El módulo `osm_network.py` conserva el motor local anterior para pruebas y usos explícitos;
  OSMnx se importa únicamente al utilizar ese motor.

Ejemplo de CSV (demanda del patio ignorada):

```csv
latitud,longitud,demanda
4.7413,-74.0837,0
4.7423,-74.0837,120
4.7423,-74.0827,180
```

`data/suba_osm.json` es un extracto histórico pequeño de Suba (licencia ODbL), utilizado
para pruebas sin internet del motor local. No cubre un radio de 15 km; la aplicación
consulta OSRM para obtener las distancias y los recorridos del archivo cargado. `data/suba_instancia.json` pertenece a la versión anterior del motor.

Pruebas locales, sin llamar a servicios externos:

```bash
python -m unittest discover -s tests -v
```

## Asistente de IA

Debajo de los conductores hay un chat que responde preguntas sobre el escenario en pantalla
(rutas, horarios, cargas, costos, puntos sin recoger). Usa cualquier API compatible con OpenAI
(OpenRouter, Groq, DeepSeek, ...). Crea un archivo `.env` en la raíz o configura estas
variables en Render (la IA puede sugerir el mapeo de cabeceras no estándar):

- `LLM_API_KEY`: tu clave (no se sube a git: `.env` está en `.gitignore`).
- `LLM_MODEL`: el modelo, con el nombre que usa tu proveedor.
- `LLM_BASE_URL`: la URL de la API (por defecto `https://openrouter.ai/api/v1`).
- `LLM_INCLUIR_CODIGO`: `1` (por defecto) adjunta el código de `src/routing/vrp.py`,
  `src/graph/osm_network.py` y `src/config.py` para que pueda explicar cómo funciona el motor;
  `0` lo omite y cada pregunta gasta unas 4 veces menos tokens.

También se pueden definir como variables de entorno; estas tienen prioridad sobre `.env`.
Cada pregunta envía al proveedor una ficha técnica del sistema, el resumen del escenario actual
(parámetros, rutas y puntos) y, si está activado, el código del motor.
