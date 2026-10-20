"""Parámetros geográficos y de simulación para Suba, Bogotá."""

from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
DATA_DIR = ROOT / "data"
OSM_CACHE = DATA_DIR / "suba_osm.json"
INSTANCE_CACHE = DATA_DIR / "suba_instancia.json"

# Núcleo urbano de Suba (Parque de Suba / centro de localidad).
# Un radio limitado mantiene el grafo interactivo sin bajar toda la localidad rural.
SUBA_CENTER = (4.7413, -74.0837)  # lat, lon
RADIO_METROS = 2200  # Radio del extracto histórico data/suba_osm.json.
# El piloto dinámico mide el radio desde el patio definido en la primera fila.
RADIO_MAX_METROS = 15_000
MAX_PUNTOS_PILOTO = 51  # Un patio y hasta 50 clientes; matriz cuadrática acotada.

# Servidores del piloto dinámico (OSMnx añade /interpreter a la URL base).
OVERPASS_URLS = (
    "https://overpass-api.de/api/interpreter",
    "https://overpass.kumi.systems/api/interpreter",
)
OVERPASS_HEADERS = {
    "User-Agent": "simulador-residuos-suba/0.1 (what-if; contacto local)",
    "Accept": "application/json",
}

HIGHWAY_TYPES = (
    "motorway",
    "trunk",
    "primary",
    "secondary",
    "tertiary",
    "unclassified",
    "residential",
    "living_street",
    "service",
    "motorway_link",
    "trunk_link",
    "primary_link",
    "secondary_link",
    "tertiary_link",
)

N_PUNTOS_RECOLECCION = 28
DEMANDA_BASE_KG = 180
SERVICIO_BASE_MIN = 8
COSTO_KM_BASE = 4_500  # COP / km, orden de magnitud operativo
COSTO_FIJO_VEHICULO_BASE = 150_000  # COP / vehículo-día (hipotético)
PENALIZACION_KG_BASE = 500  # COP por kg no recogido (costo social hipotético)
HORA_INICIO_JORNADA = 6.0  # hora de salida del patio (06:00)

# Patio sintético cerca del borde operativo (no es un Relleno Sanitario real).
PATIO_LATLON = (4.7525, -74.0920)
