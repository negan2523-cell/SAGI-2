"""Asistente de IA sobre el escenario actual (API compatible con OpenAI).

Configuración por variables de entorno o un archivo `.env` en la raíz del proyecto:
LLM_API_KEY, LLM_MODEL, LLM_BASE_URL (por defecto OpenRouter) y LLM_INCLUIR_CODIGO
(por defecto 1: adjunta el código del motor para que el modelo pueda analizarlo).
"""

from __future__ import annotations

import os
from functools import lru_cache

import requests

from src.config import HORA_INICIO_JORNADA, ROOT
from src.graph.osm_network import InstanciaVRP
from src.routing.vrp import Solucion, itinerario

BASE_URL_POR_DEFECTO = "https://openrouter.ai/api/v1"
MAX_MENSAJES_HISTORIAL = 12
ARCHIVOS_CODIGO = ("src/routing/vrp.py", "src/graph/osm_network.py", "src/config.py")

INSTRUCCIONES = (
    "Eres un asistente de operaciones de recolección de residuos para la localidad de Suba "
    "(Bogotá). Respondes en español, de forma breve y concreta, usando solo la ficha técnica, "
    "el código fuente (si se adjunta) y los datos del escenario que se te entregan. Si "
    "preguntan cómo funciona el sistema, básate en la ficha técnica y en el código, y cita "
    "funciones o archivos cuando ayude. Si algo no está en esa información, dilo en lugar de "
    "inventarlo. "
    "Cuando des cifras, cítalas del escenario. Puedes sugerir qué slider mover para mejorar "
    "un indicador, pero aclara que es una estimación hasta recalcular. La simulación usa las "
    "coordenadas y demandas del archivo del usuario; "
    "no asumas que son datos operativos reales."
)

FICHA_TECNICA = """FICHA TÉCNICA DEL SIMULADOR
- App web en Python (Dash + Plotly). Cada vez que se mueve un slider se vuelve a resolver el
  problema completo y se redibujan mapa, indicadores, conductores y sus mapas.
- Red vial: calles reales de OpenStreetMap descargadas de Overpass para la extensión de
  los puntos cargados, con margen de 500 m. El piloto admite un patio en la primera fila
  y hasta 50 puntos, a un máximo de 15 km en línea recta desde el patio. Usa OSMnx 2 y un
  grafo dirigido ponderado con networkx: nodos = intersecciones, aristas = tramos de calle,
  peso = longitud en metros. Respeta sentidos viales; conserva la mayor componente
  fuertemente conexa y rechaza puntos a más de 500 m de una vía disponible.
- Cada arista conserva su geometría OSM y el mapa dibuja sus coordenadas, incluidos los
  vértices intermedios de los LineStrings, en lugar de unir solo las paradas.
- Instancia: patio, coordenadas y demandas proceden del archivo del usuario. Se calculan
  caminos más cortos dirigidos con Dijkstra, una búsqueda por origen, hasta alcanzar los
  demás puntos. Las distancias son por calle y pueden diferir de ida y vuelta. La sesión
  solo conserva la unión de esos caminos; las descargas y sesiones usan cachés temporales.
- Ruteo: CVRP con límite de jornada (sin ventanas horarias por punto). Heurística de ahorros
  de Clarke-Wright en dos variantes (ahorro dirigido y clásico), mejora 2-opt por ruta y, si
  hay más rutas que vehículos, reinserción de los puntos sobrantes en rutas existentes. Se
  elige la variante que deja menos kg sin recoger y, a igualdad, la de menos km. Una ruta es
  factible si no supera la capacidad del camión ni la jornada (viaje + servicio).
- Tiempos: distancia por calle / velocidad media + minutos de servicio por parada. Salida del
  patio a las 06:00.
- Costo total = km x costo por km + vehículos usados x costo fijo + kg no recogidos x
  penalización.
- No hay tráfico en tiempo real, ni ventanas horarias, ni varios viajes por camión."""


