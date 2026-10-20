"""Simulador What-If de recolección de residuos — Suba, Bogotá. (Arquitectura SaaS Multi-tenant)"""

from __future__ import annotations

import base64
import hashlib
import io
import json
import math
import os
import sys
import uuid
from pathlib import Path
from urllib.parse import urlparse

import pandas as pd
import plotly.graph_objects as go
from dash import ALL, MATCH, Input, Output, State, ctx, dcc, html, no_update
from dash_extensions.enrich import (
    DashProxy,
    FileSystemBackend,
    Serverside,
    ServersideOutputTransform,
)

ROOT = Path(__file__).resolve().parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from src import agente
from src.config import (
    COSTO_FIJO_VEHICULO_BASE,
    COSTO_KM_BASE,
    HORA_INICIO_JORNADA,
    PENALIZACION_KG_BASE,
    SERVICIO_BASE_MIN,
    SUBA_CENTER,
)
from src.graph.osrm_network import cargar_instancia
from src.routing.vrp import Ruta, Solucion, itinerario, resolver_vrp
from src.presentation.arrows import project, route_arrows

COLORES = [
    "#0B6E4F", "#E07A3D", "#2C7FB8", "#8C2F39", "#6A4C93",
    "#BC4B51", "#3D5A80", "#C9A227", "#2A9D8F", "#E76F51",
    "#264653", "#F4A261",
]

ESTILO_UPLOAD_VISIBLE = {"display": "block"}
ESTILO_OCULTO = {"display": "none"}
ESTILO_DASHBOARD_VISIBLE = {"display": "block"}

# Configuración de almacenamiento para despliegues en la nube (Render usa /tmp efímero pero estable por instancia)
fs_store = FileSystemBackend(cache_dir="/tmp/dash_cache")
app = DashProxy(__name__, suppress_callback_exceptions=True,
                transforms=[ServersideOutputTransform(backends=[fs_store])])
app.title = "SAGI · Planificación de rutas"


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
    inst, i: int, ruta: Ruta, *, abierto: bool, capacidad, jornada_h, velocidad, servicio, demanda, costo_km, costo_fijo
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
                html.Td(f"Punto {numero.get(p.cliente, '?')}"),
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
                            _barra("Carga", ruta.carga_kg, capacidad, f"{ruta.carga_kg:.0f} / {capacidad:.0f} kg"),
                            _barra("Jornada", ruta.tiempo_min, jornada_h * 60, f"{ruta.tiempo_min / 60:.1f} / {jornada_h:g} h"),
                            html.P([
                                html.Span("Costo de la ruta: "),
                                html.Strong(_fmt_cop(costo)),
                                html.Span(f" (km {_fmt_cop(costo - costo_fijo)} + fijo {_fmt_cop(costo_fijo)})", className="kpi-sub"),
                            ]),
                        ],
                    ),
                    html.Div(
                        children=[
                            dcc.RadioItems(
                                id={"type": "filtro-conductor", "index": i},
                                options=[
                                    {"label": "Solo ida", "value": "ida"},
                                    {"label": "Solo regreso", "value": "vuelta"},
                                    {"label": "Todo el recorrido", "value": "ambos"},
                                ],
                                value="ambos",
                                inline=True,
                                className="segmented",
                            ),
                            html.P("Las flechas indican el sentido. En tramos de doble paso, selecciona solo ida o solo regreso.", className="map-hint"),
                            dcc.Graph(
                                id={"type": "mapa-conductor-graph", "index": i},
                                figure=go.Figure(),
                                className="mini-mapa",
                                config={"displayModeBar": False, "scrollZoom": True},
                                responsive=True,
                                style={"height": "360px"},
                            ),
                        ]
                    ),
                    html.Div(className="table-scroll", children=html.Table(
                        className="itinerario",
                        children=[
                            html.Thead(html.Tr([html.Th("#"), html.Th("Parada"), html.Th("Llegada"), html.Th("Tramo (km)"), html.Th("Carga acum. (kg)")])),
                            html.Tbody(filas),
                        ],
                    )),
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
        puntos = ", ".join(str(numero.get(c, c)) for c in sorted(sol.no_cubiertos, key=lambda x: numero.get(x, 0)))
        hijos.append(html.P(f"Sin conductor asignado: puntos {puntos}.", className="warn"))
    return hijos


def _tamano_mapa(viewport=None, *, conductor=False):
    width = (viewport or {}).get("width", 1440)
    if width <= 560:
        available = width - 34
    elif width <= 900:
        available = width - 50
    elif width <= 1200:
        available = width - 316
    else:
        available = min(width, 1600) - 382
    if conductor:
        available -= 40 + (270 if width > 1200 else 0)
    return max(220, available), 360 if conductor else (400 if width <= 560 else 520)


def _encuadre(lats: list[float], lons: list[float], ancho_px: float, alto_px: float) -> tuple[dict, float]:
    points = [project(lat, lon, 0) for lat, lon in zip(lats, lons)]
    xs, ys = zip(*points)
    width, height = max(max(xs) - min(xs), 1e-4), max(max(ys) - min(ys), 1e-4)
    zoom = math.log2(min(ancho_px / width, alto_px / height)) - 0.35
    centro = {"lat": (min(lats) + max(lats)) / 2, "lon": (min(lons) + max(lons)) / 2}
    return centro, max(0, min(zoom, 16.5))


