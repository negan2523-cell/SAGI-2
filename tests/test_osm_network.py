"""Regresiones del piloto: radio, OSMnx 2 y rutas dirigidas sin llamadas externas."""
import base64
import json
import math
import unittest
from pathlib import Path
from unittest.mock import patch

import networkx as nx
import osmnx as ox
import pandas as pd
import requests
from shapely.geometry import LineString

from src.graph import osm_network as red
from src.routing.vrp import resolver_vrp


def puntos():
    return pd.DataFrame({
        "latitud": [4.7413, 4.7423, 4.7423],
        "longitud": [-74.0837, -74.0837, -74.0827],
        "demanda": [0, 120, 180],
    })


def red_prueba():
    grafo = nx.MultiDiGraph()
    for nodo, lat, lon in [(1, 4.7413, -74.0837), (2, 4.7423, -74.0837),
                            (3, 4.7423, -74.0827), (4, 4.7433, -74.0827),
                            (5, 4.7443, -74.0827), (6, 4.7444, -74.0827)]:
        grafo.add_node(nodo, x=lon, y=lat)
    # Triángulo dirigido: ir de 1 a 3 cuesta 300 m, regresar cuesta 400 m.
    grafo.add_edge(1, 2, length=900)
    grafo.add_edge(1, 2, length=100, geometry=LineString([
        (-74.0837, 4.7413), (-74.0838, 4.7418), (-74.0837, 4.7423)]))
    grafo.add_edge(2, 3, length=200)
    grafo.add_edge(3, 1, length=400)
    # Calle conectada de ida y vuelta, que no se necesita en ningún camino óptimo.
    grafo.add_edge(3, 4, length=500)
    grafo.add_edge(4, 3, length=500)
    # Componente aislada, que debe quedar fuera.
    grafo.add_edge(5, 6, length=10)
    grafo.add_edge(6, 5, length=10)
    return grafo


class ValidacionTests(unittest.TestCase):
    def test_radio_desde_patio_admite_diametro_casi_30_km(self):
        df = puntos()
        delta = math.degrees(14_990 / 6371000)
        df["latitud"] = [4.7413, 4.7413 - delta, 4.7413 + delta]
        df["longitud"] = -74.0837
        red._validar_puntos(df)
        with patch.object(red, "_descargar_grafo", return_value=red_prueba()) as descarga:
            # Validación radial pasa y se intenta descargar; el snapping falla por la fixture local.
            with self.assertRaisesRegex(ValueError, "500 m"):
                red.cargar_instancia(df)
        oeste, sur, este, norte = descarga.call_args.args[0]
        self.assertLess(sur, df.latitud.min())
        self.assertGreater(norte, df.latitud.max())
        self.assertLess(oeste, este)

    def test_rechaza_radio_mayor_a_15_km_antes_de_descargar(self):
        df = puntos()
        df.loc[1, "latitud"] += math.degrees(15_100 / 6371000)
        with patch.object(red, "_descargar_grafo") as descarga:
            with self.assertRaisesRegex(ValueError, "radio de 15 km"):
                red.cargar_instancia(df)
            descarga.assert_not_called()

    def test_rechaza_datos_invalidos_antes_de_descargar(self):
        casos = []
        for columna, valor in [("latitud", math.nan), ("longitud", math.inf),
                               ("demanda", -1), ("latitud", 91), ("longitud", -181),
                               ("demanda", "texto")]:
            df = puntos().astype(object)
            df.loc[1, columna] = valor
            casos.append(df)
        casos += [puntos().drop(columns="demanda"), puntos().iloc[:1],
                  pd.concat([puntos()] * 18, ignore_index=True)]
        with patch.object(red, "_descargar_grafo") as descarga:
            for df in casos:
                with self.subTest(datos=df.shape), self.assertRaises(ValueError):
                    red.cargar_instancia(df)
            descarga.assert_not_called()


class DescargaTests(unittest.TestCase):
    def test_api_2_y_restaura_configuracion_con_failover(self):
        bbox = (-74.09, 4.73, -74.08, 4.75)
        original = (ox.settings.overpass_url, ox.settings.requests_kwargs,
                    ox.settings.http_user_agent, ox.settings.requests_timeout)
        espejos = []

        def descargar(bbox, *, network_type="all", simplify=True, **kwargs):
            espejos.append(ox.settings.overpass_url)
            self.assertEqual(network_type, "drive")
            self.assertTrue(simplify)
            self.assertIs(ox.settings.requests_kwargs["hooks"]["response"], red._comprobar_respuesta)
            if len(espejos) == 1:
                raise requests.Timeout("prueba de timeout")
            return red_prueba()

        # autospec comprueba la firma de la versión realmente instalada.
        with patch.object(ox, "graph_from_bbox", autospec=True, side_effect=descargar) as descargar_mock:
            self.assertIsInstance(red._descargar_grafo(bbox), nx.MultiDiGraph)
            self.assertEqual(descargar_mock.call_args.args, (bbox,))
        self.assertEqual(espejos, list(red._ESPEJOS))
        self.assertEqual(original, (ox.settings.overpass_url, ox.settings.requests_kwargs,
                                    ox.settings.http_user_agent, ox.settings.requests_timeout))

    def test_error_de_programacion_no_se_disfraza_de_servidor_caido(self):
        anterior = ox.settings.requests_kwargs
        with patch.object(ox, "graph_from_bbox", side_effect=TypeError("firma incorrecta")) as descargar:
            with self.assertRaisesRegex(TypeError, "firma incorrecta"):
                red._descargar_grafo((-74.09, 4.73, -74.08, 4.75))
            self.assertEqual(descargar.call_count, 1)
        self.assertIs(ox.settings.requests_kwargs, anterior)

    def test_servidores_fallan_con_error_claro(self):
        with patch.object(ox, "graph_from_bbox", side_effect=requests.Timeout("timeout")) as descargar:
            with self.assertRaisesRegex(RuntimeError, "Overpass"):
                red._descargar_grafo((-74.09, 4.73, -74.08, 4.75))
            self.assertEqual(descargar.call_count, 2)

    def test_saturacion_no_entra_al_reintento_infinito_de_osmnx(self):
        for estado in (429, 504):
            respuesta = requests.Response()
            respuesta.status_code = estado
            with self.subTest(estado=estado), self.assertRaises(requests.HTTPError):
                red._comprobar_respuesta(respuesta)


