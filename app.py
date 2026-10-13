"""Simulador What-If de recolección de residuos — Suba, Bogotá."""

from __future__ import annotations

import math
import os
import sys
from functools import lru_cache
from pathlib import Path
from urllib.parse import urlparse

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

import plotly.graph_objects as go
from dash import ALL, MATCH, Dash, Input, Output, State, ctx, dcc, html, no_update

from src import agente
from src.config import (
    COSTO_FIJO_VEHICULO_BASE,
    COSTO_KM_BASE,
    HORA_INICIO_JORNADA,
    PENALIZACION_KG_BASE,
    SERVICIO_BASE_MIN,
    SUBA_CENTER,
)
from src.graph.osm_network import cargar_instancia
from src.routing.vrp import Ruta, Solucion, itinerario, resolver_vrp

COLORES = [
    "#0B6E4F",
    "#E07A3D",
    "#2C7FB8",
    "#8C2F39",
    "#6A4C93",
    "#BC4B51",
    "#3D5A80",
    "#C9A227",
    "#2A9D8F",
    "#E76F51",
    "#264653",
    "#F4A261",
]


def _kpi(titulo: str, valor: str, sub: str = "") -> html.Div:
    return html.Div(
        className="kpi",
        children=[
            html.Span(titulo, className="kpi-label"),
            html.Span(valor, className="kpi-value"),
            html.Span(sub, className="kpi-sub") if sub else None,
        ],
    )


def _fmt_cop(valor: float) -> str:
    return f"${valor:,.0f}".replace(",", ".")


def _fmt_hora(minutos_desde_salida: float) -> str:
    total = round(HORA_INICIO_JORNADA * 60 + minutos_desde_salida)
    return f"{(total // 60) % 24:02d}:{total % 60:02d}"


def _barra(titulo: str, valor: float, maximo: float, texto: str) -> html.Div:
    pct = 0.0 if maximo <= 0 else valor / maximo * 100
    return html.Div(
        className="barra",
        children=[
            html.Div(
                className="barra-cab",
                children=[html.Span(titulo), html.Span(texto)],
            ),
            html.Div(
                className="barra-fondo",
                children=html.Div(
                    className="barra-relleno" + (" lleno" if pct > 100 else ""),
                    style={"width": f"{min(pct, 100):.0f}%"},
                ),
            ),
        ],
    )


def _panel_conductor(
    inst,
    i: int,
    ruta: Ruta,
    *,
    abierto: bool,
    capacidad,
    jornada_h,
    velocidad,
    servicio,
    demanda,
    costo_km,
    costo_fijo,
) -> html.Details:
    color = COLORES[i % len(COLORES)]
    paradas, regreso_m, fin_min = itinerario(
        inst, ruta, velocidad_kmh=velocidad, servicio_min=servicio, demanda_mult=demanda
    )
    numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
    costo = ruta.distancia_m / 1000 * costo_km + costo_fijo
    filas = [
        html.Tr(
            [
                html.Td(str(k + 1)),
                html.Td(f"Punto {numero[p.cliente]}"),
                html.Td(_fmt_hora(p.llegada_min)),
                html.Td(f"{p.tramo_m / 1000:.2f}"),
                html.Td(f"{p.carga_acum_kg:.0f}"),
            ]
        )
        for k, p in enumerate(paradas)
    ]
    filas.append(
        html.Tr(
            className="regreso",
            children=[
                html.Td(""),
                html.Td("Regreso al patio"),
                html.Td(_fmt_hora(fin_min)),
                html.Td(f"{regreso_m / 1000:.2f}"),
                html.Td(f"{ruta.carga_kg:.0f}"),
            ],
        )
    )
    return html.Details(
        id={"type": "conductor", "index": i},
        open=abierto,
        className="conductor",
        style={"borderLeftColor": color},
        children=[
            html.Summary(
                children=[
                    html.Span(className="punto-color", style={"background": color}),
                    html.Strong(f"Conductor {i + 1} · Ruta {i + 1}"),
                    html.Span(
                        f"{len(ruta.clientes)} paradas · {ruta.carga_kg:.0f} kg · "
                        f"{ruta.distancia_m / 1000:.1f} km · "
                        f"{_fmt_hora(0)}–{_fmt_hora(fin_min)}",
                        className="resumen",
                    ),
                ]
            ),
            html.Div(
                className="conductor-cuerpo",
                children=[
                    html.Div(
                        className="conductor-metricas",
                        children=[
                            _barra(
                                "Carga",
                                ruta.carga_kg,
                                capacidad,
                                f"{ruta.carga_kg:.0f} / {capacidad:.0f} kg",
                            ),
                            _barra(
                                "Jornada",
                                ruta.tiempo_min,
                                jornada_h * 60,
                                f"{ruta.tiempo_min / 60:.1f} / {jornada_h:g} h",
                            ),
                            html.P(
                                [
                                    html.Span("Costo de la ruta: "),
                                    html.Strong(_fmt_cop(costo)),
                                    html.Span(
                                        f" (km {_fmt_cop(costo - costo_fijo)} + "
                                        f"fijo {_fmt_cop(costo_fijo)})",
                                        className="kpi-sub",
                                    ),
                                ]
                            ),
                        ],
                    ),
                    html.Div(
                        id={"type": "mapa-conductor", "index": i},
                        className="mini-mapa",
                    ),
                    html.Table(
                        className="itinerario",
                        children=[
                            html.Thead(
                                html.Tr(
                                    [
                                        html.Th("#"),
                                        html.Th("Parada"),
                                        html.Th("Llegada"),
                                        html.Th("Tramo (km)"),
                                        html.Th("Carga acum. (kg)"),
                                    ]
                                )
                            ),
                            html.Tbody(filas),
                        ],
                    ),
                ],
            ),
        ],
    )


