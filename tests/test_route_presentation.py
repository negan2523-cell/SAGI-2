"""Regresiones de sentido de circulación y legibilidad sin servicios externos."""
import math
import unittest
from unittest.mock import patch

from src.presentation.arrows import project, unproject, route_arrows
from src.graph import osrm_network as osrm
from tests.test_osrm_network import puntos, tabla, ruta


def centers(result, zoom):
    lats, lons = result
    # El cuello del chevron (índice 3) está dos píxeles detrás del centro.
    return [project(lats[i], lons[i], zoom) for i in range(3, len(lats), 6)]


class ArrowTests(unittest.TestCase):
    def test_same_road_reverse_different_vertices_has_no_conflicting_arrows(self):
        outbound = ([4.74, 4.75], [-74.08, -74.08])
        inbound = ([4.75, 4.747, 4.744, 4.74], [-74.08] * 4)
        both = route_arrows([outbound, inbound], 16)
        self.assertTrue(all(not lat for lat, lon in both))
        self.assertGreater(len(route_arrows([outbound], 16)[0][0]), 0)
        self.assertGreater(len(route_arrows([inbound], 16)[0][0]), 0)

    def test_arrow_size_stays_in_pixels_at_different_zoom(self):
        path = ([4.74, 4.75], [-74.08, -74.08])
        for zoom in (13, 16, 19):
            lats, lons = route_arrows([path], zoom)[0]
            shoulder = project(lats[0], lons[0], zoom)
            tip = project(lats[1], lons[1], zoom)
            self.assertAlmostEqual(math.dist(shoulder, tip), math.hypot(14, 5), places=5)

    def test_repeated_path_and_crossings_respect_screen_spacing(self):
        path = ([4.74, 4.75], [-74.08, -74.08])
        crossing = ([4.745, 4.745], [-74.09, -74.07])
        arrows = route_arrows([path, path, crossing], 16)
        positions = [p for result in arrows for p in centers(result, 16)]
        # Vertices used for measurement are within 2 px of the collision center.
        self.assertTrue(all(math.dist(a, b) >= 56 for i, a in enumerate(positions) for b in positions[i + 1:]))

    def test_gaps_duplicates_and_short_paths_do_not_create_spurious_arrows(self):
        for path in (([], []), ([4.74], [-74.08]),
                     ([4.74, 4.74], [-74.08, -74.08]),
                     ([4.74, None, 4.75], [-74.08, None, -74.08])):
            self.assertEqual(route_arrows([path], 16), [([], [])])

    def test_avoids_stop_markers(self):
        path = ([4.74, 4.75], [-74.08, -74.08])
        result = route_arrows([path], 16)[0]
        stops = [unproject(x, y, 16) for x, y in centers(result, 16)]
        # Every former arrow center is now occupied by a stop marker.
        self.assertEqual(route_arrows([path], 16, stops), [([], [])])


class RoutePresentationTests(unittest.TestCase):
    def test_return_starts_at_last_pickup_even_when_not_farthest(self):
        import app
        from src.routing.vrp import Ruta
        osrm._tabla.cache_clear()
        osrm._geometrias.cache_clear()
        with patch.object(osrm, '_consultar', side_effect=[tabla(), ruta()]):
            inst = osrm.cargar_instancia(puntos())
            # Point 1 is farther, but point 2 is the last collection stop.
            # Shift the depot near point 2 to expose the old farthest-node split.
            inst.grafo.nodes[0]['lat'] = 4.7423
            inst.grafo.nodes[0]['lon'] = -74.0826
            route = Ruta(clientes=[1, 2], carga_kg=300, distancia_m=700, tiempo_min=20)
            ida, vuelta = app._geometria_ruta(inst, route)
        self.assertEqual((ida[0][-1], ida[1][-1]), inst.coords(2))
        self.assertEqual((vuelta[0][0], vuelta[1][0]), inst.coords(2))
        self.assertIn(-74.0838, ida[1])
        self.assertNotIn(-74.0838, vuelta[1])

    def test_driver_filters_and_zoom_keep_real_lines(self):
        import app
        from src.routing.vrp import Ruta
        osrm._tabla.cache_clear()
        osrm._geometrias.cache_clear()
        with patch.object(osrm, '_consultar', side_effect=[tabla(), ruta()]):
            inst = osrm.cargar_instancia(puntos())
            route = Ruta(clientes=[1, 2], carga_kg=300, distancia_m=700, tiempo_min=20)
            all_paths = app.construir_mapa_conductor(inst, 0, route)
            outbound = app.construir_mapa_conductor(inst, 0, route, 'ida', 18)
            inbound = app.construir_mapa_conductor(inst, 0, route, 'vuelta', 18)
        self.assertEqual(tuple(all_paths.data[0].lat), tuple(outbound.data[0].lat))
        self.assertTrue(outbound.data[0].visible)
        self.assertFalse(outbound.data[1].visible)
        self.assertFalse(inbound.data[0].visible)
        self.assertTrue(inbound.data[1].visible)
        self.assertEqual(outbound.layout.map.zoom, 18)
        self.assertEqual(tuple(all_paths.data[5].lat), (inst.coords(0)[0],))

    def test_initial_mobile_view_contains_the_entire_street_geometry(self):
        import app
        from src.routing.vrp import Ruta
        osrm._tabla.cache_clear()
        osrm._geometrias.cache_clear()
        with patch.object(osrm, '_consultar', side_effect=[tabla(), ruta()]):
            inst = osrm.cargar_instancia(puntos())
            route = Ruta(clientes=[1, 2], carga_kg=300, distancia_m=700, tiempo_min=20)
            fig = app.construir_mapa_conductor(inst, 0, route, viewport={"width": 390})
        width, height = app._tamano_mapa({"width": 390}, conductor=True)
        center = project(fig.layout.map.center.lat, fig.layout.map.center.lon, fig.layout.map.zoom)
        for trace in fig.data[:2]:
            for lat, lon in zip(trace.lat, trace.lon):
                if lat is not None:
                    x, y = project(lat, lon, fig.layout.map.zoom)
                    self.assertLess(abs(x - center[0]), width / 2)
                    self.assertLess(abs(y - center[1]), height / 2)

    def test_new_scenario_clears_session_and_upload(self):
        import app
        self.assertEqual(app.nuevo_escenario(1), (None, None))
        self.assertEqual(app.procesar_archivo_llm(None, None), (None, ''))
