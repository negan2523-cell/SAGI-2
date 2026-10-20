"""Matriz y geometrías OSRM para un piloto que no descarga la red vial en Render."""
from __future__ import annotations

import math
import os
import time
from dataclasses import dataclass
from functools import lru_cache
from threading import Lock

import networkx as nx
import pandas as pd
import requests

from .osm_network import InstanciaVRP, _validar_puntos

OSRM_URL = os.environ.get("OSRM_URL", "https://router.project-osrm.org").rstrip("/")
_HEADERS = {"User-Agent": "SAGI-Pilot/1.0 (https://github.com/negan2523-cell/SAGI-2)"}
_LOCK = Lock()
_ultima_consulta = 0.0
_MAX_SNAP_METROS = 500


def _consultar(servicio: str, coordenadas: tuple[tuple[float, float], ...], **params) -> dict:
    """El servidor demo permite como máximo una petición por segundo por proceso."""
    global _ultima_consulta
    puntos = ";".join(f"{lon:.7f},{lat:.7f}" for lon, lat in coordenadas)
    params["radiuses"] = ";".join([str(_MAX_SNAP_METROS)] * len(coordenadas))
    with _LOCK:
        espera = 1.0 - (time.monotonic() - _ultima_consulta)
        if espera > 0:
            time.sleep(espera)
        _ultima_consulta = time.monotonic()
        try:
            respuesta = requests.get(
                f"{OSRM_URL}/{servicio}/v1/driving/{puntos}",
                params=params, headers=_HEADERS, timeout=(5, 20),
            )
            respuesta.raise_for_status()
            datos = respuesta.json()
        except (requests.RequestException, ValueError) as exc:
            raise RuntimeError(
                "El servicio de rutas OSRM no respondió correctamente. "
                "Intenta de nuevo en unos momentos."
            ) from exc
    if not isinstance(datos, dict) or datos.get("code") != "Ok":
        raise RuntimeError("OSRM no pudo calcular rutas para estos puntos; revisa que estén cerca de vías accesibles.")
    return datos


def _numero(valor) -> bool:
    return isinstance(valor, (int, float)) and not isinstance(valor, bool) and math.isfinite(valor)


@lru_cache(maxsize=4)
def _tabla(coordenadas: tuple[tuple[float, float], ...]) -> dict:
    datos = _consultar("table", coordenadas, annotations="distance", generate_hints="false")
    n = len(coordenadas)
    matriz = datos.get("distances")
    fuentes = datos.get("sources")
    if not isinstance(matriz, list) or len(matriz) != n or any(
        not isinstance(fila, list) or len(fila) != n for fila in matriz
    ):
        raise RuntimeError("OSRM devolvió una matriz de distancias incompleta.")
    if any(valor is None for fila in matriz for valor in fila):
        raise ValueError("Hay puntos sin conexión vial de ida y vuelta. Revisa las coordenadas del archivo.")
    if any(not _numero(valor) or valor < 0 for fila in matriz for valor in fila):
        raise RuntimeError("OSRM devolvió distancias inválidas.")
    if any(matriz[i][i] != 0 for i in range(n)):
        raise RuntimeError("OSRM devolvió una matriz con diagonal inválida.")
    if not isinstance(fuentes, list) or len(fuentes) != n:
        raise RuntimeError("OSRM no devolvió las ubicaciones de los puntos sobre las vías.")
    for punto in fuentes:
        if not isinstance(punto, dict):
            raise RuntimeError("OSRM devolvió una ubicación inválida.")
        ubicacion = punto.get("location")
        if not isinstance(ubicacion, list) or len(ubicacion) != 2 or not all(_numero(v) for v in ubicacion):
            raise RuntimeError("OSRM devolvió una ubicación inválida.")
        if not (-180 <= ubicacion[0] <= 180 and -90 <= ubicacion[1] <= 90):
            raise RuntimeError("OSRM devolvió coordenadas fuera de rango.")
        distancia = punto.get("distance")
        if not _numero(distancia) or not 0 <= distancia <= _MAX_SNAP_METROS:
            raise ValueError("Hay un punto a más de 500 m de una vía accesible. Revisa sus coordenadas.")
    return datos


