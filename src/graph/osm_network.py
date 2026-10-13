"""Red vial dirigida de Suba desde OpenStreetMap y matriz de distancias."""

from __future__ import annotations

import json
import math
import random
import xml.etree.ElementTree as ET
from dataclasses import dataclass, field
from typing import Any

import networkx as nx
import requests

from src.config import (
    DEMANDA_BASE_KG,
    HIGHWAY_TYPES,
    INSTANCE_CACHE,
    N_PUNTOS_RECOLECCION,
    OSM_CACHE,
    OVERPASS_HEADERS,
    OVERPASS_URLS,
    PATIO_LATLON,
    RADIO_METROS,
    SUBA_CENTER,
)


def _haversine_m(lat1: float, lon1: float, lat2: float, lon2: float) -> float:
    r = 6371000.0
    p1, p2 = math.radians(lat1), math.radians(lat2)
    dphi = math.radians(lat2 - lat1)
    dl = math.radians(lon2 - lon1)
    a = math.sin(dphi / 2) ** 2 + math.cos(p1) * math.cos(p2) * math.sin(dl / 2) ** 2
    return 2 * r * math.asin(math.sqrt(a))


def _bbox() -> tuple[float, float, float, float]:
    lat, lon = SUBA_CENTER
    dlat = RADIO_METROS / 111_320.0
    dlon = RADIO_METROS / (111_320.0 * math.cos(math.radians(lat)))
    min_lon, max_lon = lon - dlon, lon + dlon
    min_lat, max_lat = lat - dlat, lat + dlat
    return min_lon, min_lat, max_lon, max_lat


def _es_unidireccional(tags: dict[str, Any]) -> tuple[bool, bool]:
    oneway = str(tags.get("oneway", "no")).lower()
    highway = str(tags.get("highway", ""))
    junction = str(tags.get("junction", ""))
    if oneway in {"yes", "true", "1"}:
        return True, False
    if oneway in {"-1", "reverse"}:
        return True, True
    if highway in {"motorway", "motorway_link"} or junction == "roundabout":
        return True, False
    return False, False


def _query_overpass() -> str:
    lat, lon = SUBA_CENTER
    hw = "|".join(HIGHWAY_TYPES)
    return f"""
    [out:json][timeout:90];
    way["highway"~"^({hw})$"](around:{RADIO_METROS},{lat},{lon});
    (._;>;);
    out body;
    """


def _osm_xml_a_payload(xml_text: str) -> dict[str, Any]:
    root = ET.fromstring(xml_text)
    elements: list[dict[str, Any]] = []
    for node in root.findall("node"):
        nid = int(node.attrib["id"])
        elements.append(
            {
                "type": "node",
                "id": nid,
                "lat": float(node.attrib["lat"]),
                "lon": float(node.attrib["lon"]),
            }
        )
    allowed = set(HIGHWAY_TYPES)
    for way in root.findall("way"):
        tags = {t.attrib["k"]: t.attrib["v"] for t in way.findall("tag")}
        if tags.get("highway") not in allowed:
            continue
        refs = [int(nd.attrib["ref"]) for nd in way.findall("nd")]
        elements.append(
            {
                "type": "way",
                "id": int(way.attrib["id"]),
                "nodes": refs,
                "tags": tags,
            }
        )
    return {"elements": elements}


def _descargar_bbox_oficial() -> dict[str, Any]:
    min_lon, min_lat, max_lon, max_lat = _bbox()
    url = (
        "https://api.openstreetmap.org/api/0.6/map"
        f"?bbox={min_lon:.6f},{min_lat:.6f},{max_lon:.6f},{max_lat:.6f}"
    )
    resp = requests.get(url, headers=OVERPASS_HEADERS, timeout=180)
    resp.raise_for_status()
    return _osm_xml_a_payload(resp.text)