class RuteoTests(unittest.TestCase):
    def test_matriz_dirigida_geometria_y_grafo_compacto(self):
        multi = red_prueba()
        with patch.object(red, "_descargar_grafo", return_value=multi):
            instancia = red.cargar_instancia(puntos())
        self.assertEqual(instancia.ids, [1, 2, 3])
        self.assertEqual(instancia.demanda, {1: 0, 2: 120, 3: 180})
        self.assertEqual(instancia.matriz_m, [[0, 100, 300], [600, 0, 200], [400, 500, 0]])
        self.assertEqual(set(instancia.grafo), {1, 2, 3})
        self.assertEqual(list(instancia.grafo[1][2]["geometry"].coords),
                         list(multi[1][2][1]["geometry"].coords))
        for (a, b), camino in instancia.caminos.items():
            self.assertEqual((camino[0], camino[-1]), (a, b))
            distancia = sum(instancia.grafo[u][v]["length"] for u, v in zip(camino, camino[1:]))
            self.assertEqual(distancia, nx.shortest_path_length(multi, a, b, weight="length"))
        self.assertGreater(len(instancia.polyline([1, 2, 3, 1])[0]), 0)

    def test_nunca_repite_nodos_si_no_hay_suficientes(self):
        multi = red_prueba().subgraph([1, 2]).copy()
        multi.add_edge(2, 1, length=100)
        with patch.object(red, "_descargar_grafo", return_value=multi):
            with self.assertRaisesRegex(ValueError, "suficientes nodos"):
                red.cargar_instancia(puntos())

    def test_dijkstra_con_aristas_cero_no_crea_ciclos_de_padres(self):
        grafo = nx.DiGraph()
        grafo.add_weighted_edges_from([(1, 2, 1), (2, 3, 0), (3, 2, 0),
                                      (3, 4, 1), (1, 4, 20)], weight="length")
        distancias, padres = red._dijkstra_hasta(grafo, 1, {1, 2, 3, 4})
        self.assertEqual(distancias, nx.single_source_dijkstra_path_length(grafo, 1, weight="length"))
        for destino in (2, 3, 4):
            visitados = set()
            while destino != 1:
                self.assertNotIn(destino, visitados)
                visitados.add(destino)
                destino = padres[destino]

    def test_integracion_osmnx_real_con_extracto_versionado_y_vrp(self):
        fixture = json.loads((Path(__file__).resolve().parents[1] / "data/suba_osm.json").read_text())
        with patch.object(ox._overpass, "_download_overpass_network", return_value=iter([fixture])), \
             patch.object(requests, "post", side_effect=AssertionError("No se permite red")):
            instancia = red.cargar_instancia(puntos())
        self.assertEqual(len(instancia.clientes), 2)
        self.assertTrue(all(math.isfinite(v) for fila in instancia.matriz_m for v in fila))
        sol = resolver_vrp(instancia, capacidad_kg=1200, max_vehiculos=2, demanda_mult=1,
                           jornada_h=8, velocidad_kmh=22, servicio_min=8, costo_km=4500,
                           costo_fijo_vehiculo=150000, penalizacion_kg=500)
        self.assertTrue(sol.factible)
        self.assertEqual(sol.demanda_cubierta_kg, 300)
        self.assertFalse(sol.no_cubiertos)

    def test_upload_dashboard_y_cache_acotada(self):
        import app
        client = app.app.server.test_client()
        self.assertEqual(client.get("/").status_code, 200)
        self.assertEqual(client.get("/_dash-layout").status_code, 200)
        csv = puntos().to_csv(index=False).encode()
        contenido = "data:text/csv;base64," + base64.b64encode(csv).decode()
        with patch.dict(app.CACHE_INSTANCIAS, {"otra": object()}, clear=True), \
             patch.object(red, "_descargar_grafo", side_effect=lambda bbox: red_prueba()):
            resultado = app.confirmar_y_generar_instancia(
                1, contenido, "piloto.csv", None, "latitud", "longitud", "demanda")
            instancia = resultado[0].value
            self.assertEqual(len(app.CACHE_INSTANCIAS), 1)
            self.assertEqual(resultado[3], "")
            sol = app.resolver(instancia, 1200, 2, 1, 8, 22, 8, 4500, 150000, 500)
            for ruta in sol.rutas:
                nodos = app._nodos_ruta(instancia, ruta)
                self.assertTrue(all(instancia.grafo.has_edge(a, b) for a, b in zip(nodos, nodos[1:])))
                lats, lons = app._geometria_nodos(instancia, nodos)
                self.assertGreater(len(lats), 0)
                self.assertEqual(len(lats), len(lons))
            lats, lons = app._geometria_nodos(instancia, [1, 2])
            self.assertEqual(len(lats), 3)
            self.assertEqual(lons[1], -74.0838)


if __name__ == "__main__":
    unittest.main()
