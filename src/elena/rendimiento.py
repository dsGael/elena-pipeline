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

import math
import statistics
from collections import Counter
from collections.abc import Sequence
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from itertools import pairwise
from typing import NamedTuple

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


# --------------------------------------------------------------------------------------
# Rendimiento reciente: "¿cómo está rindiendo el autobús en sus últimos kilómetros?"
# --------------------------------------------------------------------------------------
#
# ``estimar_rendimiento`` (arriba) sirve para sacar un ``rendimiento_default`` con mucho
# histórico, pero NO para responder "cómo va ahora": exige tramos continuos largos, y en un
# autobús real eso casi nunca ocurre. Se descartan las lecturas con la ignición apagada, así que
# cada parada deja un hueco en la serie; si cada hueco corta el tramo (como arriba), los trozos
# quedan demasiado cortos y la estimación nunca llega.
#
# Este método parte de otra observación: el nivel del tanque NO cambia mientras el vehículo
# está detenido y el odómetro tampoco, así que un hueco de tiempo no interrumpe la recta
# litros-vs-km. Lo único que sí la interrumpe es una carga o una extracción (salto de nivel) o
# que el odómetro retroceda. Por eso:
#
#   * Los huecos de tiempo se atraviesan. Se corta solo por salto de nivel (comparando la
#     mediana de las lecturas de un lado y del otro, descontando el consumo esperado entre
#     ambos lados) o por odómetro que retrocede.
#   * Se toma la ventana de los últimos ``km_ventana`` km recorridos (los segmentos más
#     recientes primero), no un rango de fechas.
#   * Todos los segmentos aportan a UNA sola pendiente (litros por km) con una ordenada propia
#     por segmento: ``m = ΣSxy / ΣSxx``. Un segmento largo pesa más que uno corto, y no se
#     descarta ninguno por tener pendiente >= 0: descartar los que "no bajan" y conservar los
#     que sí sesga el resultado hacia rendimientos altos.
#   * Siempre devuelve un número cuando hay consumo visible, con su error estándar (± %) en
#     lugar de negarse a opinar por falta de litros. Quien lo muestre decide cuánta confianza
#     poner: con pocos litros el error será grande.
#
# Los parámetros son de ESTA herramienta (no vienen del documento).

M_ODOMETRO = "corte_por_odometro_que_retrocede"
M_HUECO_LARGO = "corte_por_hueco_largo"
M_SIN_ODOMETRO = "ventana_sin_odometro"
M_SEGMENTO_CORTO = "segmento_con_pocos_puntos"

# Por qué terminó un tramo continuo (campo ``cierre`` de los bloques del historial).
CIERRE_KM = "km"  # alcanzó el tamaño de bloque: bloque completo
CIERRE_CARGA = "carga_o_extraccion"
CIERRE_ODOMETRO = "odometro"
CIERRE_HUECO = "hueco_largo"

MOTIVO_SIN_DATOS = "sin_datos"
MOTIVO_SIN_RECORRIDO = "sin_recorrido"
MOTIVO_SIN_CONSUMO = "sin_consumo_visible"


@dataclass(frozen=True, slots=True)
class RendimientoReciente:
    rendimiento_km_l: float | None  # None si no hay estimación (ver ``motivo``)
    error_pct: float | None  # ± % (un error estándar); None si no se puede calcular
    km: float  # recorrido que respalda la estimación
    litros: float  # consumo que explican esos km
    puntos: int  # ventanas usadas
    segmentos: int  # trozos continuos usados
    desde: datetime | None
    hasta: datetime | None
    motivo: str | None = None  # por qué no hay estimación
    descartes: Counter[str] = field(default_factory=Counter)