def _descargar_overpass() -> dict[str, Any]:
    query = _query_overpass()
    last_error: Exception | None = None
    for url in OVERPASS_URLS:
        try:
            resp = requests.post(
                url,
                data={"data": query},
                headers=OVERPASS_HEADERS,
                timeout=180,
            )
            resp.raise_for_status()
            payload = resp.json()
            if payload.get("elements"):
                return payload
            last_error = RuntimeError(f"{url} devolvió elements vacío")
        except Exception as exc:  # noqa: BLE001
            last_error = exc
    raise RuntimeError(f"Overpass no disponible: {last_error}") from last_error


def _descargar_osm() -> dict[str, Any]:
    OSM_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if OSM_CACHE.exists():
        payload = json.loads(OSM_CACHE.read_text(encoding="utf-8"))
        if payload.get("elements"):
            return payload

    last_error: Exception | None = None
    payload: dict[str, Any] | None = None
    try:
        payload = _descargar_bbox_oficial()
    except Exception as exc:  # noqa: BLE001
        last_error = exc
        try:
            payload = _descargar_overpass()
        except Exception as exc2:  # noqa: BLE001
            last_error = exc2

    if not payload or not payload.get("elements"):
        raise RuntimeError(f"No se pudo descargar OSM de Suba: {last_error}") from last_error

    OSM_CACHE.write_text(json.dumps(payload), encoding="utf-8")
    return payload


def construir_grafo(osm: dict[str, Any]) -> nx.DiGraph:
    nodos = {
        el["id"]: (el["lat"], el["lon"])
        for el in osm.get("elements", [])
        if el.get("type") == "node" and "lat" in el
    }
    G = nx.DiGraph()
    for nid, (lat, lon) in nodos.items():
        G.add_node(nid, lat=lat, lon=lon)

    for el in osm.get("elements", []):
        if el.get("type") != "way":
            continue
        refs = el.get("nodes") or []
        tags = el.get("tags") or {}
        if len(refs) < 2:
            continue
        unidireccional, invertir = _es_unidireccional(tags)
        secuencia = list(reversed(refs)) if invertir else refs
        pares = list(zip(secuencia, secuencia[1:]))
        if not unidireccional:
            pares += [(b, a) for a, b in pares]

        nombre = tags.get("name", "")
        highway = tags.get("highway", "")
        for a, b in pares:
            if a not in G.nodes or b not in G.nodes:
                continue
            la, loa = G.nodes[a]["lat"], G.nodes[a]["lon"]
            lb, lob = G.nodes[b]["lat"], G.nodes[b]["lon"]
            dist = _haversine_m(la, loa, lb, lob)
            if dist <= 0:
                continue
            if G.has_edge(a, b):
                G[a][b]["length"] = min(G[a][b]["length"], dist)
            else:
                G.add_edge(a, b, length=dist, name=nombre, highway=highway)

    G.remove_nodes_from([n for n, d in G.degree() if d == 0])
    if G.number_of_nodes() == 0:
        raise RuntimeError("El grafo OSM de Suba quedó vacío.")
    wcc = max(nx.weakly_connected_components(G), key=len)
    G = G.subgraph(wcc).copy()
    scc = max(nx.strongly_connected_components(G), key=len)
    return G.subgraph(scc).copy()


def _nodo_mas_cercano(G: nx.DiGraph, lat: float, lon: float) -> int:
    return min(
        G.nodes,
        key=lambda n: _haversine_m(lat, lon, G.nodes[n]["lat"], G.nodes[n]["lon"]),
    )


def _muestrear_puntos(G: nx.DiGraph, n: int, depot: int, rng: random.Random) -> list[int]:
    candidatos = [nid for nid in G.nodes if G.degree(nid) >= 3 and nid != depot]
    if len(candidatos) < n:
        candidatos = [nid for nid in G.nodes if nid != depot]
    rng.shuffle(candidatos)

    min_sep_m = 180.0
    elegidos: list[int] = []
    for nid in candidatos:
        lat, lon = G.nodes[nid]["lat"], G.nodes[nid]["lon"]
        if all(
            _haversine_m(lat, lon, G.nodes[o]["lat"], G.nodes[o]["lon"]) >= min_sep_m
            for o in elegidos
        ):
            elegidos.append(nid)
        if len(elegidos) >= n:
            break
    if len(elegidos) < n:
        for nid in candidatos:
            if nid not in elegidos and nid != depot:
                elegidos.append(nid)
            if len(elegidos) >= n:
                break
    return elegidos