@lru_cache(maxsize=32)
def _geometrias(coordenadas: tuple[tuple[float, float], ...]) -> tuple:
    datos = _consultar(
        "route", coordenadas, geometries="geojson", steps="true", overview="false",
        alternatives="false", continue_straight="false", generate_hints="false",
    )
    rutas = datos.get("routes")
    if not isinstance(rutas, list) or not rutas or not isinstance(rutas[0], dict):
        raise RuntimeError("OSRM no devolvió la geometría de la ruta.")
    piernas = rutas[0].get("legs")
    if not isinstance(piernas, list) or len(piernas) != len(coordenadas) - 1:
        raise RuntimeError("OSRM devolvió una ruta incompleta.")
    resultado = []
    for pierna in piernas:
        if not isinstance(pierna, dict) or not _numero(pierna.get("distance")) or pierna["distance"] < 0:
            raise RuntimeError("OSRM devolvió un tramo inválido.")
        puntos = []
        pasos = pierna.get("steps")
        if not isinstance(pasos, list):
            raise RuntimeError("OSRM no devolvió los tramos de calle.")
        for paso in pasos:
            geometria = paso.get("geometry", {}) if isinstance(paso, dict) else {}
            coords = geometria.get("coordinates") if isinstance(geometria, dict) else None
            if not isinstance(geometria, dict) or geometria.get("type") != "LineString" or not isinstance(coords, list):
                raise RuntimeError("OSRM devolvió una geometría inválida.")
            for coord in coords:
                if not isinstance(coord, list) or len(coord) != 2 or not all(_numero(v) for v in coord):
                    raise RuntimeError("OSRM devolvió coordenadas inválidas para una ruta.")
                punto = tuple(coord)
                if not puntos or punto != puntos[-1]:
                    puntos.append(punto)
        # Dos paradas en la misma ubicación pueden tener distancia cero.
        if len(puntos) == 1 and pierna["distance"] == 0:
            puntos.append(puntos[0])
        if len(puntos) < 2:
            raise RuntimeError("OSRM devolvió una ruta sin geometría de calles.")
        resultado.append((float(pierna["distance"]), tuple(puntos)))
    return tuple(resultado)


@dataclass
class InstanciaOSRM(InstanciaVRP):
    coordenadas_osrm: tuple[tuple[float, float], ...]

    def preparar_ruta(self, secuencia: list[int]) -> None:
        pares = list(zip(secuencia, secuencia[1:]))
        if not pares or all(par in self.caminos for par in pares):
            return
        tramos = _geometrias(tuple(self.coordenadas_osrm[self.indice[n]] for n in secuencia))
        # Validar todo antes de guardar: un fallo no debe dejar una ruta a medias.
        for (a, b), (distancia, _) in zip(pares, tramos):
            esperada = self.matriz_m[self.indice[a]][self.indice[b]]
            if abs(distancia - esperada) > max(5.0, esperada * 0.01):
                _tabla.cache_clear()
                _geometrias.cache_clear()
                raise RuntimeError(
                    "La ruta recibida no coincide con las distancias del escenario. "
                    "Vuelve a cargar los puntos para recalcular."
                )
        for (a, b), (distancia, geometria) in zip(pares, tramos):
            self.grafo.add_edge(a, b, length=distancia, geometry=geometria)
            self.caminos[(a, b)] = [a, b]

    def polyline(self, secuencia_nodos_logicos: list[int]) -> tuple[list[float], list[float]]:
        self.preparar_ruta(secuencia_nodos_logicos)
        puntos = []
        for a, b in zip(secuencia_nodos_logicos, secuencia_nodos_logicos[1:]):
            tramo = self.grafo[a][b]["geometry"]
            puntos.extend(tramo[1:] if puntos and puntos[-1] == tramo[0] else tramo)
        return [lat for lon, lat in puntos], [lon for lon, lat in puntos]


def cargar_instancia(df: pd.DataFrame) -> InstanciaOSRM:
    puntos = _validar_puntos(df)
    coordenadas = tuple(zip(puntos["longitud"].tolist(), puntos["latitud"].tolist()))
    tabla = _tabla(coordenadas)
    grafo = nx.DiGraph()
    for i, fuente in enumerate(tabla["sources"]):
        lon, lat = fuente["location"]
        grafo.add_node(i, lat=lat, lon=lon)
    return InstanciaOSRM(
        grafo=grafo, depot=0, clientes=list(range(1, len(puntos))),
        demanda={i: (0.0 if i == 0 else float(d)) for i, d in enumerate(puntos["demanda"])},
        matriz_m=[list(fila) for fila in tabla["distances"]], caminos={},
        coordenadas_osrm=coordenadas,
    )