def _nodos_ruta(inst, ruta: Ruta) -> list[int]:
    nodos: list[int] = []
    secuencia = [inst.depot, *ruta.clientes, inst.depot]
    inst.preparar_ruta(secuencia)
    for a, b in zip(secuencia, secuencia[1:]):
        camino = inst.caminos.get((a, b))
        if not camino:
            continue
        nodos.extend(camino if not nodos else camino[1:])
    return nodos


def _geometria_ruta(inst, ruta: Ruta) -> tuple[tuple[list[float | None], list[float | None]], tuple[list[float | None], list[float | None]]]:
    if not ruta.clientes:
        return ([], []), ([], [])
    secuencia = [inst.depot, *ruta.clientes, inst.depot]
    inst.preparar_ruta(secuencia)

    def nodos_de(tramos):
        nodos = []
        for origen, destino in zip(tramos, tramos[1:]):
            camino = inst.caminos.get((origen, destino), [])
            nodos.extend(camino if not nodos else camino[1:])
        return nodos

    # La recolección termina en la última parada, aunque no sea la más lejana.
    return (_geometria_nodos(inst, nodos_de(secuencia[:-1])),
            _geometria_nodos(inst, nodos_de(secuencia[-2:])))


def _geometria_nodos(inst, nodos: list[int]) -> tuple[list[float | None], list[float | None]]:
    lats: list[float | None] = []
    lons: list[float | None] = []
    if len(nodos) < 2:
        return lats, lons
    tramo: list[tuple[float, float]] = []
    for a, b in zip(nodos, nodos[1:]):
        arista = inst.grafo.get_edge_data(a, b)
        geometries = []
        if arista:
            geometry = arista.get("geometry")
            if geometry is not None:
                geometries.append(geometry)
            else:
                geometries.extend(
                    datos_arista["geometry"] for datos_arista in arista.values()
                    if isinstance(datos_arista, dict) and datos_arista.get("geometry") is not None
                )
        pts_candidatos = []
        for geometry in geometries:
            if isinstance(geometry, dict):
                coordenadas = geometry["coordinates"]
            elif hasattr(geometry, "coords"):
                coordenadas = geometry.coords
            else:
                coordenadas = geometry
            pts = [(float(lat), float(lon)) for lon, lat in coordenadas]
            if len(pts) >= 2:
                longitud = sum(math.dist(p1, p2) for p1, p2 in zip(pts, pts[1:]))
                pts_candidatos.append((longitud, pts))
        pts = min(pts_candidatos, key=lambda candidato: candidato[0])[1] if pts_candidatos else [inst.coords(a), inst.coords(b)]
        origen = inst.coords(a)
        if math.dist(pts[-1], origen) < math.dist(pts[0], origen):
            pts = pts[::-1]
        if tramo:
            ultimo_punto = tramo[-1]
            if math.dist(ultimo_punto, pts[-1]) < math.dist(ultimo_punto, pts[0]):
                pts = pts[::-1]
            if math.dist(tramo[-1], pts[0]) < 1e-5:
                tramo.extend(pts[1:])
            else:
                tramo.extend(pts)
        else:
            tramo.extend(pts)
    lats.extend(lat for lat, _ in tramo)
    lons.extend(lon for _, lon in tramo)
    return lats, lons


def construir_mapa_conductor(inst, i: int, ruta: Ruta, filtro_trayecto: str = "ambos", zoom_actual: float | None = None, viewport=None) -> go.Figure:
    color = COLORES[i % len(COLORES)]
    (ida_lats, ida_lons), (vuelta_lats, vuelta_lons) = _geometria_ruta(inst, ruta)
    numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
    plat = [inst.coords(c)[0] for c in ruta.clientes]
    plon = [inst.coords(c)[1] for c in ruta.clientes]
    dlat, dlon = inst.coords(inst.depot)
    mostrar_ida = filtro_trayecto in {"ida", "ambos"}
    mostrar_vuelta = filtro_trayecto in {"vuelta", "ambos"}
    centro, zoom = _encuadre(
        [lat for path in (ida_lats, vuelta_lats) for lat in path if lat is not None] + [dlat],
        [lon for path in (ida_lons, vuelta_lons) for lon in path if lon is not None] + [dlon], *_tamano_mapa(viewport, conductor=True))
    zoom_flechas = zoom if zoom_actual is None else zoom_actual
    paths = [(ida_lats, ida_lons) if mostrar_ida else ([], []),
             (vuelta_lats, vuelta_lons) if mostrar_vuelta else ([], [])]
    (ida_flecha_lats, ida_flecha_lons), (vuelta_flecha_lats, vuelta_flecha_lons) = route_arrows(
        paths, zoom_flechas, stops=[(dlat, dlon), *zip(plat, plon)])
    trazas_ruta = [
        go.Scattermap(lat=ida_lats, lon=ida_lons, mode="lines", line={"width": 4, "color": color}, name="Ida", visible=mostrar_ida, hoverinfo="skip"),
        go.Scattermap(lat=vuelta_lats, lon=vuelta_lons, mode="lines", line={"width": 4, "color": color}, opacity=0.65, name="Vuelta", visible=mostrar_vuelta, hoverinfo="skip"),
        go.Scattermap(lat=ida_flecha_lats, lon=ida_flecha_lons, mode="lines", fill="toself", fillcolor="white", line={"width": 1, "color": color}, visible=mostrar_ida, hoverinfo="skip"),
        go.Scattermap(lat=vuelta_flecha_lats, lon=vuelta_flecha_lons, mode="lines", fill="toself", fillcolor="#cbd5e1", line={"width": 1, "color": color}, visible=mostrar_vuelta, hoverinfo="skip"),
    ]
    fig = go.Figure([
        *trazas_ruta,
        go.Scattermap(lat=plat, lon=plon, mode="markers+text", marker={"size": 18, "color": color}, text=[str(k + 1) for k in range(len(ruta.clientes))], textfont={"color": "white", "size": 11}, customdata=[numero.get(c, "?") for c in ruta.clientes], hovertemplate="Parada %{text} · Punto %{customdata}<extra></extra>"),
        go.Scattermap(lat=[dlat], lon=[dlon], mode="markers", marker={"size": 16, "color": "#e9c46a"}, hovertemplate="Patio<extra></extra>"),
    ])
    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False, map={"style": "carto-positron", "center": centro, "zoom": zoom_flechas, "uirevision": "constante"}, autosize=True, uirevision=f"conductor-{i}-{ruta.clientes}")
    return fig