def construir_conductores(inst, sol: Solucion, abiertos: set[int], **params) -> list:
    hijos: list = [
        _panel_conductor(inst, i, ruta, abierto=i in abiertos, **params)
        for i, ruta in enumerate(sol.rutas)
    ]
    if sol.no_cubiertos:
        numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
        puntos = ", ".join(str(numero[c]) for c in sorted(sol.no_cubiertos, key=numero.get))
        hijos.append(
            html.P(f"Sin conductor asignado: puntos {puntos}.", className="warn")
        )
    return hijos


def _encuadre(
    lats: list[float], lons: list[float], ancho_px: float, alto_px: float
) -> tuple[dict, float]:
    lat_min, lat_max, lon_min, lon_max = min(lats), max(lats), min(lons), max(lons)
    dlat = max(lat_max - lat_min, 1e-4)
    dlon = max(lon_max - lon_min, 1e-4)
    zoom = math.log2(min(ancho_px * 360 / (256 * dlon), alto_px * 360 / (256 * dlat)))
    centro = {"lat": (lat_min + lat_max) / 2, "lon": (lon_min + lon_max) / 2}
    return centro, min(zoom - 0.35, 16.5)


def _nodos_ruta(inst, ruta: Ruta) -> list[int]:
    nodos: list[int] = []
    secuencia = [inst.depot, *ruta.clientes, inst.depot]
    for a, b in zip(secuencia, secuencia[1:]):
        camino = inst.caminos.get((a, b))
        if not camino:
            continue
        nodos.extend(camino if not nodos else camino[1:])
    return nodos


def _aristas_bidireccionales(inst, rutas: list[Ruta]) -> set[tuple[int, int]]:
    aristas: set[tuple[int, int]] = set()
    for ruta in rutas:
        nodos = _nodos_ruta(inst, ruta)
        aristas.update(zip(nodos, nodos[1:]))
    return {(a, b) for a, b in aristas if (b, a) in aristas}


