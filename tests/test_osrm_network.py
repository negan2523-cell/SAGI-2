"""El adaptador remoto debe conservar distancias dirigidas y geometrías reales."""
import base64
import copy
import pickle
import unittest
from types import SimpleNamespace
from unittest.mock import Mock, patch

import pandas as pd
import requests
from dash import no_update

from src.graph import osrm_network as osrm


def puntos():
    return pd.DataFrame({'latitud': [4.7413, 4.7423, 4.7423],
                         'longitud': [-74.0837, -74.0837, -74.0827],
                         'demanda': [0, 120, 180]})


def tabla():
    return {'code': 'Ok', 'distances': [[0, 100, 300], [600, 0, 200], [400, 500, 0]],
            'sources': [{'location': [lon, lat], 'distance': 2.0}
                        for lat, lon in zip(puntos().latitud, puntos().longitud)]}


def ruta():
    return {'code': 'Ok', 'routes': [{'legs': [
        {'distance': 100, 'steps': [{'geometry': {'type': 'LineString', 'coordinates': [
            [-74.0837, 4.7413], [-74.0838, 4.7418], [-74.0837, 4.7423]]}}]},
        {'distance': 200, 'steps': [{'geometry': {'type': 'LineString', 'coordinates': [
            [-74.0837, 4.7423], [-74.0827, 4.7423]]}}]},
        {'distance': 400, 'steps': [{'geometry': {'type': 'LineString', 'coordinates': [
            [-74.0827, 4.7423], [-74.0837, 4.7413]]}}]},
    ]}]}