def construir_mapa(inst, sol: Solucion, filtro_trayecto: str = "ambos", viewport=None) -> go.Figure:
    fig = go.Figure()
    route_lats, route_lons = [], []
    for i, ruta in enumerate(sol.rutas):
        (ida_lats, ida_lons), (vuelta_lats, vuelta_lons) = _geometria_ruta(inst, ruta)
        route_lats.extend(lat for lat in ida_lats + vuelta_lats if lat is not None)
        route_lons.extend(lon for lon in ida_lons + vuelta_lons if lon is not None)
        color = COLORES[i % len(COLORES)]
        fig.add_traces([
            go.Scattermap(lat=ida_lats, lon=ida_lons, mode="lines", line=dict(width=3.2, color=color), name=f"Ruta {i + 1} · ida", visible=filtro_trayecto in {"ida", "ambos"}, hoverinfo="name"),
            go.Scattermap(lat=vuelta_lats, lon=vuelta_lons, mode="lines", line=dict(width=3.2, color=color), opacity=0.65, name=f"Ruta {i + 1} · vuelta", visible=filtro_trayecto in {"vuelta", "ambos"}, hoverinfo="name"),
        ])
    clat = [inst.coords(c)[0] for c in inst.clientes]
    clon = [inst.coords(c)[1] for c in inst.clientes]
    ctext = [f"Punto {i + 1}<br>{inst.demanda.get(c, 0.0):.0f} kg base" for i, c in enumerate(inst.clientes)]
    fig.add_trace(go.Scattermap(lat=clat, lon=clon, mode="markers", marker=dict(size=9, color="#1a1a1a"), name="Acumulación", text=ctext, hovertemplate="%{text}<extra></extra>"))
    if sol.no_cubiertos:
        ulat = [inst.coords(c)[0] for c in sol.no_cubiertos]
        ulon = [inst.coords(c)[1] for c in sol.no_cubiertos]
        fig.add_trace(go.Scattermap(lat=ulat, lon=ulon, mode="markers", marker=dict(size=11, color="#c1121f"), name="No cubiertos"))
    dlat, dlon = inst.coords(inst.depot)
    fig.add_trace(go.Scattermap(lat=[dlat], lon=[dlon], mode="markers", marker=dict(size=16, color="#e9c46a"), name="Patio", text=["Patio de operaciones"], hovertemplate="%{text}<extra></extra>"))
    centro, zoom = _encuadre(route_lats + clat + [dlat], route_lons + clon + [dlon], *_tamano_mapa(viewport))
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), map=dict(style="carto-positron", center=centro, zoom=zoom, uirevision="constante"), legend=dict(orientation="h", yanchor="bottom", y=0.01, x=0.01, bgcolor="rgba(255,255,255,0.85)"), autosize=True, uirevision="mapa")
    return fig


def _estado_asistente() -> str:
    falta = agente.falta_configuracion()
    if falta:
        return falta
    cfg = agente.configuracion()
    return f"Modelo {cfg['modelo']} vía {urlparse(cfg['base_url']).netloc}. Responde con los datos del escenario."


def _render_chat(historial: list[dict]) -> list:
    if not historial:
        return [html.P("Ejemplos: ¿Qué conductor va más cargado? ¿Por qué quedan puntos sin recoger?", className="kpi-sub")]
    burbujas = [html.Div(className=f"chat-msg {m['role']}", children=dcc.Markdown(m["content"]) if m["role"] == "assistant" else m["content"]) for m in historial]
    return burbujas[::-1]


