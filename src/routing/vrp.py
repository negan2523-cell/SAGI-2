"""Heurística CVRP con límite de jornada: Clarke-Wright + 2-opt + reinserción.

Cada ruta sale y vuelve al patio, respeta la capacidad del vehículo y su duración
(viaje + servicio) no puede superar la jornada. No hay ventanas horarias por punto.
"""

from __future__ import annotations

from dataclasses import dataclass, field

from src.graph.osm_network import InstanciaSuba


@dataclass
class Ruta:
    clientes: list[int]
    carga_kg: float
    distancia_m: float
    tiempo_min: float
    factible: bool = True


@dataclass
class Solucion:
    rutas: list[Ruta]
    distancia_total_m: float
    tiempo_total_min: float
    tiempo_max_ruta_min: float
    vehiculos_usados: int
    demanda_total_kg: float
    demanda_cubierta_kg: float
    costo_variable_cop: float
    costo_fijo_cop: float
    penalizacion_cop: float
    factible: bool
    no_cubiertos: list[int] = field(default_factory=list)

    @property
    def demanda_no_cubierta_kg(self) -> float:
        return self.demanda_total_kg - self.demanda_cubierta_kg

    @property
    def costo_cop(self) -> float:
        return self.costo_variable_cop + self.costo_fijo_cop + self.penalizacion_cop


@dataclass
class Parada:
    cliente: int
    llegada_min: float
    tramo_m: float
    carga_acum_kg: float


def itinerario(
    inst: InstanciaSuba,
    ruta: Ruta,
    *,
    velocidad_kmh: float,
    servicio_min: float,
    demanda_mult: float,
) -> tuple[list[Parada], float, float]:
    """Paradas con minutos desde la salida; devuelve también el tramo y minuto de regreso."""
    velocidad_m_min = (velocidad_kmh * 1000.0) / 60.0
    idx = inst.indice
    paradas: list[Parada] = []
    t = 0.0
    carga = 0.0
    previo = inst.depot
    for c in ruta.clientes:
        tramo = inst.matriz_m[idx[previo]][idx[c]]
        t += tramo / velocidad_m_min
        carga += inst.demanda[c] * demanda_mult
        paradas.append(Parada(c, t, tramo, carga))
        t += servicio_min
        previo = c
    regreso = inst.matriz_m[idx[previo]][idx[inst.depot]]
    return paradas, regreso, t + regreso / velocidad_m_min


@dataclass
class _Parametros:
    inst: InstanciaSuba
    capacidad_kg: float
    jornada_min: float
    velocidad_m_min: float
    servicio_min: float
    demanda_mult: float

    def dist(self, a: int, b: int) -> float:
        idx = self.inst.indice
        return self.inst.matriz_m[idx[a]][idx[b]]

    def demanda(self, c: int) -> float:
        return self.inst.demanda[c] * self.demanda_mult

    def carga(self, clientes: list[int]) -> float:
        return sum(self.demanda(c) for c in clientes)

    def distancia(self, clientes: list[int]) -> float:
        seq = [self.inst.depot, *clientes, self.inst.depot]
        return sum(self.dist(a, b) for a, b in zip(seq, seq[1:]))

    def tiempo(self, clientes: list[int], distancia: float | None = None) -> float:
        if distancia is None:
            distancia = self.distancia(clientes)
        return distancia / self.velocidad_m_min + self.servicio_min * len(clientes)

    def es_factible(self, clientes: list[int]) -> bool:
        return (
            self.carga(clientes) <= self.capacidad_kg + 1e-6
            and self.tiempo(clientes) <= self.jornada_min + 1e-6
        )

    def metricas(self, clientes: list[int]) -> Ruta:
        dist = self.distancia(clientes)
        tiempo = self.tiempo(clientes, dist)
        return Ruta(
            clientes=list(clientes),
            carga_kg=self.carga(clientes),
            distancia_m=dist,
            tiempo_min=tiempo,
            factible=tiempo <= self.jornada_min + 1e-6,
        )


def _clarke_wright(
    p: _Parametros, viables: list[int], dirigido: bool
) -> list[list[int]]:
    """Ahorros de Clarke-Wright.

    `dirigido`: pares ordenados, solo se encadena ra + rb y solo con ahorro positivo
    (respeta la asimetría de la red). Si no, variante clásica: pares no ordenados,
    se permiten rutas invertidas y se fusiona mientras sea factible (consolida flota).
    La factibilidad siempre se evalúa con distancias reales.
    """
    depot = p.inst.depot
    if dirigido:
        pares = [(a, b) for a in viables for b in viables if a != b]
    else:
        pares = [(a, b) for i, a in enumerate(viables) for b in viables[i + 1 :]]
    ahorros = sorted(
        ((p.dist(a, depot) + p.dist(depot, b) - p.dist(a, b), a, b) for a, b in pares),
        reverse=True,
    )

    ruta_de: dict[int, list[int]] = {c: [c] for c in viables}
    for s, a, b in ahorros:
        ra, rb = ruta_de[a], ruta_de[b]
        if ra is rb:
            continue
        fusion: list[int] | None = None
        if dirigido:
            if s > 0 and ra[-1] == a and rb[0] == b:
                fusion = ra + rb
        elif ra[-1] == a and rb[0] == b:
            fusion = ra + rb
        elif rb[-1] == b and ra[0] == a:
            fusion = rb + ra
        elif ra[-1] == a and rb[-1] == b:
            fusion = ra + rb[::-1]
        elif ra[0] == a and rb[0] == b:
            fusion = ra[::-1] + rb
        if fusion is None or not p.es_factible(fusion):
            continue
        for c in fusion:
            ruta_de[c] = fusion

    unicas: dict[int, list[int]] = {id(r): r for r in ruta_de.values()}
    return list(unicas.values())