def _camino_y_distancia(G: nx.DiGraph, origen: int, destino: int) -> tuple[list[int], float]:
    path = nx.shortest_path(G, origen, destino, weight="length")
    dist = nx.shortest_path_length(G, origen, destino, weight="length")
    return path, float(dist)


@dataclass
class InstanciaSuba:
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


def construir_instancia(G: nx.DiGraph) -> InstanciaSuba:
    rng = random.Random(42)
    depot = _nodo_mas_cercano(G, *PATIO_LATLON)
    clientes = _muestrear_puntos(G, N_PUNTOS_RECOLECCION, depot, rng)
    demanda = {
        cid: float(DEMANDA_BASE_KG + rng.randint(-40, 90))
        for cid in clientes
    }
    ids = [depot, *clientes]
    n = len(ids)
    matriz = [[0.0] * n for _ in range(n)]
    caminos: dict[tuple[int, int], list[int]] = {}
    for i, a in enumerate(ids):
        for j, b in enumerate(ids):
            if i == j:
                continue
            path, dist = _camino_y_distancia(G, a, b)
            matriz[i][j] = dist
            caminos[(a, b)] = path
    return InstanciaSuba(
        grafo=G,
        depot=depot,
        clientes=clientes,
        demanda=demanda,
        matriz_m=matriz,
        caminos=caminos,
    )


_FORMATO_CACHE = 1


def _instancia_a_dict(inst: InstanciaSuba) -> dict[str, Any]:
    G = inst.grafo
    return {
        "formato": _FORMATO_CACHE,
        "nodos": [[n, d["lat"], d["lon"]] for n, d in G.nodes(data=True)],
        "aristas": [
            [a, b, d["length"], d.get("name", ""), d.get("highway", "")]
            for a, b, d in G.edges(data=True)
        ],
        "depot": inst.depot,
        "clientes": inst.clientes,
        "demanda": [[c, q] for c, q in inst.demanda.items()],
        "matriz_m": inst.matriz_m,
        "caminos": [[a, b, path] for (a, b), path in inst.caminos.items()],
    }


def _instancia_desde_dict(data: dict[str, Any]) -> InstanciaSuba:
    if data.get("formato") != _FORMATO_CACHE:
        raise ValueError("Formato de caché de instancia no soportado.")
    G = nx.DiGraph()
    for n, lat, lon in data["nodos"]:
        G.add_node(n, lat=lat, lon=lon)
    for a, b, length, name, highway in data["aristas"]:
        G.add_edge(a, b, length=length, name=name, highway=highway)
    return InstanciaSuba(
        grafo=G,
        depot=data["depot"],
        clientes=list(data["clientes"]),
        demanda={c: q for c, q in data["demanda"]},
        matriz_m=data["matriz_m"],
        caminos={(a, b): path for a, b, path in data["caminos"]},
    )


def cargar_instancia(forzar: bool = False) -> InstanciaSuba:
    INSTANCE_CACHE.parent.mkdir(parents=True, exist_ok=True)
    if INSTANCE_CACHE.exists() and not forzar:
        try:
            data = json.loads(INSTANCE_CACHE.read_text(encoding="utf-8"))
            return _instancia_desde_dict(data)
        except (ValueError, KeyError, TypeError):
            pass
    osm = _descargar_osm()
    G = construir_grafo(osm)
    inst = construir_instancia(G)
    INSTANCE_CACHE.write_text(json.dumps(_instancia_a_dict(inst)), encoding="utf-8")
    return inst