def _grupos_de_saltos(
    litros: Sequence[float],
    xs: Sequence[float],
    salto_litros: float,
    k: int,
    rendimiento_esperado: float | None,
) -> list[tuple[int, int]]:
    """Fronteras (b = entre b-1 y b) donde el nivel cambia de golpe, agrupadas.

    Compara la mediana de las ``k`` lecturas anteriores con la de las ``k`` siguientes. Si se
    conoce un rendimiento esperado, antes se descuenta lo que se debió consumir entre ambos
    lados (importa al atravesar un hueco con kilómetros recorridos sin datos).
    """
    fronteras: list[int] = []
    for b in range(1, len(litros)):
        izq = slice(max(0, b - k), b)
        der = slice(b, b + k)
        delta = statistics.median(litros[der]) - statistics.median(litros[izq])
        if rendimiento_esperado:
            dx = statistics.fmean(xs[der]) - statistics.fmean(xs[izq])
            delta += dx / rendimiento_esperado  # lo que debió bajar, no cuenta como salto
        if abs(delta) >= salto_litros:
            fronteras.append(b)
    grupos: list[list[int]] = []
    for b in fronteras:
        if grupos and b - grupos[-1][-1] <= 1:
            grupos[-1].append(b)
        else:
            grupos.append([b])
    return [(g[0], g[-1]) for g in grupos]


class _P(NamedTuple):
    t: datetime
    x: float  # km (odómetro ya convertido)
    y: float  # litros
    i: int  # posición en la serie (sin las ventanas sin odómetro)


@dataclass(slots=True)
class _Segmento:
    puntos: list[_P]
    cierre: str | None  # por qué terminó; None = sigue abierto (el más reciente)


def _segmentos_continuos(
    puntos: Sequence[PuntoRendimiento],
    *,
    odometro_a_km: float,
    salto_litros: float,
    max_hueco_horas: float | None,
    rendimiento_esperado: float | None,
    recorte: int,
    k_salto: int,
    descartes: Counter[str],
) -> tuple[list[_Segmento], int]:
    """Parte la serie en segmentos donde la recta litros-vs-km vale.

    Devuelve (segmentos, n): ``n`` es cuántos puntos tiene la serie (con odómetro).
    """
    # 1. cortes por odómetro que retrocede (y por hueco larguísimo, si se pidió)
    corridas: list[tuple[list[_P], str | None]] = []
    actual: list[_P] = []
    anterior: PuntoRendimiento | None = None
    n = 0
    for p in puntos:
        if p.odometro_total is None:
            descartes[M_SIN_ODOMETRO] += 1
            continue
        if anterior is not None and actual:
            if p.odometro_total < (anterior.odometro_total or 0.0):
                descartes[M_ODOMETRO] += 1
                corridas.append((actual, CIERRE_ODOMETRO))
                actual = []
            elif (
                max_hueco_horas is not None
                and p.ventana_inicio - anterior.ventana_inicio
                > timedelta(hours=max_hueco_horas)
            ):
                descartes[M_HUECO_LARGO] += 1
                corridas.append((actual, CIERRE_HUECO))
                actual = []
        actual.append(_P(p.ventana_inicio, p.odometro_total * odometro_a_km, p.litros, n))
        n += 1
        anterior = p
    if actual:
        corridas.append((actual, None))

    # 2. cortes por salto de nivel (cargas / extracciones), también a través de huecos
    segmentos: list[_Segmento] = []
    for corrida, cierre_corrida in corridas:
        xs = [q.x for q in corrida]
        ys = [q.y for q in corrida]
        inicio = 0
        for a, b in _grupos_de_saltos(ys, xs, salto_litros, k_salto, rendimiento_esperado):
            descartes[M_SALTO] += 1
            antes = corrida[inicio : max(inicio, a - recorte)]
            if antes:
                segmentos.append(_Segmento(antes, CIERRE_CARGA))
            inicio = min(len(corrida), b + recorte)
        resto = corrida[inicio:]
        if resto:
            segmentos.append(_Segmento(resto, cierre_corrida))
    return segmentos, n


class _Ajuste(NamedTuple):
    n: int
    xbar: float
    ybar: float
    sxx: float
    sxy: float
    syy: float


