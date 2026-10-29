"""Consolidación de gps_records en ventanas de 5 minutos -> fuel_windows.

Qué hace
  1. Resuelve IMEI -> dispositivo -> autobús -> fuel_config (solo se consolidan los IMEI
     asignados a un autobús con configuración activa). El sensor es ``gps_records.fuel``.
  2. Agrupa por (imei, ventana) usando SIEMPRE ``gps_records.recorded_at`` (nunca created_at).
  3. Quita duplicados exactos (mismo imei y recorded_at) antes de agregar.
  4. Calcula mediana, promedio, mín., máx., conteos y estadísticos de ángulo en SQL.
  5. La calidad de cada ventana (valida / dudosa / descartada) la decide ``quality.py``.
  6. Convierte la mediana del sensor a litros con la calibración del autobús
     (``fuel_config.calibracion``). Son litros crudos, con el ruido del sensor.
  7. Calcula ``litros_grafica``: igual a ``litros``, salvo estacionado (se arrastra el nivel
     anterior y solo pasan cargas/extracciones) o con el ángulo fuera de rango (última ventana
     válida menos km / rendimiento).
  8. Hace upsert idempotente en ``fuel_windows`` por (imei, ventana_inicio).

Qué NO hace: no modifica gps_records ni toca los litros crudos.

Las ventanas viven en una rejilla global anclada a 2000-01-01 00:00 UTC. Como Hermosillo
no tiene horario de verano y 5 min divide una hora exacta, la rejilla coincide con las
marcas :00, :05, :10... de la hora local.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any, NamedTuple

from psycopg import Connection

from .calibration import CalibrationError, CalibrationTable
from .quality import M_ANGULO, QualityRules, WindowStats, evaluate_window

log = logging.getLogger(__name__)

WINDOW_MINUTES = 5
GRID_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)
ODOMETRO_A_KM = 0.001  # gps_records guarda el odómetro en METROS (Teltonika)
# < 300 m en 5 min es ir a < 3.6 km/h: maniobra o deriva del GPS estacionado (se ven ventanas
# de hasta ~150 m sin moverse). Lo que se consume en ese tramo (~0.04 L) no se ve en el sensor.
ODOMETRO_PARADO_M = 300.0
# Al detenerse el sensor tarda hasta 2 ventanas en asentarse en el valor (falso) de la rampa:
# la de la parada mezcla muestras en marcha y en reposo.
VENTANAS_TRANSICION = 2
# Al arrancar el sensor sigue mandando 1-3 ventanas el valor de estacionado o del encendido
# (cambia por escalones, p. ej. 1554 fijo 13 min y 4 km el 10-28 16:36) antes de ponerse al día.
VENTANAS_ARRANQUE = 3
# Ya estacionado, la deriva es ~0.1 L por ventana; una carga/extracción mueve 2-12 L por ventana.
SALTO_ESTACIONADO_L = 1.0
# Estacionado, % mínimo de muestras con ángulo válido para tomar la lectura (en prueba).
PORCENTAJE_ANGULO_ESTACIONADO = 80.0
# En marcha los baches sacan muchas muestras de rango; con < 50% suben las lecturas con más de
# 1.5 L de error (13% -> 8% al filtrar, medido del 10-09 al 10-28 en el autobús 2).
PORCENTAJE_ANGULO_MARCHA = 50.0

W_MIN_MUESTRAS_PENDIENTE = "min_muestras_ventana sin definir en fuel_config: no se valida suficiencia de muestras."

# --------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------

AGGREGATE_SQL = """
WITH cfg AS (
    SELECT d.imei,
           d.idautobus,
           c.fcc_inclinacion,
           c.angulo_max,
           c.min_muestras_ventana
    FROM public.dispositivos d
    JOIN public.fuel_config c ON c.idautobus = d.idautobus
    WHERE d.imei IS NOT NULL
      AND c.activo
      AND (%(imei)s::text IS NULL OR d.imei = %(imei)s::text)
),
src AS (
    SELECT DISTINCT ON (g.imei, g.recorded_at)
           g.imei,
           g.recorded_at,
           -- Un valor negativo (p. ej. -4) es un código de lectura inválida: se ignora.
           CASE WHEN g.fuel >= 0 THEN g.fuel END                       AS sensor_uso,
           g.inclinacion_vertical,
           g.speed,
           g.ignicion,
           g.odometro_total
    FROM public.gps_records g
    JOIN cfg ON cfg.imei = g.imei
    WHERE g.recorded_at >= %(desde)s
      AND g.recorded_at <  %(hasta)s
    ORDER BY g.imei, g.recorded_at, g.created_at DESC NULLS LAST
)
SELECT s.imei,
       cfg.idautobus,
       date_bin(make_interval(mins => %(minutos)s::int), s.recorded_at,
                %(origen)s::timestamptz)                                   AS ventana_inicio,
       cfg.fcc_inclinacion,
       cfg.angulo_max,
       cfg.min_muestras_ventana,
       count(*)                                                            AS muestras_totales,
       count(s.sensor_uso)                                              AS muestras_validas,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.sensor_uso)        AS sensor_mediana,
       avg(s.sensor_uso)                                                AS sensor_promedio,
       min(s.sensor_uso)                                                AS sensor_min,
       max(s.sensor_uso)                                                AS sensor_max,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.inclinacion_vertical) AS inclinacion_mediana,
       count(s.inclinacion_vertical)                                       AS muestras_con_angulo,
       count(*) FILTER (
           WHERE s.inclinacion_vertical IS NOT NULL
             AND abs(s.inclinacion_vertical - cfg.fcc_inclinacion) <= cfg.angulo_max
       )                                                                   AS muestras_angulo_valido,
       avg(s.speed)                                                        AS velocidad_promedio,
       -- Informativo (gráfica): encendida si al menos la mitad de las muestras con dato lo están.
       CASE WHEN count(s.ignicion) > 0
            THEN count(*) FILTER (WHERE s.ignicion) * 2 >= count(s.ignicion)
       END                                                                 AS ignicion_dominante,
       max(s.odometro_total)                                               AS odometro_total