def _cargar_env() -> None:
    archivo = ROOT / ".env"
    if not archivo.exists():
        return
    for linea in archivo.read_text(encoding="utf-8").splitlines():
        linea = linea.strip()
        if not linea or linea.startswith("#") or "=" not in linea:
            continue
        clave, valor = linea.split("=", 1)
        os.environ.setdefault(clave.strip(), valor.strip().strip("\"'"))


_cargar_env()


def configuracion() -> dict:
    return {
        "incluir_codigo": os.environ.get("LLM_INCLUIR_CODIGO", "1").strip().lower()
        not in {"0", "no", "false"},
        "api_key": os.environ.get("LLM_API_KEY", ""),
        "modelo": os.environ.get("LLM_MODEL", ""),
        "base_url": os.environ.get("LLM_BASE_URL", BASE_URL_POR_DEFECTO).rstrip("/"),
    }


def falta_configuracion() -> str | None:
    cfg = configuracion()
    faltan = [n for n, k in (("LLM_API_KEY", "api_key"), ("LLM_MODEL", "modelo")) if not cfg[k]]
    if faltan:
        return "Falta configurar " + " y ".join(faltan) + " (variable de entorno o archivo .env)."
    return None


@lru_cache(maxsize=1)
def codigo_fuente() -> str:
    partes = ["CÓDIGO FUENTE DEL MOTOR (Python)"]
    for rel in ARCHIVOS_CODIGO:
        texto = (ROOT / rel).read_text(encoding="utf-8").replace("\r\n", "\n")
        partes.append(f"### {rel}\n```python\n{texto}\n```")
    return "\n\n".join(partes)


def _hora(minutos_desde_salida: float) -> str:
    total = round(HORA_INICIO_JORNADA * 60 + minutos_desde_salida)
    return f"{total // 60 % 24:02d}:{total % 60:02d}"


