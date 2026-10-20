"""Red vial dirigida desde OpenStreetMap y matriz de distancias (Pipeline Dinámico Optimizado)."""

from __future__ import annotations

import math
from dataclasses import dataclass, field

import networkx as nx
import osmnx as ox
import pandas as pd


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula la distancia en metros entre dos coordenadas geográficas."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


@dataclass
class InstanciaVRP:
    grafo: nx.DiGraph
    depot: int
    clientes: list[int]
    demanda: dict[int, float]
    matriz_m: list[list[float]]
    caminos: dict[tuple[int, int], list[int]]
    ids: list[int] = field(init=False)
    indice: dict[int, int] = field(init=False)

    def __post_init__(self) -> None:
        self.ids = [self.depot, *self.clientes]
        self.indice = {nid: i for i, nid in enumerate(self.ids)}

    def coords(self, nid: int) -> tuple[float, float]:
        return self.grafo.nodes[nid]["lat"], self.grafo.nodes[nid]["lon"]

    def polyline(self, secuencia_nodos_logicos: list[int]) -> tuple[list[float], list[float]]:
        lats: list[float] = []
        lons: list[float] = []
        for a, b in zip(secuencia_nodos_logicos, secuencia_nodos_logicos[1:]):
            camino = self.caminos.get((a, b))
            if not camino:
                continue
            for i, node in enumerate(camino):
                if i == 0 and lats:
                    continue
                lat, lon = self.coords(node)
                lats.append(lat)
                lons.append(lon)
        return lats, lons