def _contexto_seguro(inst, sol: Solucion, params: dict) -> str:
    """Obtiene el contexto del escenario para la IA con respaldo a prueba de fallos en instancias dinámicas."""
    try:
        return agente.contexto_escenario(inst, sol, params)
    except Exception:
        numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
        lineas = [
            f"Parámetros del escenario: {json.dumps(params, ensure_ascii=False)}",
            f"Costo total: {_fmt_cop(sol.costo_cop)} | Distancia total: {sol.distancia_total_m / 1000:.1f} km",
            f"Vehículos usados: {sol.vehiculos_usados} | Demanda cubierta: {sol.demanda_cubierta_kg:.0f} kg | No cubierta: {sol.demanda_no_cubierta_kg:.0f} kg",
        ]
        for idx, r in enumerate(sol.rutas):
            pts = [f"Punto {numero.get(c, c)}" for c in r.clientes]
            lineas.append(
                f"Conductor {idx + 1} (Ruta {idx + 1}): {len(r.clientes)} paradas ({', '.join(pts)}), "
                f"carga {r.carga_kg:.0f} kg, distancia {r.distancia_m / 1000:.2f} km, tiempo {r.tiempo_min:.1f} min."
            )
        if sol.no_cubiertos:
            pts_nc = [f"Punto {numero.get(c, c)}" for c in sol.no_cubiertos]
            lineas.append(f"Puntos sin recoger: {', '.join(pts_nc)}.")
        return "\n".join(lineas)


def _brand():
    return html.Div(className="brand", children=[
        html.Span("S", className="brand-mark", **{"aria-hidden": "true"}),
        html.Div([html.Strong("SAGI"), html.Span("Planificación de recolección")]),
    ])


def _trayecto_legend():
    return html.Div(className="route-legend", children=[
        html.Span([html.I(className="legend-line"), "Ida · recolección"]),
        html.Span([html.I(className="legend-line return"), "Regreso · última parada → patio"]),
    ])


layout_ingesta = html.Div(
    id="upload-container", style=ESTILO_UPLOAD_VISIBLE, className="onboarding",
    children=[
        html.Header(className="topbar", children=[_brand(), html.Span("Piloto · 15 km", className="badge")]),
        html.Main(className="upload-layout", children=[
            html.Section(className="upload-intro", children=[
                html.P("PLANIFICA TU OPERACIÓN", className="eyebrow"),
                html.H1(["Tus puntos. ", html.Br(), "Un recorrido claro."]),
                html.P("Organiza la recolección, compara escenarios y consulta el recorrido de cada conductor en un solo lugar.", className="lead"),
                html.Ol(className="steps", children=[
                    html.Li([html.Strong("Carga tus puntos"), html.Span("Usa un archivo CSV o Excel con las ubicaciones y la demanda.")]),
                    html.Li([html.Strong("Confirma las columnas"), html.Span("Relaciona latitud, longitud y kilos por recoger.")]),
                    html.Li([html.Strong("Explora tu plan"), html.Span("Ajusta la flota y revisa costos, cobertura y recorridos.")]),
                ]),
            ]),
            html.Section(className="upload-card", children=[
                html.Span("01 / DATOS DEL ESCENARIO", className="eyebrow"),
                html.H2("Empieza con tus puntos"),
                html.P("La primera fila debe ser el patio de operaciones.", className="muted"),
                dcc.Upload(id="upload-data", multiple=False, accept=".csv,.xlsx,.xls", className="upload-zone", children=html.Div([
                    html.Span("↑", className="upload-icon", **{"aria-hidden": "true"}),
                    html.Strong("Arrastra tu archivo aquí"),
                    html.Button("Seleccionar archivo", type="button", className="upload-action"),
                    html.Small("CSV o Excel · hasta 50 clientes · radio de 15 km"),
                ])),
                html.Details(className="file-help", children=[
                    html.Summary("¿Cómo preparo mi archivo?"),
                    html.P("Incluye latitud, longitud y demanda en kg. El patio va primero; todos los clientes deben estar a 15 km o menos del patio en línea recta."),
                    html.Pre("latitud,longitud,demanda\n4.7413,-74.0837,0\n4.7423,-74.0837,120"),
                ]),
                dcc.Loading(id="loading-mapeo-llm", type="dot", children=[
                    html.Div(id="mapping-ui-container"),
                    html.Div(id="upload-error-msg", className="upload-error", role="alert"),
                ]),
            ]),
        ]),
        html.Footer("SAGI · Decisiones claras para una operación mejor conectada.", className="site-footer"),
    ],
)