FROM src s
JOIN cfg ON cfg.imei = s.imei
GROUP BY s.imei, cfg.idautobus, cfg.fcc_inclinacion, cfg.angulo_max,
         cfg.min_muestras_ventana, ventana_inicio
ORDER BY ventana_inicio, s.imei
"""

UPSERT_SQL = """
INSERT INTO public.fuel_windows (
    imei, ventana_inicio, ventana_fin, muestras_totales, muestras_validas,
    sensor_mediana, sensor_promedio, sensor_min, sensor_max,
    inclinacion_mediana, angulo_corregido, porcentaje_angulo_valido,
    velocidad_promedio, ignicion_dominante, odometro_total,
    calidad_ventana, motivo_calidad, litros, estado_calibracion, litros_grafica
) VALUES (
    %(imei)s, %(ventana_inicio)s, %(ventana_fin)s, %(muestras_totales)s, %(muestras_validas)s,
    %(sensor_mediana)s, %(sensor_promedio)s, %(sensor_min)s, %(sensor_max)s,
    %(inclinacion_mediana)s, %(angulo_corregido)s, %(porcentaje_angulo_valido)s,
    %(velocidad_promedio)s, %(ignicion_dominante)s, %(odometro_total)s,
    %(calidad_ventana)s, %(motivo_calidad)s, %(litros)s, %(estado_calibracion)s, %(litros_grafica)s
)
ON CONFLICT (imei, ventana_inicio) DO UPDATE SET
    ventana_fin              = EXCLUDED.ventana_fin,
    muestras_totales         = EXCLUDED.muestras_totales,
    muestras_validas         = EXCLUDED.muestras_validas,
    sensor_mediana           = EXCLUDED.sensor_mediana,
    sensor_promedio          = EXCLUDED.sensor_promedio,
    sensor_min               = EXCLUDED.sensor_min,
    sensor_max               = EXCLUDED.sensor_max,
    inclinacion_mediana      = EXCLUDED.inclinacion_mediana,
    angulo_corregido         = EXCLUDED.angulo_corregido,
    porcentaje_angulo_valido = EXCLUDED.porcentaje_angulo_valido,
    velocidad_promedio       = EXCLUDED.velocidad_promedio,
    ignicion_dominante       = EXCLUDED.ignicion_dominante,
    odometro_total           = EXCLUDED.odometro_total,
    calidad_ventana          = EXCLUDED.calidad_ventana,
    motivo_calidad           = EXCLUDED.motivo_calidad,
    litros                   = EXCLUDED.litros,
    estado_calibracion       = EXCLUDED.estado_calibracion,
    litros_grafica           = EXCLUDED.litros_grafica