def cargar_instancia(df: pd.DataFrame) -> InstanciaVRP:
    """
    Ingesta dinámica de puntos geográficos para construir el grafo OSM y la matriz de ruteo.
    Se asume que el df contiene al menos: 'latitud', 'longitud' y 'demanda'.
    La primera fila se considerará el patio (depot).
    """
    if len(df) < 2:
        raise ValueError("El archivo debe contener al menos 2 filas (1 patio y 1 punto de recolección).")

    # --- ZONA DE VALIDACIÓN PRE-VUELO (Fail-Fast) ---
    min_lat = float(df["latitud"].min())
    max_lat = float(df["latitud"].max())
    min_lon = float(df["longitud"].min())
    max_lon = float(df["longitud"].max())

    diagonal_m = _haversine_m(min_lat, min_lon, max_lat, max_lon)
    if diagonal_m > 25000:
        raise ValueError(
            "Los puntos están demasiado dispersos (distancia superior a 25 km). "
            "Este sistema está optimizado para ruteo urbano local. "
            "Por favor, revisa posibles errores en las coordenadas de tu archivo."
        )

    # --- BOUNDING BOX CON BUFFER AJUSTADO ---
    delta_lat = max_lat - min_lat
    delta_lon = max_lon - min_lon

    buffer_lat = max(delta_lat * 0.15, 0.002)
    buffer_lon = max(delta_lon * 0.15, 0.002)

    bbox_north = max_lat + buffer_lat
    bbox_south = min_lat - buffer_lat
    bbox_east = max_lon + buffer_lon  # Corregido: antes era buffer_lat
    bbox_west = min_lon - buffer_lon

    # --- CONFIGURACIÓN DE RED CON FAILOVER RÁPIDO Y CACHÉ EN DISCO ---
    # Eliminado el servidor de mail.ru que ocasiona el timeout
    espejos = [
        "https://overpass-api.de/api",
        "https://overpass.kumi.systems/api",
    ]

    G: nx.DiGraph | None = None
    ultimo_error = ""

    for espejo in espejos:
        try:
            # Configuración específica y obligatoria para OSMnx 1.8+
            ox.settings.log_console = False
            ox.settings.use_cache = True
            ox.settings.cache_folder = "/tmp/osmnx_cache"
            ox.settings.overpass_endpoint = espejo
            ox.settings.overpass_rate_limit = False  # Crítico: evita pausas forzadas que agotan el timeout
            ox.settings.http_headers = {"User-Agent": "SAGI-Suba-App/1.0 (negan2523@gmail.com)"}
            ox.settings.timeout = 25
            ox.settings.requests_timeout = 25

            G_multi = ox.graph_from_bbox(
                north=bbox_north,
                south=bbox_south,
                east=bbox_east,
                west=bbox_west,
                network_type="drive",
                simplify=True,
            )
            from osmnx import utils_graph
            G_candidato = utils_graph.get_digraph(G_multi, weight="length")

            if G_candidato is not None and len(G_candidato) > 0:
                G = G_candidato
                break

        except Exception as e:
            ultimo_error = str(e)
            continue

    if G is None:
        raise RuntimeError(f"Todos los servidores públicos están caídos o saturados. Último error: {ultimo_error}")

    # --- PODA A COMPONENTE FUERTEMENTE CONEXO (GARANTIZA RUTAS DE IDA Y VUELTA) ---
    componentes_fuertes = list(nx.strongly_connected_components(G))
    if componentes_fuertes:
        mayor_componente = max(componentes_fuertes, key=len)
        if len(mayor_componente) >= len(df):
            G = G.subgraph(mayor_componente).copy()

    coords_grafo: list[tuple[int, float, float]] = []
    for n, data in G.nodes(data=True):
        lat_n = float(data.get("y", 0.0))
        lon_n = float(data.get("x", 0.0))
        data["lat"] = lat_n
        data["lon"] = lon_n
        coords_grafo.append((n, lat_n, lon_n))

    # --- IDENTIFICACIÓN (SNAPPING) EN O(V) SIN ORDENAR TODO EL GRAFO ---
    nodos_proyectados: list[int] = []
    nodos_usados: set[int] = set()

    lats = df["latitud"].astype(float).tolist()
    lons = df["longitud"].astype(float).tolist()
    # Precalculamos el factor cos_lat para acelerar el cálculo de distancia pseudo-euclidiana
    cos_lat = math.cos(math.radians(lats[0]))

    for lat_punto, lon_punto in zip(lats, lons):
        # En lugar de sort(), usamos min() con fórmula de distancia plana para no sobrecargar CPU
        candidato = min(
            (n for n in coords_grafo if n[0] not in nodos_usados),
            key=lambda n: (lat_punto - n[1]) ** 2 + ((lon_punto - n[2]) * cos_lat) ** 2,
            default=coords_grafo[0],
        )
        nodo_elegido = candidato[0]
        nodos_proyectados.append(nodo_elegido)
        nodos_usados.add(nodo_elegido)

    # --- ASIGNACIÓN HEURÍSTICA DE PUNTOS ---
    depot = nodos_proyectados[0]
    clientes = nodos_proyectados[1:]

    demandas_array = df["demanda"].astype(float).values
    demanda: dict[int, float] = {depot: 0.0}

    for i, nodo_cliente in enumerate(clientes):
        demanda[nodo_cliente] = float(demandas_array[i + 1])

    # --- MATRIZ DE DISTANCIAS LIGERA (SOLO RECONSTRUYE CAMINOS ACTIVOS) ---
    ids_activos = [depot, *clientes]
    n_puntos = len(ids_activos)

    matriz_m = [[0.0] * n_puntos for _ in range(n_puntos)]
    caminos: dict[tuple[int, int], list[int]] = {}

    for i, origen in enumerate(ids_activos):
        try:
            # Dijkstra ligero: retorna diccionario de distancias y diccionario de predecesores
            pred, dist_dict = nx.dijkstra_predecessor_and_distance(G, origen, weight="length")
        except nx.NetworkXNoPath:
            pred, dist_dict = {}, {}

        for j, destino in enumerate(ids_activos):
            if i == j:
                continue

            if destino in dist_dict:
                matriz_m[i][j] = float(dist_dict[destino])
                
                # Reconstruir camino de forma rápida navegando hacia atrás
                camino = [destino]
                actual = destino
                while actual != origen and pred.get(actual):
                    actual = pred[actual][0]
                    camino.append(actual)
                camino.reverse()
                caminos[(origen, destino)] = camino
            else:
                matriz_m[i][j] = 999999.0
                caminos[(origen, destino)] = []

    return InstanciaVRP(
        grafo=G,
        depot=depot,
        clientes=clientes,
        demanda=demanda,
        matriz_m=matriz_m,
        caminos=caminos,
    )