def _geometria_ruta(
    inst, ruta: Ruta, aristas_bidireccionales: set[tuple[int, int]]
) -> tuple[list[float | None], list[float | None]]:
    nodos = _nodos_ruta(inst, ruta)
    lats: list[float | None] = []
    lons: list[float | None] = []
    anterior_doble: bool | None = None
    desplazamiento_m = 2.5

    for a, b in zip(nodos, nodos[1:]):
        doble = (a, b) in aristas_bidireccionales
        lat_a, lon_a = inst.coords(a)
        lat_b, lon_b = inst.coords(b)
        punto_a = (lat_a, lon_a)
        punto_b = (lat_b, lon_b)

        if doble:
            lat_media = (lat_a + lat_b) / 2
            escala_lon = 111_320 * math.cos(math.radians(lat_media))
            este = (lon_b - lon_a) * escala_lon
            norte = (lat_b - lat_a) * 111_320
            longitud = math.hypot(este, norte)
            if longitud > 1e-6:
                offset_este = -norte / longitud * desplazamiento_m
                offset_norte = este / longitud * desplazamiento_m
                punto_a = (
                    lat_a + offset_norte / 111_320,
                    lon_a + offset_este / escala_lon,
                )
                punto_b = (
                    lat_b + offset_norte / 111_320,
                    lon_b + offset_este / escala_lon,
                )

        if anterior_doble is not None and anterior_doble != doble:
            lats.append(None)
            lons.append(None)
        lats.extend((punto_a[0], punto_b[0]))
        lons.extend((punto_a[1], punto_b[1]))
        anterior_doble = doble

    return lats, lons


def _flechas_ruta(
    lats: list[float | None], lons: list[float | None]
) -> tuple[list, list]:
    """Devuelve flechas sólidas pequeñas, espaciadas y agrupadas en una traza."""
    if len(lats) < 2:
        return [], []

    segmentos = []
    distancia_total = 0.0
    for idx in range(len(lats) - 1):
        lat1, lon1 = lats[idx], lons[idx]
        lat2, lon2 = lats[idx + 1], lons[idx + 1]
        if lat1 is None or lon1 is None or lat2 is None or lon2 is None:
            continue
        escala_lon = 111_320 * math.cos(math.radians((lat1 + lat2) / 2))
        este = (lon2 - lon1) * escala_lon
        norte = (lat2 - lat1) * 111_320
        longitud = math.hypot(este, norte)
        if longitud < 1e-6:
            continue
        segmentos.append((lat1, lon1, lat2, lon2, este, norte, longitud))
        distancia_total += longitud

    if not segmentos:
        return [], []

    cantidad = min(30, max(2, round(distancia_total / 200)))
    distancias_objetivo = [
        distancia_total * (idx + 1) / (cantidad + 1) for idx in range(cantidad)
    ]
    flecha_lats: list = []
    flecha_lons: list = []
    acumulada = 0.0
    objetivo_idx = 0

    for lat1, lon1, lat2, lon2, este, norte, longitud in segmentos:
        while (
            objetivo_idx < len(distancias_objetivo)
            and distancias_objetivo[objetivo_idx] <= acumulada + longitud
        ):
            fraccion = (distancias_objetivo[objetivo_idx] - acumulada) / longitud
            lat = lat1 + (lat2 - lat1) * fraccion
            lon = lon1 + (lon2 - lon1) * fraccion
            ux, uy = este / longitud, norte / longitud
            escala_lon = 111_320 * math.cos(math.radians(lat))
            punta = (ux * 16, uy * 16)
            hombro_izq = (ux * 2 - uy * 8, uy * 2 + ux * 8)
            cuello_izq = (ux * 2 - uy * 2.5, uy * 2 + ux * 2.5)
            cola_izq = (-ux * 12 - uy * 2.5, -uy * 12 + ux * 2.5)
            cola_der = (-ux * 12 + uy * 2.5, -uy * 12 - ux * 2.5)
            cuello_der = (ux * 2 + uy * 2.5, uy * 2 - ux * 2.5)
            hombro_der = (ux * 2 + uy * 8, uy * 2 - ux * 8)
            vertices = [
                cola_izq,
                cuello_izq,
                hombro_izq,
                punta,
                hombro_der,
                cuello_der,
                cola_der,
                cola_izq,
            ]
            flecha_lats.extend(lat + norte / 111_320 for _, norte in vertices)
            flecha_lons.extend(lon + este / escala_lon for este, _ in vertices)
            flecha_lats.append(None)
            flecha_lons.append(None)
            objetivo_idx += 1
        acumulada += longitud

    return flecha_lats, flecha_lons