def _ajuste(pts: Sequence[_P]) -> _Ajuste:
    xs = [q.x for q in pts]
    ys = [q.y for q in pts]
    mx, my = statistics.fmean(xs), statistics.fmean(ys)
    return _Ajuste(
        len(pts),
        mx,
        my,
        sum((x - mx) ** 2 for x in xs),
        sum((x - mx) * (y - my) for x, y in zip(xs, ys, strict=True)),
        sum((y - my) ** 2 for y in ys),
    )


def estimar_rendimiento_reciente(
    puntos: Sequence[PuntoRendimiento],
    *,
    km_ventana: float = 100.0,
    odometro_a_km: float = 0.001,
    salto_litros: float = 2.0,
    rendimiento_esperado: float | None = None,
    max_hueco_horas: float | None = None,
    min_puntos: int = 3,
    recorte: int = 1,
    k_salto: int = 5,
) -> RendimientoReciente:
    """Rendimiento (km/L) en los últimos ``km_ventana`` km recorridos, con su error estándar.

    ``puntos``: ventanas válidas con litros ya calculados (odómetro en las unidades de
    gps_records; ``odometro_a_km`` lo convierte). ``salto_litros``: cambio de nivel que se toma
    por carga/extracción. ``rendimiento_esperado``: (p. ej. ``fuel_config.rendimiento_default``)
    evita confundir con una extracción el consumo normal de un hueco con km sin datos.
    ``max_hueco_horas``: por defecto los huecos de tiempo se atraviesan; con un valor, un hueco
    mayor corta. ``recorte``: lecturas que se omiten a cada lado de una carga.
    ``k_salto``: lecturas por lado con que se compara el nivel para detectarla.
    """
    ordenados = sorted(puntos, key=lambda p: p.ventana_inicio)
    descartes: Counter[str] = Counter()
    segmentos, _ = _segmentos_continuos(
        ordenados,
        odometro_a_km=odometro_a_km,
        salto_litros=salto_litros,
        max_hueco_horas=max_hueco_horas,
        rendimiento_esperado=rendimiento_esperado,
        recorte=recorte,
        k_salto=k_salto,
        descartes=descartes,
    )

    def sin_estimacion(
        motivo: str,
        km: float = 0.0,
        puntos: int = 0,
        segmentos: int = 0,
        desde: datetime | None = None,
        hasta: datetime | None = None,
    ) -> RendimientoReciente:
        return RendimientoReciente(
            rendimiento_km_l=None,
            error_pct=None,
            km=km,
            litros=0.0,
            puntos=puntos,
            segmentos=segmentos,
            desde=desde,
            hasta=hasta,
            motivo=motivo,
            descartes=descartes,
        )

    # 3. ventana: los segmentos más recientes primero hasta juntar km_ventana
    elegidos: list[list[_P]] = []
    faltan = km_ventana
    for segmento in reversed(segmentos):
        seg = segmento.puntos
        if len(seg) < min_puntos:
            descartes[M_SEGMENTO_CORTO] += 1
            continue
        largo = seg[-1].x - seg[0].x
        if largo > faltan:  # solo la cola del segmento que cabe en la ventana
            desde_km = seg[-1].x - faltan
            seg = [q for q in seg if q.x >= desde_km]
            if len(seg) < min_puntos:
                break
            largo = seg[-1].x - seg[0].x
        elegidos.append(seg)
        faltan -= largo
        if faltan <= 0:
            break
    if not elegidos:
        return sin_estimacion(MOTIVO_SIN_DATOS)

    # 4. una pendiente común con ordenada propia por segmento
    sxx = sxy = syy = 0.0
    n_total = 0
    km = 0.0
    for seg in elegidos:
        aj = _ajuste(seg)
        sxx += aj.sxx
        sxy += aj.sxy
        syy += aj.syy
        n_total += aj.n
        km += seg[-1].x - seg[0].x
    fechas = [q.t for seg in elegidos for q in seg]
    desde, hasta = min(fechas), max(fechas)
    resumen = (round(km, 2), n_total, len(elegidos), desde, hasta)
    if sxx <= 0:
        return sin_estimacion(MOTIVO_SIN_RECORRIDO, *resumen)
    m = sxy / sxx  # litros por km (negativa si el nivel baja)
    if m >= 0:
        return sin_estimacion(MOTIVO_SIN_CONSUMO, *resumen)

    grados = n_total - len(elegidos) - 1
    error_pct = None
    if grados >= 1:
        sse = max(0.0, syy - 2 * m * sxy + m * m * sxx)
        error_pct = round(100 * math.sqrt(sse / grados / sxx) / abs(m), 1)
    return RendimientoReciente(
        rendimiento_km_l=round(-1 / m, 2),
        error_pct=error_pct,
        km=round(km, 2),
        litros=round(-m * km, 2),
        puntos=n_total,
        segmentos=len(elegidos),
        desde=desde,
        hasta=hasta,
        motivo=None,
        descartes=descartes,
    )


