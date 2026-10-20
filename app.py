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

COLORES = [
    "#0B6E4F", "#E07A3D", "#2C7FB8", "#8C2F39", "#6A4C93",
    "#BC4B51", "#3D5A80", "#C9A227", "#2A9D8F", "#E76F51",
    "#264653", "#F4A261",
]

ESTILO_UPLOAD_VISIBLE = {
    "padding": "40px",
    "maxWidth": "800px",
    "margin": "0 auto",
    "textAlign": "center",
    "display": "block",
}
ESTILO_OCULTO = {"display": "none"}
ESTILO_DASHBOARD_VISIBLE = {"display": "block"}

# Configuración de almacenamiento para despliegues en la nube (Render usa /tmp efímero pero estable por instancia)
fs_store = FileSystemBackend(cache_dir="/tmp/dash_cache")
app = DashProxy(__name__, transforms=[ServersideOutputTransform(backends=[fs_store])])
app.title = "SAGI · Residuos Suba"


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
                                    {"label": "Recolección / Ida", "value": "ida"},
                                    {"label": "Regreso al patio", "value": "vuelta"},
                                    {"label": "Ambos", "value": "ambos"},
                                ],
                                value="ambos",
                                inline=True,
                            ),
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
                    html.Table(
                        className="itinerario",
                        children=[
                            html.Thead(html.Tr([html.Th("#"), html.Th("Parada"), html.Th("Llegada"), html.Th("Tramo (km)"), html.Th("Carga acum. (kg)")])),
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
        puntos = ", ".join(str(numero.get(c, c)) for c in sorted(sol.no_cubiertos, key=lambda x: numero.get(x, 0)))
        hijos.append(html.P(f"Sin conductor asignado: puntos {puntos}.", className="warn"))
    return hijos


def _encuadre(lats: list[float], lons: list[float], ancho_px: float, alto_px: float) -> tuple[dict, float]:
    lat_min, lat_max, lon_min, lon_max = min(lats), max(lats), min(lons), max(lons)
    dlat = max(lat_max - lat_min, 1e-4)
    dlon = max(lon_max - lon_min, 1e-4)
    zoom = math.log2(min(ancho_px * 360 / (256 * dlon), alto_px * 360 / (256 * dlat)))
    centro = {"lat": (lat_min + lat_max) / 2, "lon": (lon_min + lon_max) / 2}
    return centro, min(zoom - 0.35, 16.5)


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
    nodos = _nodos_ruta(inst, ruta)
    if len(nodos) < 3:
        return _geometria_nodos(inst, nodos), ([], [])
    lat_depot, lon_depot = inst.coords(nodos[0])
    escala_lon = 111_320 * math.cos(math.radians(lat_depot))
    punto_depot_m = (lat_depot * 111_320, lon_depot * escala_lon)
    punto_giro = max(
        range(1, len(nodos) - 1),
        key=lambda indice: math.dist(
            punto_depot_m,
            (inst.coords(nodos[indice])[0] * 111_320, inst.coords(nodos[indice])[1] * escala_lon),
        ),
    )
    ida = _geometria_nodos(inst, nodos[: punto_giro + 1])
    vuelta = _geometria_nodos(inst, nodos[punto_giro:])
    return ida, vuelta


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


def _flechas_ruta(lats: list[float | None], lons: list[float | None]) -> tuple[list, list]:
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
    distancias_objetivo = [distancia_total * (idx + 1) / (cantidad + 1) for idx in range(cantidad)]
    flecha_lats: list = []
    flecha_lons: list = []
    acumulada = 0.0
    objetivo_idx = 0
    for lat1, lon1, lat2, lon2, este, norte, longitud in segmentos:
        while objetivo_idx < len(distancias_objetivo) and distancias_objetivo[objetivo_idx] <= acumulada + longitud:
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
            vertices = [cola_izq, cuello_izq, hombro_izq, punta, hombro_der, cuello_der, cola_der, cola_izq]
            flecha_lats.extend(lat + norte / 111_320 for _, norte in vertices)
            flecha_lons.extend(lon + este / escala_lon for este, _ in vertices)
            flecha_lats.append(None)
            flecha_lons.append(None)
            objetivo_idx += 1
        acumulada += longitud
    return flecha_lats, flecha_lons


def construir_mapa_conductor(inst, i: int, ruta: Ruta, filtro_trayecto: str = "ambos") -> go.Figure:
    color = COLORES[i % len(COLORES)]
    (ida_lats, ida_lons), (vuelta_lats, vuelta_lons) = _geometria_ruta(inst, ruta)
    ida_flecha_lats, ida_flecha_lons = _flechas_ruta(ida_lats, ida_lons)
    vuelta_flecha_lats, vuelta_flecha_lons = _flechas_ruta(vuelta_lats, vuelta_lons)
    numero = {c: k + 1 for k, c in enumerate(inst.clientes)}
    plat = [inst.coords(c)[0] for c in ruta.clientes]
    plon = [inst.coords(c)[1] for c in ruta.clientes]
    dlat, dlon = inst.coords(inst.depot)
    mostrar_ida = filtro_trayecto in {"ida", "ambos"}
    mostrar_vuelta = filtro_trayecto in {"vuelta", "ambos"}
    trazas_ruta = [
        go.Scattermap(lat=ida_lats, lon=ida_lons, mode="lines", line={"width": 4, "color": color}, name="Ida", visible=mostrar_ida, hoverinfo="skip"),
        go.Scattermap(lat=vuelta_lats, lon=vuelta_lons, mode="lines", line={"width": 4, "color": color}, opacity=0.65, name="Vuelta", visible=mostrar_vuelta, hoverinfo="skip"),
        go.Scattermap(lat=ida_flecha_lats, lon=ida_flecha_lons, mode="lines", fill="toself", fillcolor="white", line={"width": 1, "color": "#174d3e"}, visible=mostrar_ida, hoverinfo="skip"),
        go.Scattermap(lat=vuelta_flecha_lats, lon=vuelta_flecha_lons, mode="lines", fill="toself", fillcolor="white", line={"width": 1, "color": "#174d3e"}, visible=mostrar_vuelta, hoverinfo="skip"),
    ]
    fig = go.Figure([
        *trazas_ruta,
        go.Scattermap(lat=plat, lon=plon, mode="markers+text", marker={"size": 18, "color": color}, text=[str(k + 1) for k in range(len(ruta.clientes))], textfont={"color": "white", "size": 11}, customdata=[numero.get(c, "?") for c in ruta.clientes], hovertemplate="Parada %{text} · Punto %{customdata}<extra></extra>"),
        go.Scattermap(lat=[dlat], lon=[dlon], mode="markers", marker={"size": 16, "color": "#e9c46a"}, hovertemplate="Patio<extra></extra>"),
    ])
    centro, zoom = _encuadre([lat for trayecto_lats in (ida_lats, vuelta_lats) for lat in trayecto_lats if lat is not None] + [dlat], [lon for trayecto_lons in (ida_lons, vuelta_lons) for lon in trayecto_lons if lon is not None] + [dlon], 480, 360)
    fig.update_layout(margin={"l": 0, "r": 0, "t": 0, "b": 0}, showlegend=False, map={"style": "open-street-map", "center": centro, "zoom": zoom, "uirevision": "constante"}, autosize=True, uirevision=f"conductor-{i}-{ruta.clientes}")
    return fig


def construir_mapa(inst, sol: Solucion, filtro_trayecto: str = "ambos") -> go.Figure:
    fig = go.Figure()
    for i, ruta in enumerate(sol.rutas):
        (ida_lats, ida_lons), (vuelta_lats, vuelta_lons) = _geometria_ruta(inst, ruta)
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
    centro, zoom = _encuadre(clat + [dlat], clon + [dlon], 1000, 720)
    fig.update_layout(margin=dict(l=0, r=0, t=0, b=0), map=dict(style="open-street-map", center=centro, zoom=zoom, uirevision="constante"), legend=dict(orientation="h", yanchor="bottom", y=0.01, x=0.01, bgcolor="rgba(255,255,255,0.85)"), autosize=True, uirevision="mapa")
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


layout_ingesta = html.Div(
    id="upload-container",
    style=ESTILO_UPLOAD_VISIBLE,
    children=[
        html.H1("Configuración del Escenario Base (Carga de Puntos)"),
        html.P("Piloto: primera fila = patio; hasta 50 puntos a 15 km del patio. Rutas calculadas con OSRM."),
        dcc.Upload(
            id="upload-data",
            children=html.Div(["Arrastra o haz clic para subir tu CSV / Excel con puntos de recolección"]),
            style={
                "width": "100%", "height": "80px", "lineHeight": "80px",
                "borderWidth": "2px", "borderStyle": "dashed", "borderRadius": "8px", "cursor": "pointer"
            },
            multiple=False
        ),
        dcc.Loading(
            id="loading-mapeo-llm",
            type="dot",
            children=[
                html.Div(id="mapping-ui-container", style={"marginTop": "30px"}),
                html.Div(id="upload-error-msg", style={"color": "red", "marginTop": "20px", "fontWeight": "bold"}),
            ],
        ),
    ]
)

layout_dashboard = html.Div(
    id="dashboard-container",
    style=ESTILO_OCULTO,
    className="shell",
    children=[
        html.Header(
            children=[
                html.P("Sistema de análisis georeactivo inteligente", className="eyebrow"),
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
                        html.Label("Trayecto visible"),
                        dcc.RadioItems(id="filtro-trayecto", options=[{"label": "Recolección / Ida", "value": "ida"}, {"label": "Regreso al patio", "value": "vuelta"}, {"label": "Ambos", "value": "ambos"}], value="ambos", inline=True),
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
                        html.Label("Costo operativo (COP / km)"),
                        dcc.Slider(id="costo", min=2000, max=9000, step=250, value=COSTO_KM_BASE, marks={2000: "2k", 4500: "4.5k", 9000: "9k"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Costo fijo por vehículo (COP / día)"),
                        dcc.Slider(id="costo_fijo", min=0, max=600_000, step=10_000, value=COSTO_FIJO_VEHICULO_BASE, marks={0: "0", 150_000: "150k", 600_000: "600k"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Label("Penalización por kg no recogido (COP / kg)"),
                        dcc.Slider(id="penalizacion", min=0, max=3000, step=100, value=PENALIZACION_KG_BASE, marks={0: "0", 500: "500", 3000: "3k"}, tooltip={"placement": "bottom", "always_visible": False}),
                        html.Div(id="estado", className="estado"),
                    ],
                ),
                html.Main(
                    children=[
                        html.Div(id="kpis", className="kpis"),
                        html.Div(className="mapa-wrap", children=dcc.Graph(id="mapa", config={"displayModeBar": False, "scrollZoom": True}, responsive=True, style={"height": "720px"})),
                        html.Section(className="conductores", children=[html.H2("Conductores"), html.P(f"Detalle por ruta. Salida {_fmt_hora(0)}.", className="kpi-sub"), html.Div(id="conductores")]),
                        html.Section(
                            className="asistente",
                            children=[
                                html.H2("Asistente de rutas"),
                                html.P(_estado_asistente(), className="kpi-sub"),
                                dcc.Store(id="chat-historial", data=[], storage_type="session"),
                                dcc.Loading(html.Div(_render_chat([]), id="chat-mensajes", className="chat-mensajes"), type="dot", overlay_style={"visibility": "visible", "opacity": 0.6}),
                                html.Div(
                                    className="chat-entrada",
                                    children=[
                                        dcc.Input(id="chat-pregunta", type="text", placeholder="Pregunta sobre las rutas...", className="chat-input", n_submit=0),
                                        html.Button("Preguntar", id="chat-enviar", n_clicks=0),
                                        html.Button("Limpiar", id="chat-limpiar", n_clicks=0, className="secundario"),
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

# Store de sesión en la raíz con pantalla de carga inmersiva durante la construcción del grafo
app.layout = html.Div([
    dcc.Location(id="url_redirect", refresh=True),
    dcc.Loading(
        id="loading-instancia",
        type="cube",
        fullscreen=True,
        children=[dcc.Store(id="store-instancia", storage_type="session")],
    ),
    layout_ingesta,
    layout_dashboard,
])

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
        return no_update, no_update

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
        html.H3("Confirma las columnas del archivo:"),
        html.Div([html.Label("ID Punto: "), dcc.Dropdown(id="drop-id", options=columnas, value=mapeo_sugerido.get("id_punto"))]),
        html.Div([html.Label("Latitud: "), dcc.Dropdown(id="drop-lat", options=columnas, value=mapeo_sugerido.get("latitud"))]),
        html.Div([html.Label("Longitud: "), dcc.Dropdown(id="drop-lon", options=columnas, value=mapeo_sugerido.get("longitud"))]),
        html.Div([html.Label("Demanda (kg): "), dcc.Dropdown(id="drop-dem", options=columnas, value=mapeo_sugerido.get("demanda"))]),
        html.Button("Confirmar Mapeo y Generar Mapa", id="btn-confirmar", style={"marginTop": "20px"}),
    ], style={"textAlign": "left", "maxWidth": "400px", "margin": "0 auto"})
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
    prevent_initial_call=True
)
def mapa_conductor(
    abierto, filtro_conductor, inst,
    capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion
):
    if not abierto or inst is None:
        return no_update

    sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
    i = ctx.triggered_id["index"] if isinstance(ctx.triggered_id, dict) else ctx.outputs_list["id"]["index"]
    if i >= len(sol.rutas):
        return no_update
    try:
        return construir_mapa_conductor(inst, i, sol.rutas[i], filtro_conductor)
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
)
def actualizar(
    inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo,
    penalizacion, filtro_trayecto, paneles_abiertos, paneles_ids,
):
    try:
        # Si no hay sesión activa o el caché expiró, mantiene visible la pantalla de carga sin bucles de recarga
        if inst is None:
            return no_update, no_update, no_update, no_update, ESTILO_UPLOAD_VISIBLE, ESTILO_OCULTO

        if ctx.triggered_id == "filtro-trayecto":
            sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
            return construir_mapa(inst, sol, filtro_trayecto), no_update, no_update, no_update, ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE

        sol = resolver(inst, capacidad, vehiculos, demanda, jornada, velocidad, servicio, costo, costo_fijo, penalizacion)
        abiertos = {pid["index"] for pid, abierto in zip(paneles_ids or [], paneles_abiertos or []) if abierto}
        kpis = [
            _kpi("Costo total", _fmt_cop(sol.costo_cop), f"km {_fmt_cop(sol.costo_variable_cop)} · flota {_fmt_cop(sol.costo_fijo_cop)} · penalización {_fmt_cop(sol.penalizacion_cop)}"),
            _kpi("Distancia", f"{sol.distancia_total_m / 1000:.1f} km", "rutas OSRM · datos OSM"),
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
        return construir_mapa(inst, sol, filtro_trayecto), kpis, estado, conductores, ESTILO_OCULTO, ESTILO_DASHBOARD_VISIBLE
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
    