layout_dashboard = html.Div(
    id="dashboard-container", style=ESTILO_OCULTO, className="shell",
    children=[
        html.Header(className="topbar", children=[
            _brand(),
            html.Nav([html.A("Resumen", href="#resumen"), html.A("Conductores", href="#rutas"), html.A("Asistente", href="#asistente")], **{"aria-label": "Secciones del plan"}),
            html.Button("Cargar otro archivo", id="nuevo-escenario", className="button-secondary", n_clicks=0),
        ]),
        html.Div(className="page-heading", children=[
            html.Div([html.P("CENTRO DE OPERACIONES", className="eyebrow"), html.H1("Plan de recolección"),
                      html.P("Ajusta tu escenario y revisa cómo cambia la operación.", className="muted")]),
            html.Span("Piloto · 15 km / 50 clientes", className="badge"),
        ]),
        html.Div(className="grid", children=[
            html.Aside(className="panel controls", children=[
                html.Div([html.H2("Configura tu escenario"), html.P("Los resultados se actualizan al soltar cada control.", className="kpi-sub")], className="panel-heading"),
                html.H3("Flota y operación", className="section-label"),
                        html.Label("Capacidad del vehículo (kg)"),
                        dcc.Slider(id="capacidad", min=400, max=4000, step=100, value=1200, marks={400: "400", 1200: "1.200", 2500: "2.500", 4000: "4.000"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Número máximo de vehículos"),
                        dcc.Slider(id="vehiculos", min=1, max=12, step=1, value=6, marks={1: "1", 6: "6", 12: "12"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Multiplicador de demanda"),
                        dcc.Slider(id="demanda", min=0.5, max=2.5, step=0.1, value=1.0, marks={0.5: "0.5×", 1: "1×", 1.5: "1.5×", 2.5: "2.5×"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Ventana de jornada (horas)"),
                        dcc.Slider(id="jornada", min=4, max=12, step=0.5, value=8, marks={4: "4 h", 8: "8 h", 12: "12 h"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Velocidad promedio (km/h)"),
                        dcc.Slider(id="velocidad", min=12, max=40, step=1, value=22, marks={12: "12", 22: "22", 40: "40"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Tiempo de servicio por punto (min)"),
                        dcc.Slider(id="servicio", min=3, max=20, step=1, value=SERVICIO_BASE_MIN, marks={3: "3", 8: "8", 20: "20"}, tooltip={"placement": "bottom", "always_visible": False}),

                html.Details(className="cost-settings", children=[html.Summary("Costos y penalizaciones"),
                        html.Label("Costo operativo (COP / km)"),
                        dcc.Slider(id="costo", min=2000, max=9000, step=250, value=COSTO_KM_BASE, marks={2000: "2k", 4500: "4.5k", 9000: "9k"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Costo fijo por vehículo (COP / día)"),
                        dcc.Slider(id="costo_fijo", min=0, max=600_000, step=10_000, value=COSTO_FIJO_VEHICULO_BASE, marks={0: "0", 150_000: "150k", 600_000: "600k"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Penalización por kg no recogido (COP / kg)"),
                        dcc.Slider(id="penalizacion", min=0, max=3000, step=100, value=PENALIZACION_KG_BASE, marks={0: "0", 500: "500", 3000: "3k"}, tooltip={"placement": "bottom", "always_visible": False}),

                ]),
            ]),
            html.Main(id="resumen", children=[
                html.Div(id="kpis", className="kpis", **{"aria-live": "polite"}),
                html.Div(id="estado", className="estado", role="status"),
                html.Section(className="mapa-wrap", children=[
                    html.Div(className="map-heading", children=[
                        html.Div([html.H2("Vista general de rutas"), html.P("Explora el detalle de cada recorrido en Conductores.", className="kpi-sub")]),
                        dcc.RadioItems(id="filtro-trayecto", options=[{"label": "Solo ida", "value": "ida"}, {"label": "Solo regreso", "value": "vuelta"}, {"label": "Todo el recorrido", "value": "ambos"}], value="ambos", inline=True, className="segmented"),
                    ]),
                    dcc.Loading(dcc.Graph(id="mapa", config={"displayModeBar": False, "scrollZoom": True}, responsive=True, className="main-map"), type="dot"),
                    _trayecto_legend(),
                ]),
                html.Section(id="rutas", className="conductores", children=[html.H2("Conductores y recorridos"), html.P(f"Abre una ruta para ver paradas, horarios y sentido de circulación. Salida {_fmt_hora(0)}.", className="kpi-sub"), html.Div(id="conductores")]),
                html.Section(id="asistente", className="asistente panel", children=[
                    html.H2("Asistente de rutas"),
                    html.P("Consulta cargas, horarios o puntos pendientes de tu escenario.", className="kpi-sub"),
                    html.Details(className="assistant-info", children=[html.Summary("Disponibilidad del asistente"), html.P(_estado_asistente(), className="kpi-sub")]),
                    dcc.Store(id="chat-historial", data=[], storage_type="session"),
                    dcc.Loading(html.Div(_render_chat([]), id="chat-mensajes", className="chat-mensajes"), type="dot", overlay_style={"visibility": "visible", "opacity": 0.6}),
                    html.Div(className="chat-entrada", children=[
                        html.Label("Tu pregunta", htmlFor="chat-pregunta", className="sr-only"),
                        dcc.Input(id="chat-pregunta", type="text", placeholder="Ej.: ¿Qué ruta lleva más carga?", className="chat-input", n_submit=0),
                        html.Button("Preguntar", id="chat-enviar", n_clicks=0),
                        html.Button("Limpiar", id="chat-limpiar", n_clicks=0, className="secundario"),
                    ]),
                ]),
            ]),
        ]),
    ],
)

# Store de sesión en la raíz con pantalla de carga inmersiva durante la construcción del grafo
app.layout = html.Div([
    dcc.Location(id="url_redirect", refresh=True),
    dcc.Store(id="viewport"),
    dcc.Loading(
        id="loading-instancia",
        type="cube",
        fullscreen=True,
        children=[dcc.Store(id="store-instancia", storage_type="session")],
    ),
    layout_ingesta,
    layout_dashboard,
])

app.clientside_callback(
    "function(path) { return {width: window.innerWidth, height: window.innerHeight}; }",
    Output("viewport", "data"), Input("url_redirect", "pathname"),
)


@app.callback(
    Output("store-instancia", "data", allow_duplicate=True),
    Output("upload-data", "contents"),
    Input("nuevo-escenario", "n_clicks"),
    prevent_initial_call=True,
)
def nuevo_escenario(n_clicks):
    return (None, None) if n_clicks else (no_update, no_update)


SLIDERS = ("capacidad", "vehiculos", "demanda", "jornada", "velocidad", "servicio", "costo", "costo_fijo", "penalizacion")

# Cachés en memoria para respuesta instantánea en archivos repetidos y deslizamiento de sliders
CACHE_INSTANCIAS: dict[str, object] = {}
CACHE_VRP: dict[tuple, Solucion] = {}


def resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion) -> Solucion:
    session_id = getattr(inst, "_session_id", id(inst))
    cache_key = (session_id, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)

    if cache_key in CACHE_VRP:
        return CACHE_VRP[cache_key]

    sol = resolver_vrp(
        inst, capacidad_kg=float(capacidad), max_vehiculos=int(vehiculos),
        demanda_mult=float(demanda), jornada_h=float(jornada), velocidad_kmh=float(velocidad),
        servicio_min=float(servicio), costo_km=float(costo), costo_fijo_vehiculo=float(costo_fijo),
        penalizacion_kg=float(penalizacion),
    )

    if len(CACHE_VRP) > 128:
        CACHE_VRP.pop(next(iter(CACHE_VRP)))

    CACHE_VRP[cache_key] = sol
    return sol


@app.callback(
    Output("mapping-ui-container", "children"),
    Output("upload-error-msg", "children"),
    Input("upload-data", "contents"),
    State("upload-data", "filename"),
    prevent_initial_call=True
)
def procesar_archivo_llm(contents, filename):
    if not contents:
        return None, ""

    tipo_contenido, string_contenido = contents.split(",")
    decodificado = base64.b64decode(string_contenido)

    try:
        if "csv" in filename.lower():
            df = pd.read_csv(io.StringIO(decodificado.decode("utf-8")))
        elif "xls" in filename.lower():
            df = pd.read_excel(io.BytesIO(decodificado))
        else:
            return None, "Formato no soportado. Sube un CSV o Excel."
    except Exception as e:
        return None, f"Error al leer el archivo: {str(e)}"

    prompt_mapeo = (
        "Analiza las siguientes cabeceras y las 3 primeras filas de datos de este dataset geográfico. "
        "Devuelve estrictamente un objeto JSON con esta estructura de mapeo: "
        '{"id_punto": "nombre_columna", "latitud": "nombre_columna", "longitud": "nombre_columna", "demanda": "nombre_columna"}. '
        "No incluyas explicaciones adicionales, solo el JSON.\n\nDatos de muestra:\n"
        f"{df.head(3).to_csv(index=False)}"
    )

    columnas = df.columns.tolist()
    normalizadas = {str(col).strip().casefold(): col for col in columnas}
    mapeo_sugerido = {campo: normalizadas.get(campo) for campo in ("id_punto", "latitud", "longitud", "demanda")}
    aviso = ""
    if not (mapeo_sugerido["latitud"] and mapeo_sugerido["longitud"]):
        try:
            respuesta_llm = agente.preguntar([{"role": "user", "content": prompt_mapeo}], "")
            texto_json = respuesta_llm.replace("```json", "").replace("```", "").strip()
            sugerencia = json.loads(texto_json)
            if not isinstance(sugerencia, dict):
                raise ValueError("Mapeo inválido")
            mapeo_sugerido = {campo: col if col in columnas else None for campo, col in sugerencia.items()}
        except Exception:
            aviso = "Selecciona las columnas de tu archivo para continuar."

    ui_mapeo = html.Div([
        html.P(f"Archivo: {filename} · {len(df)} filas", className="file-selected"),
        html.H3("Confirma tus columnas"),
        html.P("Latitud y longitud son obligatorias. ID y demanda son opcionales.", className="kpi-sub"),
        html.Div([html.Label("ID del punto (opcional)", htmlFor="drop-id"), dcc.Dropdown(id="drop-id", options=columnas, value=mapeo_sugerido.get("id_punto"))]),
        html.Div([html.Label("Latitud", htmlFor="drop-lat"), dcc.Dropdown(id="drop-lat", options=columnas, value=mapeo_sugerido.get("latitud"))]),
        html.Div([html.Label("Longitud", htmlFor="drop-lon"), dcc.Dropdown(id="drop-lon", options=columnas, value=mapeo_sugerido.get("longitud"))]),
        html.Div([html.Label("Demanda en kg (opcional)", htmlFor="drop-dem"), dcc.Dropdown(id="drop-dem", options=columnas, value=mapeo_sugerido.get("demanda"))]),
        html.Button("Generar plan de recolección →", id="btn-confirmar", className="button-primary"),
    ], className="mapping-form")
    return ui_mapeo, aviso


@app.callback(
    Output("store-instancia", "data"),
    Output("upload-container", "style", allow_duplicate=True),
    Output("dashboard-container", "style", allow_duplicate=True),
    Output("upload-error-msg", "children", allow_duplicate=True),
    Input("btn-confirmar", "n_clicks"),
    State("upload-data", "contents"),
    State("upload-data", "filename"),
    State("drop-id", "value"),
    State("drop-lat", "value"),
    State("drop-lon", "value"),
    State("drop-dem", "value"),
    prevent_initial_call=True
)
def confirmar_y_generar_instancia(n_clicks, contents, filename, col_id, col_lat, col_lon, col_dem):
    if not n_clicks or not contents:
        return no_update, no_update, no_update, no_update
    if not all([col_lat, col_lon]):
        return no_update, no_update, no_update, "Error: Es obligatorio definir las columnas de Latitud y Longitud."

    tipo_contenido, string_contenido = contents.split(",")
    decodificado = base64.b64decode(string_contenido)

    if "csv" in filename.lower():
        df = pd.read_csv(io.StringIO(decodificado.decode("utf-8")))
    else:
        df = pd.read_excel(io.BytesIO(decodificado))

    try:
        mapeo_cols = {col_lat: "latitud", col_lon: "longitud"}
        if col_id:
            mapeo_cols[col_id] = "id_punto"
        if col_dem:
            mapeo_cols[col_dem] = "demanda"

        df_renamed = df.rename(columns=mapeo_cols)
        df_renamed["latitud"] = pd.to_numeric(df_renamed["latitud"])
        df_renamed["longitud"] = pd.to_numeric(df_renamed["longitud"])
        if "demanda" in df_renamed.columns:
            df_renamed["demanda"] = pd.to_numeric(df_renamed["demanda"]).fillna(0.0)
        else:
            df_renamed["demanda"] = 0.0
    except (ValueError, KeyError):
        return no_update, no_update, no_update, "Fallo Crítico: Las columnas seleccionadas no existen o no son numéricas flotantes."

    # Hash único del dataset para evitar redescargar el grafo OSM si ya está en memoria
    hash_archivo = hashlib.sha256(
        decodificado + f"|{col_id}|{col_lat}|{col_lon}|{col_dem}".encode("utf-8")
    ).hexdigest()

    if hash_archivo in CACHE_INSTANCIAS:
        instancia_cliente = CACHE_INSTANCIAS[hash_archivo]
    else:
        try:
            instancia_cliente = cargar_instancia(df_renamed)
        except ValueError as e:
            return no_update, no_update, no_update, f"Alerta de Geometría: {str(e)}"
        except RuntimeError as e:
            return no_update, no_update, no_update, f"Alerta de Red: {str(e)}"
        except Exception as e:
            return no_update, no_update, no_update, f"Error Interno Inesperado: {str(e)}"

        instancia_cliente._session_id = hash_archivo
        if len(CACHE_INSTANCIAS) >= 1:
            CACHE_INSTANCIAS.pop(next(iter(CACHE_INSTANCIAS)))
        CACHE_INSTANCIAS[hash_archivo] = instancia_cliente

    return Serverside(instancia_cliente), ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE, ""


@app.callback(
    Output({"type": "mapa-conductor-graph", "index": MATCH}, "figure"),
    Input({"type": "conductor", "index": MATCH}, "open"),
    Input({"type": "filtro-conductor", "index": MATCH}, "value"),
    Input({"type": "mapa-conductor-graph", "index": MATCH}, "relayoutData"),
    State("store-instancia", "data"),
    State("capacidad", "value"),
    State("vehiculos", "value"),
    State("demanda", "value"),
    State("jornada", "value"),
    State("velocidad", "value"),
    State("servicio", "value"),
    State("costo", "value"),
    State("costo_fijo", "value"),
    State("penalizacion", "value"),
    State("viewport", "data"),
    prevent_initial_call=True
)
def mapa_conductor(
    abierto, filtro_conductor, relayout, inst,
    capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion, viewport=None
):
    if not abierto or inst is None:
        return no_update
    if isinstance(ctx.triggered_id, dict) and ctx.triggered_id.get("type") == "mapa-conductor-graph" and "map.zoom" not in (relayout or {}):
        return no_update

    sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
    i = ctx.triggered_id["index"] if isinstance(ctx.triggered_id, dict) else ctx.outputs_list["id"]["index"]
    if i >= len(sol.rutas):
        return no_update
    try:
        return construir_mapa_conductor(inst, i, sol.rutas[i], filtro_conductor, (relayout or {}).get("map.zoom"), viewport)
    except RuntimeError as exc:
        CACHE_INSTANCIAS.pop(getattr(inst, "_session_id", None), None)
        figura = go.Figure()
        figura.add_annotation(text=str(exc), showarrow=False, x=0.5, y=0.5, xref="paper", yref="paper")
        return figura


@app.callback(
    Output("mapa", "figure"),
    Output("kpis", "children"),
    Output("estado", "children"),
    Output("conductores", "children"),
    Output("upload-container", "style"),
    Output("dashboard-container", "style"),
    Input("store-instancia", "data"),
    Input("capacidad", "value"),
    Input("vehiculos", "value"),
    Input("demanda", "value"),
    Input("jornada", "value"),
    Input("velocidad", "value"),
    Input("servicio", "value"),
    Input("costo", "value"),
    Input("costo_fijo", "value"),
    Input("penalizacion", "value"),
    Input("filtro-trayecto", "value"),
    State({"type": "conductor", "index": ALL}, "open"),
    State({"type": "conductor", "index": ALL}, "id"),
    State("viewport", "data"),
)
def actualizar(
    inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo,
    penalizacion, filtro_trayecto, paneles_abiertos, paneles_ids, viewport=None,
):
    try:
        # Si no hay sesión activa o el caché expiró, mantiene visible la pantalla de carga sin bucles de recarga
        if inst is None:
            return no_update, no_update, no_update, no_update, ESTILO_UPLOAD_VISIBLE, ESTILO_OCULTO

        if ctx.triggered_id == "filtro-trayecto":
            sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
            return construir_mapa(inst, sol, filtro_trayecto, viewport), no_update, no_update, no_update, ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE

        sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
        abiertos = {pid["index"] for pid, abierto in zip(paneles_ids or [], paneles_abiertos or []) if abierto}
        kpis = [
            _kpi("Costo total", _fmt_cop(sol.costo_cop), f"km {_fmt_cop(sol.costo_variable_cop)} · flota {_fmt_cop(sol.costo_fijo_cop)} · penalización {_fmt_cop(sol.penalizacion_cop)}"),
            _kpi("Distancia", f"{sol.distancia_total_m / 1000:.1f} km", "Recorrido total por calles"),
            _kpi("Horas-vehículo", f"{sol.tiempo_total_min / 60:.1f} h", f"ruta más larga {sol.tiempo_max_ruta_min / 60:.1f} h"),
            _kpi("Vehículos", f"{sol.vehiculos_usados} / {int(vehiculos)}", f"{sol.demanda_cubierta_kg:.0f} kg cubiertos"),
            _kpi("No recogido", f"{sol.demanda_no_cubierta_kg:.0f} kg", f"{len(sol.no_cubiertos)} punto(s)"),
        ]
        if sol.factible:
            estado = html.P("Escenario factible: toda la demanda cabe en capacidad, flota y jornada.", className="ok")
        else:
            n = len(sol.no_cubiertos)
            estado = html.P(f"Escenario tensionado: {n} punto(s) quedan fuera. Sube capacidad, flota o jornada, o baja la demanda.", className="warn")

        conductores = construir_conductores(
            inst, sol, abiertos, capacidad=float(capacidad), jornada_h=float(jornada),
            velocidad=float(velocidad), servicio=float(servicio), demanda=float(demanda),
            costo_km=float(costo), costo_fijo=float(costo_fijo),
        )
        return construir_mapa(inst, sol, filtro_trayecto, viewport), kpis, estado, conductores, ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE
    except RuntimeError as exc:
        CACHE_INSTANCIAS.pop(getattr(inst, "_session_id", None), None)
        return (no_update, no_update, html.P(str(exc), className="warn"), no_update,
                ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE)


@app.callback(
    Output("chat-historial", "data"),
    Output("chat-mensajes", "children"),
    Output("chat-pregunta", "value"),
    Input("chat-enviar", "n_clicks"),
    Input("chat-pregunta", "n_submit"),
    Input("chat-limpiar", "n_clicks"),
    State("chat-pregunta", "value"),
    State("chat-historial", "data"),
    State("store-instancia", "data"),
    State("capacidad", "value"),
    State("vehiculos", "value"),
    State("demanda", "value"),
    State("jornada", "value"),
    State("velocidad", "value"),
    State("servicio", "value"),
    State("costo", "value"),
    State("costo_fijo", "value"),
    State("penalizacion", "value"),
    prevent_initial_call=True,
)
def chat(
    _enviar, _enter, _limpiar, pregunta, historial, inst,
    capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion
):
    if ctx.triggered_id == "chat-limpiar":
        return [], _render_chat([]), ""

    historial = list(historial or [])
    pregunta = (pregunta or "").strip()
    if not pregunta:
        return no_update, no_update, no_update

    if inst is None:
        historial.append({"role": "error", "content": "La sesión del mapa expiró. Recarga la página y vuelve a subir el archivo."})
        return historial, _render_chat(historial), ""

    historial.append({"role": "user", "content": pregunta})
    falta = agente.falta_configuracion()
    if falta:
        historial.append({"role": "error", "content": falta})
    else:
        valores = (capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
        sol = resolver(inst, *valores)
        params = dict(zip(SLIDERS, map(float, valores)))
        try:
            contexto = _contexto_seguro(inst, sol, params)
            respuesta = agente.preguntar(historial, contexto)
            historial.append({"role": "assistant", "content": respuesta})
        except Exception as e:
            historial.append({"role": "error", "content": f"Fallo en el asistente: {str(e)}"})

    return historial, _render_chat(historial), ""


if __name__ == "__main__":
    app.run(debug=False, host="0.0.0.0", port=int(os.environ.get("PORT", 8051)))
    