def _dos_opt(p: _Parametros, ruta: list[int]) -> list[int]:
    """2-opt con recálculo completo (la matriz es asimétrica). Menos distancia ⇒ menos tiempo."""
    mejor = list(ruta)
    mejor_dist = p.distancia(mejor)
    mejoro = True
    while mejoro:
        mejoro = False
        for i in range(len(mejor) - 1):
            for j in range(i + 1, len(mejor)):
                cand = mejor[:i] + mejor[i : j + 1][::-1] + mejor[j + 1 :]
                d = p.distancia(cand)
                if d < mejor_dist - 1e-6:
                    mejor, mejor_dist, mejoro = cand, d, True
    return mejor


def _reinsertar(
    p: _Parametros, rutas: list[list[int]], pendientes: list[int]
) -> list[int]:
    """Inserción más barata de los puntos sobrantes en las rutas ya asignadas."""
    sin_lugar: list[int] = []
    for c in sorted(pendientes, key=p.demanda, reverse=True):
        mejor: tuple[float, int, int] | None = None
        for ri, r in enumerate(rutas):
            if p.carga(r) + p.demanda(c) > p.capacidad_kg + 1e-6:
                continue
            base = p.distancia(r)
            for pos in range(len(r) + 1):
                cand = r[:pos] + [c] + r[pos:]
                if p.tiempo(cand) > p.jornada_min + 1e-6:
                    continue
                delta = p.distancia(cand) - base
                if mejor is None or delta < mejor[0]:
                    mejor = (delta, ri, pos)
        if mejor is None:
            sin_lugar.append(c)
        else:
            _, ri, pos = mejor
            rutas[ri] = rutas[ri][:pos] + [c] + rutas[ri][pos:]
    return sin_lugar


def _construir(
    p: _Parametros, viables: list[int], max_v: int, dirigido: bool
) -> tuple[list[list[int]], list[int]]:
    rutas = [_dos_opt(p, r) for r in _clarke_wright(p, viables, dirigido)]
    if len(rutas) <= max_v:
        return rutas, []
    rutas.sort(key=p.carga, reverse=True)
    sobrantes = [c for r in rutas[max_v:] for c in r]
    rutas = rutas[:max_v]
    sin_lugar = _reinsertar(p, rutas, sobrantes)
    return [_dos_opt(p, r) for r in rutas], sin_lugar


def resolver_vrp(
    inst: InstanciaSuba,
    *,
    capacidad_kg: float,
    max_vehiculos: int,
    demanda_mult: float,
    jornada_h: float,
    velocidad_kmh: float,
    servicio_min: float,
    costo_km: float,
    costo_fijo_vehiculo: float,
    penalizacion_kg: float,
) -> Solucion:
    p = _Parametros(
        inst=inst,
        capacidad_kg=capacidad_kg,
        jornada_min=jornada_h * 60.0,
        velocidad_m_min=(velocidad_kmh * 1000.0) / 60.0,
        servicio_min=servicio_min,
        demanda_mult=demanda_mult,
    )

    viables = [c for c in inst.clientes if p.es_factible([c])]
    no_cubiertos = [c for c in inst.clientes if c not in viables]

    max_v = max(1, int(max_vehiculos))
    candidatos = [_construir(p, viables, max_v, d) for d in (True, False)]
    rutas, sin_lugar = min(
        candidatos,
        key=lambda c: (p.carga(c[1]), sum(p.distancia(r) for r in c[0])),
    )
    no_cubiertos.extend(sin_lugar)

    detalle = [p.metricas(r) for r in rutas if r]
    dist_total = sum(r.distancia_m for r in detalle)
    demanda_total = p.carga(inst.clientes)
    no_cubierta = p.carga(no_cubiertos)
    return Solucion(
        rutas=detalle,
        distancia_total_m=dist_total,
        tiempo_total_min=sum(r.tiempo_min for r in detalle),
        tiempo_max_ruta_min=max((r.tiempo_min for r in detalle), default=0.0),
        vehiculos_usados=len(detalle),
        demanda_total_kg=demanda_total,
        demanda_cubierta_kg=demanda_total - no_cubierta,
        costo_variable_cop=(dist_total / 1000.0) * costo_km,
        costo_fijo_cop=len(detalle) * costo_fijo_vehiculo,
        penalizacion_cop=no_cubierta * penalizacion_kg,
        factible=not no_cubiertos and all(r.factible for r in detalle),
        no_cubiertos=no_cubiertos,
    )