def construir_mapa_conductor(inst, i: int, ruta: Ruta) -> go.Figure:
    color = COLORES[i % len(COLORES)]
    lats, lons = _geometria_ruta(
        inst, ruta, _aristas_bidireccionales(inst, [ruta])
    )
    flecha_lats, flecha_lons = _flechas_ruta(lats, lons)
    numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
    plat = [inst.coords(c)[0] for c in ruta.clientes]
    plon = [inst.coords(c)[1] for c in ruta.clientes]
    dlat, dlon = inst.coords(inst.depot)
    fig = go.Figure(
        [
            go.Scattermap(
                lat=lats,
                lon=lons,
                mode="lines",
                line={"width": 4, "color": color},
                hoverinfo="skip",
            ),
            go.Scattermap(
                lat=flecha_lats,
                lon=flecha_lons,
                mode="lines",
                fill="toself",
                fillcolor="white",
                line={"width": 1, "color": "#174d3e"},
                hoverinfo="skip",
            ),
            go.Scattermap(
                lat=plat,
                lon=plon,
                mode="markers+text",
                marker={"size": 18, "color": color},
                text=[str(k + 1) for k in range(len(ruta.clientes))],
                textfont={"color": "white", "size": 11},
                customdata=[numero[c] for c in ruta.clientes],
                hovertemplate="Parada %{text} · Punto %{customdata}<extra></extra>",
            ),
            go.Scattermap(
                lat=[dlat],
                lon=[dlon],
                mode="markers",
                marker={"size": 16, "color": "#e9c46a"},
                hovertemplate="Patio<extra></extra>",
            ),
        ]
    )
    centro, zoom = _encuadre(
        [lat for lat in lats if lat is not None] + [dlat],
        [lon for lon in lons if lon is not None] + [dlon],
        480,
        360,
    )
    fig.update_layout(
        margin={"l": 0, "r": 0, "t": 0, "b": 0},
        showlegend=False,
        map={"style": "open-street-map", "center": centro, "zoom": zoom},
        autosize=True,
        uirevision=f"conductor-{i}-{ruta.clientes}",
    )
    return fig


def construir_mapa(inst, sol: Solucion) -> go.Figure:
    fig = go.Figure()
    aristas_bidireccionales = _aristas_bidireccionales(inst, sol.rutas)

    for i, ruta in enumerate(sol.rutas):
        lats, lons = _geometria_ruta(inst, ruta, aristas_bidireccionales)
        color = COLORES[i % len(COLORES)]
        fig.add_trace(
            go.Scattermap(
                lat=lats,
                lon=lons,
                mode="lines",
                line=dict(width=3.2, color=color),
                name=f"Ruta {i + 1} · {ruta.carga_kg:.0f} kg",
                hoverinfo="name",
            )
        )

    clat = [inst.coords(c)[0] for c in inst.clientes]
    clon = [inst.coords(c)[1] for c in inst.clientes]
    ctext = [
        f"Punto {i + 1}<br>{inst.demanda[c]:.0f} kg base"
        for i, c in enumerate(inst.clientes)
    ]
    fig.add_trace(
        go.Scattermap(
            lat=clat,
            lon=clon,
            mode="markers",
            marker=dict(size=9, color="#1a1a1a"),
            name="Acumulación",
            text=ctext,
            hovertemplate="%{text}<extra></extra>",
        )
    )

    if sol.no_cubiertos:
        ulat = [inst.coords(c)[0] for c in sol.no_cubiertos]
        ulon = [inst.coords(c)[1] for c in sol.no_cubiertos]
        fig.add_trace(
            go.Scattermap(
                lat=ulat,
                lon=ulon,
                mode="markers",
                marker=dict(size=11, color="#c1121f"),
                name="No cubiertos",
            )
        )

    dlat, dlon = inst.coords(inst.depot)
    fig.add_trace(
        go.Scattermap(
            lat=[dlat],
            lon=[dlon],
            mode="markers",
            marker=dict(size=16, color="#e9c46a"),
            name="Patio",
            text=["Patio de operaciones (Suba)"],
            hovertemplate="%{text}<extra></extra>",
        )
    )

    fig.update_layout(
        margin=dict(l=0, r=0, t=0, b=0),
        map=dict(
            style="open-street-map",
            center=dict(lat=SUBA_CENTER[0], lon=SUBA_CENTER[1]),
            zoom=13.1,
        ),
        legend=dict(
            orientation="h",
            yanchor="bottom",
            y=0.01,
            x=0.01,
            bgcolor="rgba(255,255,255,0.85)",
        ),
        autosize=True,
        uirevision="mapa",
    )
    return fig


