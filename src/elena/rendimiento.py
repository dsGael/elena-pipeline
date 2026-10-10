"""Estimación del rendimiento (km/L) a partir de ventanas ya consolidadas.

Sirve para obtener un ``rendimiento_default`` con datos reales del vehículo. Es PURO (recibe
puntos, devuelve números); el comando que lee fuel_windows vive en cli_fuel.py.

Por qué NO se compara par de ventanas contra par de ventanas
------------------------------------------------------------
Entre dos ventanas de 5 min el vehículo gasta ~0.1 L, mucho menos que el ruido del sensor
(~0.5-1 L). Sumar esas diferencias sale sesgado en cuanto se descartan los pares que "suben"
(posibles cargas) y se conservan los que "bajan": el ruido hacia abajo se acumula y el
rendimiento sale mucho más bajo que el real (con ruido de 0.5 L, 12 km/L salía como 5.5).

Método por tramos (regresión)
-----------------------------
1. Se parte la serie en tramos continuos: se corta con un hueco mayor a ``max_hueco_minutos``
   o si el odómetro falta o retrocede.
2. Dentro de cada tramo se buscan saltos de nivel (cargas o extracciones) sobre una versión
   suavizada (mediana móvil de 5 puntos): un cambio >= ``salto_litros`` corta el tramo, y se
   recortan ``recorte`` puntos alrededor del corte (donde la mediana aún está mezclada).
3. Cada subtramo con al menos ``min_puntos`` puntos y ``min_km_tramo`` km recorridos aporta una
   recta litros-vs-km por mínimos cuadrados. El ruido del sensor se promedia en todos los
   puntos del tramo en lugar de pesar en cada par. Pendiente >= 0 (el nivel no baja) se descarta.
4. rendimiento = suma de km de los tramos / suma de litros que la recta explica.

Solo es confiable con suficiente consumo acumulado (``min_litros``); si no, es None.
Los parámetros de umbral son de ESTA herramienta (no están en el documento).
"""

from __future__ import annotations

import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise

M_HUECO = "corte_por_hueco_u_odometro"
M_SALTO = "corte_por_salto_de_nivel"
M_POCOS_PUNTOS = "tramo_con_pocos_puntos"
M_POCOS_KM = "tramo_con_pocos_km"
M_SIN_CONSUMO = "tramo_sin_consumo_visible"


@dataclass(frozen=True, slots=True)
class PuntoRendimiento:
    """Ventana válida con litros ya calculados por calibración."""

    ventana_inicio: datetime
    litros: float
    odometro_total: float | None  # en las unidades de gps_records (metros)


@dataclass(frozen=True, slots=True)
class EstimacionRendimiento:
    km: float  # recorrido de los tramos usados
    litros: float  # consumo que explican esos tramos
    puntos: int  # ventanas recibidas
    tramos_usados: int
    puntos_usados: int
    descartes: Counter[str] = field(default_factory=Counter)  # motivo -> cantidad
    rendimiento_km_l: float | None = None  # None si no hay suficiente consumo acumulado


def _mediana_movil(valores: Sequence[float], k: int = 5) -> list[float]:
    h = k // 2
    return [
        statistics.median(valores[max(0, i - h) : i + h + 1])
        for i in range(len(valores))
    ]


def _pendiente(xs: Sequence[float], ys: Sequence[float]) -> float:
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    sxx = sum((x - mx) ** 2 for x in xs)
    if sxx == 0:
        return 0.0
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)) / sxx


def estimar_rendimiento(
    puntos: Sequence[PuntoRendimiento],
    *,
    odometro_a_km: float = 0.001,
    salto_litros: float = 2.0,
    max_hueco_minutos: float = 15.0,
    min_puntos: int = 6,
    min_km_tramo: float = 10.0,
    recorte: int = 2,
    min_litros: float = 10.0,
) -> EstimacionRendimiento:
    ordenados = sorted(puntos, key=lambda p: p.ventana_inicio)
    descartes: Counter[str] = Counter()
    hueco_max = timedelta(minutes=max_hueco_minutos)

    # 1. tramos continuos
    tramos: list[list[PuntoRendimiento]] = []
    actual: list[PuntoRendimiento] = []
    for p in ordenados:
        if p.odometro_total is None:  # sin odómetro no se puede medir recorrido
            descartes[M_HUECO] += 1
            if actual:
                tramos.append(actual)
                actual = []
            continue
        if actual:
            ant = actual[-1]
            if p.ventana_inicio - ant.ventana_inicio > hueco_max or p.odometro_total < (
                ant.odometro_total or 0.0
            ):
                descartes[M_HUECO] += 1
                tramos.append(actual)
                actual = []
        actual.append(p)
    if actual:
        tramos.append(actual)

    km_total = litros_total = 0.0
    usados = puntos_usados = 0
    for tramo in tramos:
        # 2. saltos de nivel sobre la serie suavizada
        suave = _mediana_movil([p.litros for p in tramo])
        cortes = [0]
        for i, (a, b) in enumerate(pairwise(suave)):
            if abs(b - a) >= salto_litros:
                cortes.append(i + 1)
        cortes.append(len(tramo))
        hubo_corte = len(cortes) > 2
        if hubo_corte:
            descartes[M_SALTO] += len(cortes) - 2
        for a, b in pairwise(cortes):
            ini = a + (recorte if a > 0 else 0)
            fin = b - (recorte if b < len(tramo) else 0)
            sub = tramo[ini:fin]
            # 3. recta litros-vs-km por mínimos cuadrados
            if len(sub) < min_puntos:
                descartes[M_POCOS_PUNTOS] += 1
                continue
            xs = [(p.odometro_total or 0.0) * odometro_a_km for p in sub]
            ys = [p.litros for p in sub]
            recorrido = xs[-1] - xs[0]
            if recorrido < min_km_tramo:
                descartes[M_POCOS_KM] += 1
                continue
            m = _pendiente(xs, ys)
            if m >= 0:
                descartes[M_SIN_CONSUMO] += 1
                continue
            km_total += recorrido
            litros_total += -m * recorrido
            usados += 1
            puntos_usados += len(sub)

    km_total = round(km_total, 2)
    litros_total = round(litros_total, 2)
    rend = round(km_total / litros_total, 2) if litros_total >= min_litros else None
    return EstimacionRendimiento(
        km=km_total,
        litros=litros_total,
        puntos=len(ordenados),
        tramos_usados=usados,
        puntos_usados=puntos_usados,
        descartes=descartes,
        rendimiento_km_l=rend,
    )