"""Red vial dirigida desde OpenStreetMap y matriz de distancias (Pipeline Dinámico)."""

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
    
    # --- ZONA DE VALIDACIÓN PRE-VUELO (Fail-Fast) ---
    min_lat = df['latitud'].min()
    max_lat = df['latitud'].max()
    min_lon = df['longitud'].min()
    max_lon = df['longitud'].max()

    diagonal_m = _haversine_m(min_lat, min_lon, max_lat, max_lon)
    if diagonal_m > 25000:
        raise ValueError(
            "Los puntos están demasiado dispersos (distancia superior a 25 km). "
            "Este sistema está optimizado para ruteo urbano local. "
            "Por favor, revisa posibles errores en las coordenadas de tu archivo."
        )

    # --- BOUNDING BOX Y DESCARGA SEGURA CON TIMEOUT ---
    delta_lat = max_lat - min_lat
    delta_lon = max_lon - min_lon

    buffer_lat = max(delta_lat * 0.20, 0.002)
    buffer_lon = max(delta_lon * 0.20, 0.002)

    bbox_north = max_lat + buffer_lat
    bbox_south = min_lat - buffer_lat
    bbox_east = max_lon + buffer_lon
    bbox_west = min_lon - buffer_lon

# --- CONFIGURACIÓN ESTRICTA DE RED OSMNX (SOPORTE MULTI-VERSIÓN) ---
    espejo = "https://overpass.kumi.systems/api" # Servidor suizo de alta tolerancia a nubes
    
    # 1. En OSMnx 1.x, es OBLIGATORIO usar ox.config() para que el motor interno cambie de servidor.
    if hasattr(ox, 'config'):
        ox.config(
            log_console=False,
            overpass_endpoint=espejo,
            http_headers={'User-Agent': 'SAGI-Suba-App/1.0 (negan2523@gmail.com)'},
            timeout=300
        )
    else:
        # Para versiones modernas (OSMnx 2.0+)
        ox.settings.log_console = False
        ox.settings.overpass_url = espejo
        ox.settings.http_headers = {'User-Agent': 'SAGI-Suba-App/1.0 (negan2523@gmail.com)'}
        ox.settings.timeout = 300

    try:
        # 2. Detectar la versión instalada en Render para enviar las coordenadas en el orden que exige cada versión
        version_mayor = int(ox.__version__.split('.')[0]) if hasattr(ox, '__version__') else 1
        
        if version_mayor >= 2:
            # Formato estricto para OSMnx 2.0+ (Tupla Bbox: Left, Bottom, Right, Top)
            G_multi = ox.graph_from_bbox(bbox=(bbox_west, bbox_south, bbox_east, bbox_north), network_type='drive')
            G = ox.convert.to_digraph(G_multi, weight='length')
        else:
            # Formato estricto para OSMnx 1.x (Parámetros nombrados explícitamente para evitar inversión de mapa)
            G_multi = ox.graph_from_bbox(north=bbox_north, south=bbox_south, east=bbox_east, west=bbox_west, network_type='drive')
            from osmnx import utils_graph
            G = utils_graph.get_digraph(G_multi, weight='length')
            
    except Exception as e:
        raise RuntimeError(f"Fallo técnico al procesar el mapa. Detalle: {str(e)}")

    for n, data in G.nodes(data=True):
        data['lat'] = data.get('y', 0.0)
        data['lon'] = data.get('x', 0.0)

    # --- IDENTIFICACIÓN (SNAPPING) DE NODOS ---
    nodos_proyectados = ox.distance.nearest_nodes(
        G,
        X=df['longitud'].tolist(),
        Y=df['latitud'].tolist()
    )

    # --- ASIGNACIÓN HEURÍSTICA DE PUNTOS ---
    depot = nodos_proyectados[0]
    clientes = nodos_proyectados[1:]

    demandas_array = df['demanda'].astype(float).values
    demanda: dict[int, float] = {depot: 0.0}
    
    for i, nodo_cliente in enumerate(clientes):
        demanda[nodo_cliente] = demandas_array[i + 1]

    # --- MATRIZ DE DISTANCIAS OPTIMIZADA (DIJKSTRA O(N)) ---
    ids_activos = [depot] + clientes
    n_puntos = len(ids_activos)
    
    matriz_m = [[0.0] * n_puntos for _ in range(n_puntos)]
    caminos: dict[tuple[int, int], list[int]] = {}

    for i, origen in enumerate(ids_activos):
        # El algoritmo se ejecuta una sola vez por nodo origen buscando todas las rutas posibles
        try:
            dist_dict, path_dict = nx.single_source_dijkstra(G, origen, weight='length')
        except nx.NetworkXNoPath:
            dist_dict, path_dict = {}, {}
            
        for j, destino in enumerate(ids_activos):
            if i == j:
                continue
            
            if destino in dist_dict:
                matriz_m[i][j] = float(dist_dict[destino])
                caminos[(origen, destino)] = path_dict[destino]
            else:
                matriz_m[i][j] = 999999.0
                caminos[(origen, destino)] = []

    return InstanciaVRP(
        grafo=G,
        depot=depot,
        clientes=clientes,
        demanda=demanda,
        matriz_m=matriz_m,
        caminos=caminos
    )