# --------------------------------------------------------------------------------------
# Historial: bloques de km sin traslape, para reportes y métricas
# --------------------------------------------------------------------------------------
#
# ``estimar_rendimiento_reciente`` responde "cómo va ahora" y se recalcula cada ciclo: cada
# resultado se traslapa con el anterior, así que guardarlos todos no sirve para sumar. El
# historial guarda otra cosa: BLOQUES de ``km_bloque`` km consecutivos y sin traslape, cada uno
# con su propio ajuste (km, litros consumidos, rendimiento y error). Como no se traslapan, un
# reporte por día, semana o conductor se arma sumando: rendimiento = Σ km / Σ litros.
#
#   * Los bloques son inmutables: solo se emite un bloque cuando ya no puede cambiar con datos
#     posteriores (se deja un margen al final de la serie para la detección de cargas). Por eso
#     el cálculo se puede repetir cada ciclo y retomar en el ``fin`` del último bloque guardado
#     (``desde``) con el mismo resultado que si se hubiera calculado todo de una vez.
#   * Un bloque nunca cruza una carga, una extracción ni un retroceso del odómetro: ese tramo
#     se cierra antes (``completo=False``) y el siguiente bloque empieza después.
#   * Un bloque comparte su último punto con el primero del siguiente, así que los km suman
#     sin huecos ni dobles conteos.

ESTADO_OK = "ok"
ESTADO_SIN_CONSUMO = "sin_consumo_visible"
ESTADO_POCOS_PUNTOS = "pocos_puntos"
ESTADO_SIN_RECORRIDO = "sin_recorrido"


@dataclass(frozen=True, slots=True)
class BloqueRendimiento:
    inicio: datetime  # primera ventana del bloque
    fin: datetime  # última ventana (también es el inicio del siguiente bloque)
    odometro_inicio_km: float
    odometro_fin_km: float
    km: float
    litros_inicio: float | None  # nivel ajustado por la recta (sin el ruido de cada lectura)
    litros_fin: float | None
    litros_consumidos: float | None
    rendimiento_km_l: float | None
    error_pct: float | None
    puntos: int
    cierre: str  # CIERRE_*
    completo: bool  # True si cerró por alcanzar km_bloque
    estado: str  # ESTADO_*


