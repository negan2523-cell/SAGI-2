"""Red vial dirigida y distancias por calles para el piloto en Render Free."""

from __future__ import annotations

import math
from dataclasses import dataclass, field
from heapq import heappop, heappush
from threading import Lock

import networkx as nx
import osmnx as ox
import pandas as pd
import requests

from src.config import MAX_PUNTOS_PILOTO, OVERPASS_URLS, RADIO_MAX_METROS


# OSMnx usa configuración global. Serializar también evita construir dos redes a la vez.
_CARGA_LOCK = Lock()
_ESPEJOS = tuple(url.removesuffix("/interpreter") for url in OVERPASS_URLS)
_BUFFER_METROS = 500
_MAX_SNAP_METROS = 500


def _comprobar_respuesta(respuesta: requests.Response, **kwargs) -> requests.Response:
    # OSMnx reintenta 429/504 sin límite y espera 55 s entre intentos.
    # Elevar aquí permite pasar al siguiente espejo dentro del timeout del piloto.
    respuesta.raise_for_status()
    return respuesta


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    """Calcula la distancia en metros entre dos coordenadas geográficas."""
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(min(1.0, max(0.0, a))))


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


def _validar_puntos(df: pd.DataFrame) -> pd.DataFrame:
    columnas = ["latitud", "longitud", "demanda"]
    if not set(columnas).issubset(df.columns):
        raise ValueError("El archivo debe contener latitud, longitud y demanda.")
    if not 2 <= len(df) <= MAX_PUNTOS_PILOTO:
        raise ValueError(
            f"El piloto admite de 2 a {MAX_PUNTOS_PILOTO} filas "
            "(la primera es el patio; las demás, puntos de recolección)."
        )
    try:
        puntos = df[columnas].astype(float)
    except (ValueError, TypeError) as exc:
        raise ValueError("Latitud, longitud y demanda deben ser numéricas.") from exc
    if not all(math.isfinite(v) for fila in puntos.itertuples(index=False, name=None) for v in fila):
        raise ValueError("Latitud, longitud y demanda no pueden contener valores vacíos o infinitos.")
    if not puntos["latitud"].between(-85, 85).all() or not puntos["longitud"].between(-180, 180).all():
        raise ValueError("Coordenadas fuera de rango: latitud entre -85 y 85, longitud entre -180 y 180.")
    if (puntos["demanda"] < 0).any():
        raise ValueError("La demanda no puede ser negativa.")
    lat_patio, lon_patio = puntos.iloc[0][["latitud", "longitud"]]
    for lat, lon in puntos[["latitud", "longitud"]].itertuples(index=False, name=None):
        if _haversine_m(lat_patio, lon_patio, lat, lon) > RADIO_MAX_METROS:
            raise ValueError("Todos los puntos deben estar dentro de un radio de 15 km del patio (primera fila).")
    # La caja no puede cruzar el antimeridiano en este piloto urbano.
    if puntos["longitud"].max() - puntos["longitud"].min() > 180:
        raise ValueError("El piloto no admite zonas que crucen el meridiano de 180°.")
    return puntos


def _descargar_grafo(bbox: tuple[float, float, float, float]) -> nx.MultiDiGraph:
    configuracion = {
        "log_console": False,
        "use_cache": True,
        "cache_folder": "/tmp/osmnx_cache",
        "overpass_rate_limit": False,
        "http_user_agent": "SAGI-Suba-App/1.0",
        "requests_timeout": 25,
        "useful_tags_node": [],
        "useful_tags_way": ["highway", "oneway", "junction"],
        "requests_kwargs": {"hooks": {"response": _comprobar_respuesta}},
        "overpass_url": _ESPEJOS[0],
    }
    anterior = {clave: getattr(ox.settings, clave) for clave in configuracion}
    try:
        for clave, valor in configuracion.items():
            setattr(ox.settings, clave, valor)
        ultimo_error = None
        for espejo in _ESPEJOS:
            ox.settings.overpass_url = espejo
            try:
                # OSMnx 2: (oeste, sur, este, norte), en una sola tupla.
                return ox.graph_from_bbox(bbox, network_type="drive", simplify=True)
            except (requests.RequestException, ox._errors.InsufficientResponseError) as exc:
                ultimo_error = exc.with_traceback(None)
        raise RuntimeError(
            "No se pudo obtener una red vial de Overpass para estos puntos. "
            f"Intenta de nuevo más tarde. Detalle: {ultimo_error}"
        ) from ultimo_error
    finally:
        for clave, valor in anterior.items():
            setattr(ox.settings, clave, valor)


def _grafo_ligero(multi: nx.MultiDiGraph) -> nx.DiGraph:
    """Conserva solo coordenadas, longitudes y geometría de la arista más corta."""
    grafo = nx.DiGraph()
    for nodo, datos in multi.nodes(data=True):
        grafo.add_node(nodo, lat=float(datos["y"]), lon=float(datos["x"]))
    for a, b, datos in multi.edges(data=True):
        longitud = float(datos["length"])
        if not grafo.has_edge(a, b) or longitud < grafo[a][b]["length"]:
            grafo.add_edge(a, b, length=longitud, geometry=datos.get("geometry"))
    return grafo


