"""Flechas en coordenadas de pantalla: tamaño estable y sin sentidos enfrentados."""
import math


def project(lat, lon, zoom):
    size = 512 * 2 ** zoom  # MapLibre usa un mundo de 512 px en zoom 0.
    sin_lat = math.sin(math.radians(max(-85, min(85, lat))))
    return (lon + 180) / 360 * size, (0.5 - math.log((1 + sin_lat) / (1 - sin_lat)) / (4 * math.pi)) * size


def unproject(x, y, zoom):
    size = 512 * 2 ** zoom
    return math.degrees(math.atan(math.sinh(math.pi * (1 - 2 * y / size)))), x / size * 360 - 180


def _segments(lats, lons, zoom):
    segments = []
    for lat1, lon1, lat2, lon2 in zip(lats, lons, lats[1:], lons[1:]):
        if None in (lat1, lon1, lat2, lon2):
            continue
        a, b = project(lat1, lon1, zoom), project(lat2, lon2, zoom)
        length = math.dist(a, b)
        if length > 1e-6:
            segments.append((a, b, length, ((b[0] - a[0]) / length, (b[1] - a[1]) / length)))
    return segments


def _distance_to_segment(p, a, b, length):
    t = max(0, min(1, ((p[0] - a[0]) * (b[0] - a[0]) + (p[1] - a[1]) * (b[1] - a[1])) / length ** 2))
    return math.dist(p, (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1])))


def route_arrows(paths, zoom, stops=()):
    """Una pareja lat/lon por trayecto visible, con colisiones compartidas.

    No se altera la línea real. Si otro tramo pasa en sentido contrario a menos
    de 12 m, se omite la flecha. El filtro de un solo trayecto permite inspeccionarlo.
    Se aplica también a una ruta que se devuelve dentro de la propia recolección.
    """
    zoom = max(0, min(22, float(zoom)))
    segments = [_segments(*path, zoom) for path in paths]
    all_segments = [s for path in segments for s in path]
    occupied = [project(lat, lon, zoom) for lat, lon in stops]
    stop_count = len(occupied)
    results = []
    for path in segments:
        lats, lons = [], []
        total = sum(s[2] for s in path)
        count = min(24, int(total / 76))
        targets = [total * (k + 1) / (count + 1) for k in range(count)]
        travelled, index = 0.0, 0
        for a, b, length, (ux, uy) in path:
            while index < count and targets[index] <= travelled + length:
                t = (targets[index] - travelled) / length
                p = (a[0] + t * (b[0] - a[0]), a[1] + t * (b[1] - a[1]))
                index += 1
                if any(math.dist(p, q) < (24 if k < stop_count else 60) for k, q in enumerate(occupied)):
                    continue
                lat, _ = unproject(*p, zoom)
                tolerance = 12 / (math.cos(math.radians(lat)) * 2 * math.pi * 6378137 / (512 * 2 ** zoom))
                if any(ux * direction[0] + uy * direction[1] < -0.7 and
                       _distance_to_segment(p, start, end, distance) <= tolerance
                       for start, end, distance, direction in all_segments):
                    continue
                # Chevron de 14 × 10 px, orientado con el sentido del recorrido.
                for along, across in ((-6, -5), (8, 0), (-6, 5), (-2, 0), (-6, -5)):
                    plat, plon = unproject(p[0] + ux * along - uy * across,
                                           p[1] + uy * along + ux * across, zoom)
                    lats.append(plat)
                    lons.append(plon)
                lats.append(None)
                lons.append(None)
                occupied.append(p)
            travelled += length
        results.append((lats, lons))
    return results