def _bloque(pts: Sequence[_P], cierre: str, min_puntos: int) -> BloqueRendimiento:
    km = pts[-1].x - pts[0].x
    base = {
        "inicio": pts[0].t,
        "fin": pts[-1].t,
        "odometro_inicio_km": round(pts[0].x, 3),
        "odometro_fin_km": round(pts[-1].x, 3),
        "km": round(km, 3),
        "puntos": len(pts),
        "cierre": cierre,
        "completo": cierre == CIERRE_KM,
    }
    vacio = {
        "litros_inicio": None,
        "litros_fin": None,
        "litros_consumidos": None,
        "rendimiento_km_l": None,
        "error_pct": None,
    }
    if len(pts) < min_puntos:
        return BloqueRendimiento(**base, **vacio, estado=ESTADO_POCOS_PUNTOS)
    aj = _ajuste(pts)
    if aj.sxx <= 0:
        return BloqueRendimiento(**base, **vacio, estado=ESTADO_SIN_RECORRIDO)
    m = aj.sxy / aj.sxx  # litros por km
    inicio_l = aj.ybar + m * (pts[0].x - aj.xbar)
    fin_l = aj.ybar + m * (pts[-1].x - aj.xbar)
    if m >= 0:
        return BloqueRendimiento(
            **base,
            litros_inicio=round(inicio_l, 2),
            litros_fin=round(fin_l, 2),
            litros_consumidos=None,
            rendimiento_km_l=None,
            error_pct=None,
            estado=ESTADO_SIN_CONSUMO,
        )
    grados = aj.n - 2
    error_pct = None
    if grados >= 1:
        sse = max(0.0, aj.syy - 2 * m * aj.sxy + m * m * aj.sxx)
        error_pct = round(100 * math.sqrt(sse / grados / aj.sxx) / abs(m), 1)
    return BloqueRendimiento(
        **base,
        litros_inicio=round(inicio_l, 2),
        litros_fin=round(fin_l, 2),
        litros_consumidos=round(-m * km, 2),
        rendimiento_km_l=round(-1 / m, 2),
        error_pct=error_pct,
        estado=ESTADO_OK,
    )


def calcular_bloques(
    puntos: Sequence[PuntoRendimiento],
    *,
    km_bloque: float = 50.0,
    min_km_parcial: float = 5.0,
    desde: datetime | None = None,
    odometro_a_km: float = 0.001,
    salto_litros: float = 2.0,
    rendimiento_esperado: float | None = None,
    max_hueco_horas: float | None = None,
    min_puntos: int = 3,
    recorte: int = 1,
    k_salto: int = 5,
) -> list[BloqueRendimiento]:
    """Bloques ya definitivos de la serie, en orden. Los que aún pueden cambiar no salen.

    ``desde``: se retoma en este instante (el ``fin`` del último bloque guardado). Los puntos
    anteriores a ``desde`` solo sirven de contexto para detectar cargas; incluye unos cuantos
    (``2 * k_salto + recorte``) para que el resultado coincida con el de calcular todo junto.
    ``min_km_parcial``: un trozo que termina por una carga/odómetro antes de juntar
    ``km_bloque`` solo se guarda (como bloque incompleto) si recorrió al menos esto.
    """
    ordenados = sorted(puntos, key=lambda p: p.ventana_inicio)
    segmentos, n = _segmentos_continuos(
        ordenados,
        odometro_a_km=odometro_a_km,
        salto_litros=salto_litros,
        max_hueco_horas=max_hueco_horas,
        rendimiento_esperado=rendimiento_esperado,
        recorte=recorte,
        k_salto=k_salto,
        descartes=Counter(),
    )
    # Último punto cuya situación ya no cambia con datos posteriores: la detección de un
    # salto mira ``k_salto`` lecturas hacia adelante y luego se recortan ``recorte`` más.
    limite = n - 1 - (k_salto + recorte + 1)

    bloques: list[BloqueRendimiento] = []
    for segmento in segmentos:
        pts = segmento.puntos
        if desde is not None:
            pts = [q for q in pts if q.t >= desde]
        if len(pts) < 2:
            continue
        ini = 0
        for j in range(1, len(pts)):
            if pts[j].x - pts[ini].x >= km_bloque:
                if pts[j].i > limite:  # todavía puede cambiar: se espera al siguiente ciclo
                    break
                bloques.append(_bloque(pts[ini : j + 1], CIERRE_KM, min_puntos))
                ini = j
        else:
            # El segmento terminó por un corte (no por falta de datos): lo que queda es un
            # bloque incompleto, si es lo bastante largo y ya es definitivo.
            resto = pts[ini:]
            if (
                segmento.cierre is not None
                and resto[-1].i <= limite
                and resto[-1].x - resto[0].x >= min_km_parcial
            ):
                bloques.append(_bloque(resto, segmento.cierre, min_puntos))
    return bloques