RETURNING (xmax = 0) AS insertada
"""

CALIBRACIONES_SQL = "SELECT idautobus, calibracion FROM public.fuel_config WHERE activo"

RENDIMIENTOS_SQL = """
SELECT c.idautobus, coalesce(r.rendimiento_km_l, c.rendimiento_default)
FROM public.fuel_config c
LEFT JOIN public.fuel_rendimiento_reciente r ON r.idautobus = c.idautobus
WHERE c.activo
"""

# Últimas ventanas (con odómetro) de cada IMEI antes del rango: el nivel del que se parte y
# si la primera del rango está estacionada o recién arrancada.
PREVIAS_SQL = """
SELECT imei, odometro_total, litros_grafica, litros, porcentaje_angulo_valido
FROM (
    SELECT imei, ventana_inicio, odometro_total, litros_grafica, litros, porcentaje_angulo_valido,
           row_number() OVER (PARTITION BY imei ORDER BY ventana_inicio DESC) AS k
    FROM public.fuel_windows
    WHERE ventana_inicio < %(desde)s
      AND odometro_total IS NOT NULL
      AND (%(imei)s::text IS NULL OR imei = %(imei)s::text)
) x
WHERE k <= %(n)s
ORDER BY imei, k
"""


# --------------------------------------------------------------------------------------
# Tiempo
# --------------------------------------------------------------------------------------


def align_down(ts: datetime, minutes: int = WINDOW_MINUTES) -> datetime:
    """Inicio de la ventana que contiene ``ts`` (misma rejilla que date_bin en SQL)."""
    if ts.tzinfo is None:
        raise ValueError("ts debe tener zona horaria")
    if minutes <= 0 or 60 % minutes != 0:
        raise ValueError("minutes debe dividir exactamente una hora")
    step = minutes * 60
    aligned = (int(ts.timestamp()) // step) * step
    return datetime.fromtimestamp(aligned, tz=ts.tzinfo)


def closed_window_limit(
    now: datetime, lag_minutes: int, minutes: int = WINDOW_MINUTES
) -> datetime:
    """Límite superior (exclusivo) de lo que ya se puede consolidar.

    Se deja un colchón de ``lag_minutes`` para tolerar datos que llegan tarde.
    """
    return align_down(now - timedelta(minutes=lag_minutes), minutes)


class NoStartError(RuntimeError):
    """No hay punto de partida: fuel_windows está vacía y no se indicó --desde."""


def plan_range(
    conn: Connection,
    *,
    now: datetime,
    desde: datetime | None,
    hasta: datetime | None,
    lag_minutes: int,
    overlap_minutes: int,
    minutes: int = WINDOW_MINUTES,
    imei: str | None = None,
) -> tuple[datetime, datetime]:
    """Rango [desde, hasta) alineado a la rejilla.

    Sin ``hasta``: la última ventana cerrada (con colchón). Sin ``desde``: se retoma desde la
    última ventana guardada menos ``overlap_minutes``, para recoger datos tardíos.
    """
    hasta_ = (
        closed_window_limit(now, lag_minutes, minutes)
        if hasta is None
        else align_down(hasta, minutes)
    )
    if desde is None:
        row = conn.execute(
            "SELECT max(ventana_fin) FROM public.fuel_windows "
            "WHERE (%(imei)s::text IS NULL OR imei = %(imei)s::text)",
            {"imei": imei},
        ).fetchone()
        last = row[0] if row else None
        if last is None:
            raise NoStartError("fuel_windows está vacía: indica --desde.")
        desde = last - timedelta(minutes=overlap_minutes)
    return align_down(desde, minutes), hasta_


# --------------------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------------------


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


def _r(value: float | None, digits: int = 2) -> float | None:
    return None if value is None else round(value, digits)


@dataclass(frozen=True, slots=True)
class WindowAggregate:
    """Una fila del SQL de agregación (sin interpretar)."""

    imei: str
    idautobus: str
    ventana_inicio: datetime
    fcc_inclinacion: float
    angulo_max: float
    min_muestras: int | None
    muestras_totales: int
    muestras_validas: int
    sensor_mediana: float | None
    sensor_promedio: float | None
    sensor_min: float | None
    sensor_max: float | None
    inclinacion_mediana: float | None
    muestras_con_angulo: int
    muestras_angulo_valido: int
    velocidad_promedio: float | None
    ignicion_dominante: bool | None
    odometro_total: float | None

    @classmethod
    def from_row(cls, row: dict[str, Any]) -> WindowAggregate:
        return cls(
            imei=row["imei"],
            idautobus=row["idautobus"],
            ventana_inicio=row["ventana_inicio"],
            fcc_inclinacion=float(row["fcc_inclinacion"]),
            angulo_max=float(row["angulo_max"]),
            min_muestras=row["min_muestras_ventana"],
            muestras_totales=int(row["muestras_totales"]),
            muestras_validas=int(row["muestras_validas"]),
            sensor_mediana=_f(row["sensor_mediana"]),
            sensor_promedio=_f(row["sensor_promedio"]),
            sensor_min=_f(row["sensor_min"]),
            sensor_max=_f(row["sensor_max"]),
            inclinacion_mediana=_f(row["inclinacion_mediana"]),
            muestras_con_angulo=int(row["muestras_con_angulo"]),
            muestras_angulo_valido=int(row["muestras_angulo_valido"]),
            velocidad_promedio=_f(row["velocidad_promedio"]),
            ignicion_dominante=row["ignicion_dominante"],
            odometro_total=_f(row["odometro_total"]),
        )


@dataclass(frozen=True, slots=True)
class ConsolidatedWindow:
    """Fila lista para fuel_windows (valores ya redondeados a la escala de la tabla)."""

    imei: str
    idautobus: str  # solo informativo: fuel_windows no lo guarda
    ventana_inicio: datetime
    ventana_fin: datetime
    muestras_totales: int
    muestras_validas: int
    sensor_mediana: float | None
    sensor_promedio: float | None
    sensor_min: float | None
    sensor_max: float | None
    inclinacion_mediana: float | None
    angulo_corregido: float | None
    porcentaje_angulo_valido: float | None
    velocidad_promedio: float | None
    ignicion_dominante: bool | None
    odometro_total: float | None
    calidad_ventana: str
    motivo_calidad: str | None
    litros: float | None  # crudos: en zona_baja / arriba_de_calibracion son cotas
    estado_calibracion: str | None  # None = el autobús no tiene calibración válida
    litros_grafica: float | None = None  # lo llena litros_para_grafica

    def as_params(self) -> dict[str, Any]:
        return {
            "imei": self.imei,
            "ventana_inicio": self.ventana_inicio,
            "ventana_fin": self.ventana_fin,
            "muestras_totales": self.muestras_totales,
            "muestras_validas": self.muestras_validas,
            "sensor_mediana": self.sensor_mediana,
            "sensor_promedio": self.sensor_promedio,
            "sensor_min": self.sensor_min,
            "sensor_max": self.sensor_max,
            "inclinacion_mediana": self.inclinacion_mediana,
            "angulo_corregido": self.angulo_corregido,
            "porcentaje_angulo_valido": self.porcentaje_angulo_valido,
            "velocidad_promedio": self.velocidad_promedio,
            "ignicion_dominante": self.ignicion_dominante,
            "odometro_total": self.odometro_total,
            "calidad_ventana": self.calidad_ventana,
            "motivo_calidad": self.motivo_calidad,
            "litros": self.litros,
            "estado_calibracion": self.estado_calibracion,
            "litros_grafica": self.litros_grafica,
        }


class Previa(NamedTuple):
    """Lo que ``litros_para_grafica`` necesita de la ventana anterior de un IMEI."""

    odometro: float
    litros_grafica: float | None
    litros: float | None  # última lectura cruda disponible
    parados: int  # estacionadas seguidas hasta esta (tope VENTANAS_TRANSICION + 1)
    marcha: int  # en marcha seguidas hasta esta (tope VENTANAS_ARRANQUE + 1)


@dataclass(slots=True)
class ConsolidationSummary:
    desde: datetime
    hasta: datetime
    dry_run: bool = False
    ventanas: int = 0
    insertadas: int = 0
    actualizadas: int = 0
    imeis: set[str] = field(default_factory=set)
    por_calidad: Counter[str] = field(default_factory=Counter)
    por_motivo: Counter[str] = field(default_factory=Counter)
    advertencias: list[str] = field(default_factory=list)
    muestra: list[ConsolidatedWindow] = field(default_factory=list)

    def advertir(self, texto: str) -> None:
        if texto not in self.advertencias:
            self.advertencias.append(texto)


# --------------------------------------------------------------------------------------
# Lógica
# --------------------------------------------------------------------------------------


def build_window(
    agg: WindowAggregate,
    minutes: int = WINDOW_MINUTES,
    base_rules: QualityRules | None = None,
    calibracion: CalibrationTable | None = None,
) -> ConsolidatedWindow:
    """Convierte un agregado en la fila de fuel_windows: calidad y litros.

    FCC, ángulo máximo y mínimo de muestras vienen de fuel_config (por vehículo); el resto
    de reglas opcionales, de ``base_rules``. Sin ``calibracion`` los litros quedan en None.
    """
    rules = replace(
        base_rules or QualityRules(fcc_inclinacion=0.0),
        fcc_inclinacion=agg.fcc_inclinacion,
        angulo_max=agg.angulo_max,
        min_muestras=agg.min_muestras,
    )
    stats = WindowStats(
        muestras_totales=agg.muestras_totales,
        muestras_validas=agg.muestras_validas,
        sensor_mediana=agg.sensor_mediana,
        sensor_min=agg.sensor_min,
        sensor_max=agg.sensor_max,
        inclinacion_mediana=agg.inclinacion_mediana,
        muestras_con_angulo=agg.muestras_con_angulo,
        muestras_angulo_valido=agg.muestras_angulo_valido,
    )
    q = evaluate_window(stats, rules)
    conv = calibracion.to_liters(agg.sensor_mediana) if calibracion else None
    return ConsolidatedWindow(
        imei=agg.imei,
        idautobus=agg.idautobus,
        ventana_inicio=agg.ventana_inicio,
        ventana_fin=agg.ventana_inicio + timedelta(minutes=minutes),
        muestras_totales=agg.muestras_totales,
        muestras_validas=agg.muestras_validas,
        sensor_mediana=_r(agg.sensor_mediana),
        sensor_promedio=_r(agg.sensor_promedio),
        sensor_min=_r(agg.sensor_min),
        sensor_max=_r(agg.sensor_max),
        inclinacion_mediana=_r(agg.inclinacion_mediana),
        angulo_corregido=q.angulo_corregido,
        porcentaje_angulo_valido=q.porcentaje_angulo_valido,
        velocidad_promedio=_r(agg.velocidad_promedio),
        ignicion_dominante=agg.ignicion_dominante,
        odometro_total=_r(agg.odometro_total),
        calidad_ventana=q.calidad.value,
        motivo_calidad=q.motivo,
        litros=_r(conv.litros) if conv else None,
        estado_calibracion=conv.estado.value if conv else None,
    )


def litros_para_grafica(
    windows: list[ConsolidatedWindow],
    rendimientos: dict[str, float | None],
    previas: dict[str, Previa],
) -> list[ConsolidatedWindow]:
    """Llena ``litros_grafica``. ``windows`` debe venir en orden de tiempo.

    ``previas`` (imei -> estado de la ventana anterior) se actualiza en el lugar, para que pase
    de un bloque al siguiente. Todo el estado sale de la ventana anterior, así que retomar
    desde lo guardado en la base (``fetch_previas``) da lo mismo que calcular de corrido.

    1. Estacionado (el odómetro avanzó menos de ``ODOMETRO_PARADO_M`` desde la ventana
       anterior): se arrastra el nivel de la ventana anterior. Al detenerse el sensor brinca
       a un valor falso (rampas, ±2-5 L) aunque el ángulo esté en rango, y luego solo deriva
       despacio. Por eso se ignoran las primeras ``VENTANAS_TRANSICION`` ventanas y, después,
       un cambio de al menos ``SALTO_ESTACIONADO_L`` entre ventanas seguidas (carga o
       extracción con el motor apagado) hace que la gráfica tome la lectura.
       Las ventanas con ``porcentaje_angulo_valido`` < ``PORCENTAJE_ANGULO_ESTACIONADO`` no
       cuentan como lectura.
    2. Ángulo fuera de rango, en marcha con ``porcentaje_angulo_valido`` <
       ``PORCENTAJE_ANGULO_MARCHA``, o primeras ``VENTANAS_ARRANQUE`` en marcha tras una
       parada de más de ``VENTANAS_TRANSICION`` ventanas: nivel anterior - km desde la ventana
       anterior / rendimiento. NULL si falta nivel anterior, rendimiento u odómetro, si el
       odómetro retrocede o si daría negativo.
    3. Lo demás: los litros medidos.
    """
    out: list[ConsolidatedWindow] = []
    for w in windows:
        litros = w.litros
        previa = previas.get(w.imei)
        parado, parados, marcha = _movimiento(previa, w.odometro_total)
        estacionado = (
            parado and previa is not None and previa.litros_grafica is not None
        )
        confiable = _angulo_confiable_estacionado(w.porcentaje_angulo_valido)
        if estacionado:
            litros = previa.litros_grafica
            if (
                parados > VENTANAS_TRANSICION
                and confiable
                and w.litros is not None
                and previa.litros is not None
                and abs(w.litros - previa.litros) >= SALTO_ESTACIONADO_L
            ):
                # Se toma la lectura y no la suma de cambios: así no se pierden los cambios
                # chicos ni se rebasan los topes del sensor (0 L y máximo calibrado).
                litros = w.litros
        elif (
            (w.motivo_calidad and M_ANGULO in w.motivo_calidad.split("; "))
            or (
                w.porcentaje_angulo_valido is not None
                and w.porcentaje_angulo_valido < PORCENTAJE_ANGULO_MARCHA
            )
            or 1 <= marcha <= VENTANAS_ARRANQUE
        ):
            rend = rendimientos.get(w.idautobus)
            litros = None
            if (
                previa is not None
                and previa.litros_grafica is not None
                and rend
                and w.odometro_total is not None
            ):
                km = (w.odometro_total - previa.odometro) * ODOMETRO_A_KM
                # negativo = nivel viejo con muchos km encima: no hay estimado creíble
                if km >= 0 and previa.litros_grafica - km / rend >= 0:
                    litros = _r(previa.litros_grafica - km / rend)
        if w.odometro_total is not None:
            usar = w.litros is not None and (confiable or not estacionado)
            crudo = w.litros if usar else previa and previa.litros
            previas[w.imei] = Previa(w.odometro_total, litros, crudo, parados, marcha)
        out.append(replace(w, litros_grafica=litros))
    return out


def _movimiento(previa: Previa | None, odometro: float | None) -> tuple[bool, int, int]:
    """(parado, parados, marcha) de una ventana a partir de la anterior."""
    parado = (
        previa is not None
        and odometro is not None
        and 0 <= odometro - previa.odometro < ODOMETRO_PARADO_M
    )
    if parado:
        return True, min(previa.parados + 1, VENTANAS_TRANSICION + 1), 0
    if previa is None or (previa.marcha == 0 and previa.parados <= VENTANAS_TRANSICION):
        # sin historia, o tras una parada corta (semáforo, maniobra): el sensor no se quedó
        # con un valor de reposo, no hay arranque que bloquear
        return False, 0, VENTANAS_ARRANQUE + 1
    return False, 0, min(previa.marcha + 1, VENTANAS_ARRANQUE + 1)


def _angulo_confiable_estacionado(porcentaje: float | None) -> bool:
    """Estacionado, una ventana con muchas muestras fuera de ángulo no cuenta como lectura."""
    return porcentaje is None or porcentaje >= PORCENTAJE_ANGULO_ESTACIONADO


def fetch_rendimientos(conn: Connection) -> dict[str, float | None]:
    """km/L de cada autobús activo: el reciente y, si no hay, ``rendimiento_default``."""
    return {bus: _f(rend) for bus, rend in conn.execute(RENDIMIENTOS_SQL).fetchall()}


def fetch_previas(
    conn: Connection, desde: datetime, imei: str | None = None
) -> dict[str, Previa]:
    """Estado de la última ventana antes de ``desde``, reconstruido de las anteriores igual
    que lo lleva ``litros_para_grafica``."""
    # Suficientes ventanas para ver una parada completa (tope de ``parados``) y un arranque.
    params = {
        "desde": desde,
        "imei": imei,
        "n": VENTANAS_TRANSICION + VENTANAS_ARRANQUE + 3,
    }
    filas: dict[str, list[tuple[Any, ...]]] = {}
    for i, odo, lg, lt, pct in conn.execute(PREVIAS_SQL, params).fetchall():
        filas.setdefault(i, []).append((float(odo), _f(lg), _f(lt), _f(pct)))
    out: dict[str, Previa] = {}
    for i, f in filas.items():
        # De la más vieja a la más reciente, con las mismas reglas que en memoria. La más vieja
        # no tiene anterior: se toma como en marcha sin arranque pendiente.
        p: Previa | None = None
        for odo, lg, lt, pct in reversed(f):
            parado, parados, marcha = _movimiento(p, odo)
            estacionado = parado and p is not None and p.litros_grafica is not None
            usar = lt is not None and (
                _angulo_confiable_estacionado(pct) or not estacionado
            )
            crudo = lt if usar else p and p.litros
            p = Previa(odo, lg, crudo, parados, marcha)
        assert p is not None
        out[i] = p
    return out


def fetch_calibraciones(
    conn: Connection, summary: ConsolidationSummary | None = None
) -> dict[str, CalibrationTable | None]:
    """Calibración de cada autobús activo. Una inválida queda en None (y se advierte)."""
    out: dict[str, CalibrationTable | None] = {}
    for bus, raw in conn.execute(CALIBRACIONES_SQL).fetchall():
        try:
            out[bus] = CalibrationTable.from_json(raw)
        except CalibrationError as exc:
            out[bus] = None
            log.error(
                "%s: calibración inválida, sus ventanas quedan sin litros: %s", bus, exc
            )
            if summary is not None:
                summary.advertir(
                    f"{bus}: calibración inválida ({exc}); ventanas sin litros."
                )
    return out


def fetch_aggregates(
    conn: Connection,
    desde: datetime,
    hasta: datetime,
    *,
    imei: str | None = None,
    minutes: int = WINDOW_MINUTES,
) -> list[WindowAggregate]:
    """Lee gps_records en [desde, hasta) y agrega por (imei, ventana)."""
    params = {
        "desde": desde,
        "hasta": hasta,
        "imei": imei,
        "minutos": minutes,
        "origen": GRID_ORIGIN,
    }
    with conn.cursor() as cur:
        cur.execute(AGGREGATE_SQL, params)
        names = [c.name for c in cur.description or []]
        return [
            WindowAggregate.from_row(dict(zip(names, row, strict=True)))
            for row in cur.fetchall()
        ]


def upsert_windows(
    conn: Connection, windows: list[ConsolidatedWindow]
) -> tuple[int, int]:
    """Upsert idempotente por (imei, ventana_inicio). Devuelve (insertadas, actualizadas)."""
    if not windows:
        return 0, 0
    insertadas = actualizadas = 0
    with conn.cursor() as cur:
        cur.executemany(UPSERT_SQL, [w.as_params() for w in windows], returning=True)
        while True:
            row = cur.fetchone()
            if row is not None:
                if row[0]:
                    insertadas += 1
                else:
                    actualizadas += 1
            if not cur.nextset():
                break
    return insertadas, actualizadas


def consolidate(
    conn: Connection,
    desde: datetime,
    hasta: datetime,
    *,
    imei: str | None = None,
    minutes: int = WINDOW_MINUTES,
    base_rules: QualityRules | None = None,
    dry_run: bool = False,
    chunk_hours: int = 24,
    keep_sample: int = 0,
) -> ConsolidationSummary:
    """Consolida [desde, hasta) en fuel_windows. No hace commit: lo decide quien llama.

    Con ``dry_run`` calcula todo pero no escribe nada.
    """
    desde = align_down(desde, minutes)
    hasta = align_down(hasta, minutes)
    summary = ConsolidationSummary(desde=desde, hasta=hasta, dry_run=dry_run)
    calibraciones = fetch_calibraciones(conn, summary)
    rendimientos = fetch_rendimientos(conn)
    previas = fetch_previas(conn, desde, imei)

    cursor = desde
    while cursor < hasta:
        end = min(cursor + timedelta(hours=chunk_hours), hasta)
        aggs = fetch_aggregates(conn, cursor, end, imei=imei, minutes=minutes)
        windows = litros_para_grafica(
            [
                build_window(a, minutes, base_rules, calibraciones.get(a.idautobus))
                for a in aggs
            ],
            rendimientos,
            previas,
        )

        for a, w in zip(aggs, windows, strict=True):
            summary.ventanas += 1
            summary.imeis.add(w.imei)
            summary.por_calidad[w.calidad_ventana] += 1
            if w.motivo_calidad:
                for m in w.motivo_calidad.split("; "):
                    summary.por_motivo[m] += 1
            if a.min_muestras is None:
                summary.advertir(W_MIN_MUESTRAS_PENDIENTE)
            if len(summary.muestra) < keep_sample:
                summary.muestra.append(w)

        if windows and not dry_run:
            ins, upd = upsert_windows(conn, windows)
            summary.insertadas += ins
            summary.actualizadas += upd
        cursor = end

    log.info(
        "consolidación %s -> %s UTC: %d ventanas (%d nuevas, %d actualizadas)%s",
        summary.desde.astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
        summary.hasta.astimezone(UTC).strftime("%Y-%m-%d %H:%M"),
        summary.ventanas,
        summary.insertadas,
        summary.actualizadas,
        " [dry-run]" if dry_run else "",
    )
    return summary


__all__ = [
    "WINDOW_MINUTES",
    "ConsolidatedWindow",
    "ConsolidationSummary",
    "NoStartError",
    "Previa",
    "WindowAggregate",
    "align_down",
    "build_window",
    "closed_window_limit",
    "consolidate",
    "fetch_aggregates",
    "fetch_calibraciones",
    "fetch_previas",
    "fetch_rendimientos",
    "litros_para_grafica",
    "plan_range",
    "upsert_windows",
]