def contexto_escenario(inst: InstanciaVRP, sol: Solucion, p: dict) -> str:
    numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
    lineas = [
        f"Grafo vial cargado: {inst.grafo.number_of_nodes()} nodos, "
        f"{inst.grafo.number_of_edges()} aristas dirigidas.",
        "",
        "PARÁMETROS DEL ESCENARIO",
        f"- Capacidad por camión: {p['capacidad']:.0f} kg",
        f"- Vehículos disponibles: {p['vehiculos']:.0f}",
        f"- Multiplicador de demanda: {p['demanda']:.2f}",
        f"- Jornada: {p['jornada']:.1f} h (salida del patio {_hora(0)})",
        f"- Velocidad media: {p['velocidad']:.0f} km/h",
        f"- Servicio por punto: {p['servicio']:.0f} min",
        f"- Costo por km: {p['costo']:.0f} COP; costo fijo por vehículo: "
        f"{p['costo_fijo']:.0f} COP/día; penalización: {p['penalizacion']:.0f} COP/kg no recogido",
        "",
        "RESULTADOS",
        f"- Costo total: {sol.costo_cop:.0f} COP (km {sol.costo_variable_cop:.0f}, "
        f"flota {sol.costo_fijo_cop:.0f}, penalización {sol.penalizacion_cop:.0f})",
        f"- Distancia total: {sol.distancia_total_m / 1000:.1f} km",
        f"- Horas-vehículo: {sol.tiempo_total_min / 60:.2f} h; "
        f"ruta más larga: {sol.tiempo_max_ruta_min / 60:.2f} h",
        f"- Vehículos usados: {sol.vehiculos_usados} de {p['vehiculos']:.0f}",
        f"- Demanda total: {sol.demanda_total_kg:.0f} kg; cubierta: "
        f"{sol.demanda_cubierta_kg:.0f} kg; no recogida: {sol.demanda_no_cubierta_kg:.0f} kg",
        f"- Escenario factible: {'sí' if sol.factible else 'no'}",
        "",
        "CONDUCTORES (cada uno hace una ruta que sale y vuelve al patio)",
    ]
    for i, ruta in enumerate(sol.rutas):
        paradas, regreso_m, fin_min = itinerario(
            inst,
            ruta,
            velocidad_kmh=p["velocidad"],
            servicio_min=p["servicio"],
            demanda_mult=p["demanda"],
        )
        costo = ruta.distancia_m / 1000 * p["costo"] + p["costo_fijo"]
        lineas.append(
            f"Conductor {i + 1}: {len(ruta.clientes)} paradas, {ruta.carga_kg:.0f} kg "
            f"({ruta.carga_kg / p['capacidad']:.0%} de la capacidad), "
            f"{ruta.distancia_m / 1000:.1f} km, {ruta.tiempo_min / 60:.2f} h "
            f"({ruta.tiempo_min / 60 / p['jornada']:.0%} de la jornada), "
            f"{_hora(0)}–{_hora(fin_min)}, costo {costo:.0f} COP"
        )
        for k, par in enumerate(paradas):
            lineas.append(
                f"  {k + 1}. Punto {numero[par.cliente]} llegada {_hora(par.llegada_min)}, "
                f"tramo {par.tramo_m / 1000:.2f} km, carga acumulada {par.carga_acum_kg:.0f} kg"
            )
        lineas.append(f"  Regreso al patio {_hora(fin_min)}, tramo {regreso_m / 1000:.2f} km")
    lineas += ["", "PUNTOS DE ACUMULACIÓN (número, demanda del escenario, lat, lon)"]
    asignado = {c: i + 1 for i, r in enumerate(sol.rutas) for c in r.clientes}
    for c in inst.clientes:
        lat, lon = inst.coords(c)
        quien = f"conductor {asignado[c]}" if c in asignado else "SIN CONDUCTOR"
        lineas.append(
            f"- Punto {numero[c]}: {inst.demanda[c] * p['demanda']:.0f} kg, "
            f"{lat:.5f}, {lon:.5f}, {quien}"
        )
    dlat, dlon = inst.coords(inst.depot)
    lineas.append(f"- Patio: {dlat:.5f}, {dlon:.5f}")
    return "\n".join(lineas)


def preguntar(historial: list[dict], contexto: str) -> str:
    """Envía la conversación al modelo y devuelve el texto de la respuesta."""
    cfg = configuracion()
    mensajes = [
        {"role": "system", "content": INSTRUCCIONES + "\n\n" + FICHA_TECNICA},
        *([{"role": "system", "content": codigo_fuente()}] if cfg["incluir_codigo"] else []),
        {"role": "system", "content": "ESCENARIO ACTUAL\n" + contexto},
        *[m for m in historial if m["role"] in ("user", "assistant")][-MAX_MENSAJES_HISTORIAL:],
    ]
    try:
        r = requests.post(
            f"{cfg['base_url']}/chat/completions",
            headers={
                "Authorization": f"Bearer {cfg['api_key']}",
                "Content-Type": "application/json",
                "X-Title": "Simulador residuos Suba",
            },
            json={"model": cfg["modelo"], "messages": mensajes, "temperature": 0.2},
            timeout=90,
        )
    except requests.RequestException as e:
        raise RuntimeError(f"No se pudo conectar con {cfg['base_url']}: {type(e).__name__}") from e
    if r.status_code != 200:
        try:
            detalle = r.json().get("error", {})
            detalle = detalle.get("message", detalle) if isinstance(detalle, dict) else detalle
        except ValueError:
            detalle = r.text[:300]
        raise RuntimeError(f"El proveedor respondió {r.status_code}: {detalle}")
    try:
        return r.json()["choices"][0]["message"]["content"].strip()
    except (ValueError, KeyError, IndexError, TypeError, AttributeError) as e:
        raise RuntimeError("Respuesta del proveedor con formato inesperado.") from e