def _estado_asistente() -> str:
    falta = agente.falta_configuracion()
    if falta:
        return falta
    cfg = agente.configuracion()
    return (
        f"Modelo {cfg['modelo']} vía {urlparse(cfg['base_url']).netloc}. "
        "Responde con los datos del escenario que tengas en pantalla."
    )


def _render_chat(historial: list[dict]) -> list:
    if not historial:
        return [
            html.P(
                "Ejemplos: ¿Qué conductor va más cargado? ¿Por qué quedan puntos sin "
                "recoger? ¿A qué hora llega el conductor 2 al punto 14?",
                className="kpi-sub",
            )
        ]
    burbujas = [
        html.Div(
            className=f"chat-msg {m['role']}",
            children=dcc.Markdown(m["content"]) if m["role"] == "assistant" else m["content"],
        )
        for m in historial
    ]
    # El contenedor usa column-reverse para quedar anclado al último mensaje.
    return burbujas[::-1]


print("Cargando red vial de Suba (OpenStreetMap)...")
INSTANCIA = cargar_instancia()
print(
    f"Grafo: {INSTANCIA.grafo.number_of_nodes()} nodos, "
    f"{INSTANCIA.grafo.number_of_edges()} aristas, "
    f"{len(INSTANCIA.clientes)} puntos de demanda."
)

app = Dash(__name__)
app.title = "SAGI · Residuos Suba"