def _dijkstra_hasta(
    grafo: nx.DiGraph, origen: int, destinos: set[int],
) -> tuple[dict[int, float], dict[int, int]]:
    """Un padre por nodo; termina al alcanzar todos los puntos de la matriz."""
    pendientes = destinos - {origen}
    distancias = {origen: 0.0}
    padres = {}
    cola = [(0.0, origen)]
    visitados = set()
    while cola and pendientes:
        distancia, nodo = heappop(cola)
        if nodo in visitados:
            continue
        visitados.add(nodo)
        pendientes.discard(nodo)
        if not pendientes:
            break
        for vecino, datos in grafo[nodo].items():
            propuesta = distancia + datos["length"]
            if vecino not in visitados and propuesta < distancias.get(vecino, math.inf):
                distancias[vecino] = propuesta
                padres[vecino] = nodo
                heappush(cola, (propuesta, vecino))
    return distancias, padres


def cargar_instancia(df: pd.DataFrame) -> InstanciaVRP:
    """Primera fila = patio; hasta 50 clientes a 15 km del patio en línea recta."""
    puntos = _validar_puntos(df)
    with _CARGA_LOCK:
        return _construir_instancia(puntos)


def _construir_instancia(puntos: pd.DataFrame) -> InstanciaVRP:
    lats = puntos["latitud"].tolist()
    lons = puntos["longitud"].tolist()
    # Descargar solo la extensión de los puntos, con margen fijo para calles de acceso.
    buffer_lat = math.degrees(_BUFFER_METROS / 6371000)
    buffer_lon = buffer_lat / math.cos(math.radians(lats[0]))
    bbox = (
        min(lons) - buffer_lon, min(lats) - buffer_lat,
        max(lons) + buffer_lon, max(lats) + buffer_lat,
    )
    if bbox[0] < -180 or bbox[2] > 180:
        raise ValueError("El piloto no admite zonas que crucen el meridiano de 180°.")
    multi = _descargar_grafo(bbox)
    grafo = _grafo_ligero(multi)
    del multi  # Evitar retener el MultiDiGraph y sus etiquetas durante el ruteo.
    componente = max(nx.strongly_connected_components(grafo), key=len, default=set())
    if len(componente) < len(puntos):
        raise ValueError("No hay suficientes nodos viales conectados para todos los puntos del archivo.")
    grafo.remove_nodes_from(set(grafo) - componente)

    coords = [(n, d["lat"], d["lon"]) for n, d in grafo.nodes(data=True)]
    usados = set()
    ids = []
    cos_lat = math.cos(math.radians(lats[0]))
    for fila, (lat, lon) in enumerate(zip(lats, lons), start=1):
        candidato = min(
            (n for n in coords if n[0] not in usados),
            key=lambda n: (lat - n[1]) ** 2 + ((lon - n[2]) * cos_lat) ** 2,
        )
        if _haversine_m(lat, lon, candidato[1], candidato[2]) > _MAX_SNAP_METROS:
            raise ValueError(
                f"La fila {fila} está a más de {_MAX_SNAP_METROS} m de una vía "
                "disponible con rutas de ida y vuelta. Revisa las coordenadas."
            )
        ids.append(candidato[0])
        usados.add(candidato[0])

    matriz = [[0.0] * len(ids) for _ in ids]
    caminos = {}
    for i, origen in enumerate(ids):
        distancias, padres = _dijkstra_hasta(grafo, origen, usados)
        for j, destino in enumerate(ids):
            if i == j:
                continue
            if destino not in distancias:
                raise ValueError("No existe una ruta de ida y vuelta entre todos los puntos.")
            matriz[i][j] = distancias[destino]
            camino = [destino]
            while camino[-1] != origen:
                camino.append(padres[camino[-1]])
            camino.reverse()
            caminos[(origen, destino)] = camino

    # El mapa y el solver solo necesitan la unión de los caminos calculados.
    # No guardar toda la ciudad en cada sesión/caché de Dash.
    compacto = nx.DiGraph()
    for camino in caminos.values():
        for nodo in camino:
            if nodo not in compacto:
                compacto.add_node(nodo, **grafo.nodes[nodo])
        for a, b in zip(camino, camino[1:]):
            if not compacto.has_edge(a, b):
                compacto.add_edge(a, b, **grafo[a][b])
    return InstanciaVRP(
        grafo=compacto, depot=ids[0], clientes=ids[1:],
        demanda={n: (0.0 if i == 0 else float(puntos.iloc[i]["demanda"])) for i, n in enumerate(ids)},
        matriz_m=matriz, caminos=caminos,
    )