class OSRMTests(unittest.TestCase):
    def setUp(self):
        osrm._tabla.cache_clear()
        osrm._geometrias.cache_clear()

    def tearDown(self):
        osrm._tabla.cache_clear()
        osrm._geometrias.cache_clear()

    def test_matriz_asimetrica_cache_sin_compartir_demanda_y_serializacion(self):
        with patch.object(osrm, '_consultar', return_value=tabla()) as consultar:
            inst = osrm.cargar_instancia(puntos())
            otros = puntos()
            otros.loc[1, 'demanda'] = 77
            segunda = osrm.cargar_instancia(otros)
        self.assertEqual(consultar.call_count, 1)
        self.assertEqual(inst.matriz_m[0][1], 100)
        self.assertEqual(inst.matriz_m[1][0], 600)
        self.assertEqual(inst.demanda[1], 120)
        self.assertEqual(segunda.demanda[1], 77)
        inst.matriz_m[0][1] = 999
        self.assertEqual(segunda.matriz_m[0][1], 100)
        self.assertEqual(pickle.loads(pickle.dumps(segunda)).coords(1), (4.7423, -74.0837))
        self.assertEqual(len(inst.grafo), 3)
        self.assertEqual(inst.grafo.number_of_edges(), 0)

    def test_paradas_coincidentes_conservan_demanda_independiente(self):
        df = puntos()
        df.loc[2, ['latitud', 'longitud']] = df.loc[1, ['latitud', 'longitud']]
        datos = tabla()
        datos['sources'][2] = copy.deepcopy(datos['sources'][1])
        datos['distances'] = [[0, 100, 100], [600, 0, 0], [600, 0, 0]]
        with patch.object(osrm, '_consultar', return_value=datos):
            inst = osrm.cargar_instancia(df)
        self.assertEqual(inst.coords(1), inst.coords(2))
        self.assertEqual(inst.clientes, [1, 2])
        self.assertEqual(sum(inst.demanda.values()), 300)

    def test_geometrias_solo_para_la_ruta_resuelta_y_reutilizables(self):
        with patch.object(osrm, '_consultar', side_effect=[tabla(), ruta()]) as consultar:
            inst = osrm.cargar_instancia(puntos())
            lats, lons = inst.polyline([0, 1, 2, 0])
            inst.polyline([0, 1, 2, 0])
        self.assertEqual(consultar.call_count, 2)
        self.assertEqual(len(inst.caminos), 3)
        self.assertEqual(lats[0], lats[-1])
        self.assertEqual(lons[1], -74.0838)  # Conserva el vértice intermedio de la calle.
        self.assertEqual(len(lats), 5)
        self.assertEqual(consultar.call_args.kwargs['continue_straight'], 'false')

    def test_rechaza_matriz_incompleta_invalida_o_sin_conexion(self):
        for matriz in ([[0]], [[0, None, 300], [600, 0, 200], [400, 500, 0]],
                       [[0, -1, 300], [600, 0, 200], [400, 500, 0]],
                       [[0, float('nan'), 300], [600, 0, 200], [400, 500, 0]]):
            with self.subTest(matriz=matriz):
                datos = tabla()
                datos['distances'] = matriz
                with patch.object(osrm, '_consultar', return_value=datos):
                    with self.assertRaises((RuntimeError, ValueError)):
                        osrm.cargar_instancia(puntos())

    def test_rechaza_snapping_lejano(self):
        datos = tabla()
        datos['sources'][1]['distance'] = 501
        with patch.object(osrm, '_consultar', return_value=datos):
            with self.assertRaisesRegex(ValueError, '500 m'):
                osrm.cargar_instancia(puntos())

    def test_geometria_invalida_o_distinta_no_guarda_rutas_incompletas(self):
        for variante in ('falta', 'tipo', 'distancia'):
            with self.subTest(variante=variante):
                osrm._geometrias.cache_clear()
                datos = ruta()
                if variante == 'falta':
                    datos['routes'][0]['legs'][1]['steps'] = []
                elif variante == 'tipo':
                    datos['routes'][0]['legs'][1]['steps'][0]['geometry'] = None
                else:
                    datos['routes'][0]['legs'][1]['distance'] = 9999
                with patch.object(osrm, '_consultar', return_value=tabla()):
                    inst = osrm.cargar_instancia(puntos())
                with patch.object(osrm, '_consultar', return_value=datos):
                    with self.assertRaises(RuntimeError):
                        inst.preparar_ruta([0, 1, 2, 0])
                self.assertFalse(inst.caminos)
                self.assertEqual(inst.grafo.number_of_edges(), 0)

    def test_timeout_y_limite_de_una_peticion_por_segundo(self):
        with patch.object(osrm, '_ultima_consulta', 9.6), \
             patch.object(osrm.time, 'monotonic', return_value=10), \
             patch.object(osrm.time, 'sleep') as dormir, \
             patch.object(osrm.requests, 'get', side_effect=requests.Timeout):
            with self.assertRaisesRegex(RuntimeError, 'OSRM'):
                osrm._consultar('table', ((-74.08, 4.74),))
        self.assertAlmostEqual(dormir.call_args.args[0], 0.6)

    def test_valida_radio_y_filas_antes_de_llamar_al_proveedor(self):
        with patch.object(osrm, '_consultar') as consultar:
            lejos = puntos()
            lejos.loc[1, 'latitud'] = 5.0
            for df in (lejos, pd.concat([puntos()] * 18)):
                with self.assertRaises(ValueError):
                    osrm.cargar_instancia(df)
        consultar.assert_not_called()

    def test_csv_mapeo_solver_y_mapa_sin_llm(self):
        import app
        self.assertTrue(app.app.config.suppress_callback_exceptions)
        contents = 'data:text/csv;base64,' + base64.b64encode(puntos().to_csv(index=False).encode()).decode()
        with patch.object(app.agente, 'preguntar') as preguntar:
            ui, aviso = app.procesar_archivo_llm(contents, 'puntos.csv')
        preguntar.assert_not_called()
        self.assertIsNotNone(ui)
        self.assertEqual(aviso, '')
        with patch.dict(app.CACHE_INSTANCIAS, {}, clear=True), \
             patch.dict(app.CACHE_VRP, {}, clear=True), \
             patch.object(osrm, '_consultar', side_effect=[tabla(), ruta()]) as consultar:
            respuesta = app.confirmar_y_generar_instancia(1, contents, 'puntos.csv', None,
                                                          'latitud', 'longitud', 'demanda')
            inst = respuesta[0].value
            self.assertEqual(respuesta[3], '')
            with patch.object(app, 'ctx', SimpleNamespace(triggered_id=None)):
                outputs = app.actualizar(inst, 1200, 2, 1, 8, 22, 8, 4500, 150000, 500, 'ambos', [], [])
            self.assertEqual(outputs[-1], app.ESTILO_DASHBOARD_VISIBLE)
            self.assertGreater(len(outputs[0].data), 2)
            self.assertEqual(consultar.call_count, 2)

    def test_mapeo_manual_si_falla_la_ia(self):
        import app
        csv = 'Y,X,kg\n4.74,-74.08,0\n4.741,-74.081,100\n'
        contents = 'data:text/csv;base64,' + base64.b64encode(csv.encode()).decode()
        with patch.object(app.agente, 'preguntar', side_effect=RuntimeError('No disponible')):
            ui, aviso = app.procesar_archivo_llm(contents, 'puntos.csv')
        self.assertIsNotNone(ui)
        self.assertIn('Selecciona las columnas', aviso)

    def test_fallo_geometria_se_muestra_al_usuario(self):
        import app
        with patch.object(osrm, '_consultar', return_value=tabla()):
            inst = osrm.cargar_instancia(puntos())
        with patch.dict(app.CACHE_VRP, {}, clear=True), \
             patch.object(osrm, '_geometrias', side_effect=RuntimeError('OSRM no disponible')), \
             patch.object(app, 'ctx', SimpleNamespace(triggered_id=None)):
            outputs = app.actualizar(inst, 1200, 2, 1, 8, 22, 8, 4500, 150000, 500, 'ambos', [], [])
        self.assertIs(outputs[0], no_update)
        self.assertEqual(outputs[2].children, 'OSRM no disponible')


if __name__ == '__main__':
    unittest.main()