app.layout = html.Div(
    className="shell",
    children=[
        html.Header(
            children=[
                html.P(
                    "Sistema de análisis georeactivo inteligente",
                    className="eyebrow",
                ),
                html.H1("SAGI"),
            ]
        ),
        html.Div(
            className="grid",
            children=[
                html.Aside(
                    className="panel",
                    children=[
                        html.H2("Escenario hipotético"),
                        html.Label("Capacidad del vehículo (kg)"),
                        dcc.Slider(
                            id="capacidad",
                            min=400,
                            max=4000,
                            step=100,
                            value=1200,
                            marks={400: "400", 1200: "1.200", 2500: "2.500", 4000: "4.000"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Número máximo de vehículos"),
                        dcc.Slider(
                            id="vehiculos",
                            min=1,
                            max=12,
                            step=1,
                            value=6,
                            marks={1: "1", 6: "6", 12: "12"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Multiplicador de demanda"),
                        dcc.Slider(
                            id="demanda",
                            min=0.5,
                            max=2.5,
                            step=0.1,
                            value=1.0,
                            marks={0.5: "0.5×", 1: "1×", 1.5: "1.5×", 2.5: "2.5×"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Ventana de jornada (horas)"),
                        dcc.Slider(
                            id="jornada",
                            min=4,
                            max=12,
                            step=0.5,
                            value=8,
                            marks={4: "4 h", 8: "8 h", 12: "12 h"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Velocidad promedio (km/h)"),
                        dcc.Slider(
                            id="velocidad",
                            min=12,
                            max=40,
                            step=1,
                            value=22,
                            marks={12: "12", 22: "22", 40: "40"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Tiempo de servicio por punto (min)"),
                        dcc.Slider(
                            id="servicio",
                            min=3,
                            max=20,
                            step=1,
                            value=SERVICIO_BASE_MIN,
                            marks={3: "3", 8: "8", 20: "20"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Costo operativo (COP / km)"),
                        dcc.Slider(
                            id="costo",
                            min=2000,
                            max=9000,
                            step=250,
                            value=COSTO_KM_BASE,
                            marks={2000: "2k", 4500: "4.5k", 9000: "9k"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Costo fijo por vehículo (COP / día)"),
                        dcc.Slider(
                            id="costo_fijo",
                            min=0,
                            max=600_000,
                            step=10_000,
                            value=COSTO_FIJO_VEHICULO_BASE,
                            marks={0: "0", 150_000: "150k", 600_000: "600k"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Label("Penalización por kg no recogido (COP / kg)"),
                        dcc.Slider(
                            id="penalizacion",
                            min=0,
                            max=3000,
                            step=100,
                            value=PENALIZACION_KG_BASE,
                            marks={0: "0", 500: "500", 3000: "3k"},
                            tooltip={"placement": "bottom", "always_visible": False},
                        ),
                        html.Div(id="estado", className="estado"),
                    ],
                ),
                html.Main(
                    children=[
                        html.Div(id="kpis", className="kpis"),
                        html.Div(
                            className="mapa-wrap",
                            children=dcc.Graph(
                                id="mapa",
                                config={"displayModeBar": False, "scrollZoom": True},
                                responsive=True,
                                style={"height": "720px"},
                            ),
                        ),
                        html.Section(
                            className="conductores",
                            children=[
                                html.H2("Conductores"),
                                html.P(
                                    "Detalle por ruta. Se recalcula con los "
                                    f"parámetros del escenario (salida {_fmt_hora(0)}).",
                                    className="kpi-sub",
                                ),
                                html.Div(id="conductores"),
                            ],
                        ),
                        html.Section(
                            className="asistente",
                            children=[
                                html.H2("Asistente de rutas"),
                                html.P(_estado_asistente(), className="kpi-sub"),
                                dcc.Store(id="chat-historial", data=[]),
                                dcc.Loading(
                                    html.Div(
                                        _render_chat([]),
                                        id="chat-mensajes",
                                        className="chat-mensajes",
                                    ),
                                    type="dot",
                                    overlay_style={"visibility": "visible", "opacity": 0.6},
                                ),
                                html.Div(
                                    className="chat-entrada",
                                    children=[
                                        dcc.Input(
                                            id="chat-pregunta",
                                            type="text",
                                            placeholder="Pregunta sobre las rutas del "
                                            "escenario actual...",
                                            className="chat-input",
                                            n_submit=0,
                                        ),
                                        html.Button(
                                            "Preguntar", id="chat-enviar", n_clicks=0
                                        ),
                                        html.Button(
                                            "Limpiar",
                                            id="chat-limpiar",
                                            n_clicks=0,
                                            className="secundario",
                                        ),
                                    ],
                                ),
                            ],
                        ),
                    ]
                ),
            ],
        ),
    ],
)


SLIDERS = (
    "capacidad",
    "vehiculos",
    "demanda",
    "jornada",
    "velocidad",
    "servicio",
    "costo",
    "costo_fijo",
    "penalizacion",
)


@lru_cache(maxsize=32)
def resolver(
    capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion
) -> Solucion:
    return resolver_vrp(
        INSTANCIA,
        capacidad_kg=float(capacidad),
        max_vehiculos=int(vehiculos),
        demanda_mult=float(demanda),
        jornada_h=float(jornada),
        velocidad_kmh=float(velocidad),
        servicio_min=float(servicio),
        costo_km=float(costo),
        costo_fijo_vehiculo=float(costo_fijo),
        penalizacion_kg=float(penalizacion),
    )


@app.callback(
    Output({"type": "mapa-conductor", "index": MATCH}, "children"),
    Input({"type": "conductor", "index": MATCH}, "open"),
    *[State(s, "value") for s in SLIDERS],
)
def mapa_conductor(abierto, *valores):
    # Solo se monta el mapa del panel abierto: cada mapa es un contexto WebGL.
    if not abierto:
        return None
    sol = resolver(*valores)
    i = ctx.outputs_list["id"]["index"]
    if i >= len(sol.rutas):
        return None
    return dcc.Graph(
        figure=construir_mapa_conductor(INSTANCIA, i, sol.rutas[i]),
        config={"displayModeBar": False, "scrollZoom": True},
        responsive=True,
        style={"height": "360px"},
    )


@app.callback(
    Output("mapa", "figure"),
    Output("kpis", "children"),
    Output("estado", "children"),
    Output("conductores", "children"),
    Input("capacidad", "value"),
    Input("vehiculos", "value"),
    Input("demanda", "value"),
    Input("jornada", "value"),
    Input("velocidad", "value"),
    Input("servicio", "value"),
    Input("costo", "value"),
    Input("costo_fijo", "value"),
    Input("penalizacion", "value"),
    State({"type": "conductor", "index": ALL}, "open"),
    State({"type": "conductor", "index": ALL}, "id"),
)
def actualizar(
    capacidad,
    vehiculos,
    demanda,
    jornada,
    velocidad,
    servicio,
    costo,
    costo_fijo,
    penalizacion,
    paneles_abiertos,
    paneles_ids,
):
    sol = resolver(
        capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion
    )
    abiertos = {
        pid["index"] for pid, abierto in zip(paneles_ids or [], paneles_abiertos or []) if abierto
    }
    kpis = [
        _kpi(
            "Costo total",
            _fmt_cop(sol.costo_cop),
            f"km {_fmt_cop(sol.costo_variable_cop)} · flota {_fmt_cop(sol.costo_fijo_cop)}"
            f" · penalización {_fmt_cop(sol.penalizacion_cop)}",
        ),
        _kpi("Distancia", f"{sol.distancia_total_m / 1000:.1f} km", "red vial OSM"),
        _kpi(
            "Horas-vehículo",
            f"{sol.tiempo_total_min / 60:.1f} h",
            f"ruta más larga {sol.tiempo_max_ruta_min / 60:.1f} h",
        ),
        _kpi(
            "Vehículos",
            f"{sol.vehiculos_usados} / {int(vehiculos)}",
            f"{sol.demanda_cubierta_kg:.0f} kg cubiertos",
        ),
        _kpi(
            "No recogido",
            f"{sol.demanda_no_cubierta_kg:.0f} kg",
            f"{len(sol.no_cubiertos)} punto(s)",
        ),
    ]
    if sol.factible:
        estado = html.P(
            "Escenario factible: toda la demanda cabe en capacidad, flota y jornada.",
            className="ok",
        )
    else:
        n = len(sol.no_cubiertos)
        estado = html.P(
            f"Escenario tensionado: {n} punto(s) quedan fuera. "
            "Sube capacidad, flota o jornada, o baja la demanda.",
            className="warn",
        )
    conductores = construir_conductores(
        INSTANCIA,
        sol,
        abiertos,
        capacidad=float(capacidad),
        jornada_h=float(jornada),
        velocidad=float(velocidad),
        servicio=float(servicio),
        demanda=float(demanda),
        costo_km=float(costo),
        costo_fijo=float(costo_fijo),
    )
    return construir_mapa(INSTANCIA, sol), kpis, estado, conductores



@app.callback(
    Output("chat-historial", "data"),
    Output("chat-mensajes", "children"),
    Output("chat-pregunta", "value"),
    Input("chat-enviar", "n_clicks"),
    Input("chat-pregunta", "n_submit"),
    Input("chat-limpiar", "n_clicks"),
    State("chat-pregunta", "value"),
    State("chat-historial", "data"),
    *[State(s, "value") for s in SLIDERS],
    running=[(Output("chat-enviar", "disabled"), True, False)],
    prevent_initial_call=True,
)
def chat(_enviar, _enter, _limpiar, pregunta, historial, *valores):
    if ctx.triggered_id == "chat-limpiar":
        return [], _render_chat([]), ""
    historial = list(historial or [])
    pregunta = (pregunta or "").strip()
    if not pregunta:
        return no_update, no_update, no_update
    historial.append({"role": "user", "content": pregunta})
    falta = agente.falta_configuracion()
    if falta:
        historial.append({"role": "error", "content": falta})
    else:
        sol = resolver(*valores)
        params = dict(zip(SLIDERS, map(float, valores)))
        try:
            respuesta = agente.preguntar(
                historial, agente.contexto_escenario(INSTANCIA, sol, params)
            )
            historial.append({"role": "assistant", "content": respuesta})
        except RuntimeError as e:
            historial.append({"role": "error", "content": str(e)})
    return historial, _render_chat(historial), ""


if __name__ == "__main__":
    app.run(
        debug=False,
        host="0.0.0.0",
        port=int(os.environ.get("PORT", 8051)),
    )
