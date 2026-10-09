"""Consolidación de gps_records en ventanas de 5 minutos -> fuel_windows.

Qué hace
  1. Resuelve IMEI -> dispositivo -> autobús -> fuel_config (solo se consolidan los IMEI
     asignados a un autobús con configuración activa).
  2. Agrupa por (imei, ventana) usando SIEMPRE ``gps_records.recorded_at`` (nunca created_at).
  3. Quita duplicados exactos (mismo imei y recorded_at) antes de agregar.
  4. Calcula mediana, promedio, mín., máx., conteos y estadísticos de ángulo en SQL.
  5. La calidad de cada ventana (valida / dudosa / descartada) la decide ``quality.py``.
  6. Hace upsert idempotente en ``fuel_windows`` por (imei, ventana_inicio).

Qué NO hace: no modifica gps_records, no calcula litros, no compara contra el valor confiable.

Las ventanas viven en una rejilla global anclada a 2000-01-01 00:00 UTC. Como Hermosillo
no tiene horario de verano y 5 min divide una hora exacta, la rejilla coincide con las
marcas :00, :05, :10... de la hora local.
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import UTC, datetime, timedelta
from typing import Any

from psycopg import Connection

from .quality import QualityRules, WindowStats, evaluate_window

log = logging.getLogger(__name__)

WINDOW_MINUTES = 5
GRID_ORIGIN = datetime(2000, 1, 1, tzinfo=UTC)

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
           g.s_analogo,
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
       count(s.s_analogo)                                                  AS muestras_validas,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.s_analogo)            AS sensor_mediana,
       avg(s.s_analogo)                                                    AS sensor_promedio,
       min(s.s_analogo)                                                    AS sensor_min,
       max(s.s_analogo)                                                    AS sensor_max,
       percentile_cont(0.5) WITHIN GROUP (ORDER BY s.inclinacion_vertical) AS inclinacion_mediana,
       count(s.inclinacion_vertical)                                       AS muestras_con_angulo,
       count(*) FILTER (
           WHERE s.inclinacion_vertical IS NOT NULL
             AND abs(s.inclinacion_vertical - cfg.fcc_inclinacion) <= cfg.angulo_max
       )                                                                   AS muestras_angulo_valido,
       avg(s.speed)                                                        AS velocidad_promedio,
       count(s.ignicion)                                                   AS muestras_con_ignicion,
       count(*) FILTER (WHERE s.ignicion)                                  AS muestras_ignicion_on,
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
    calidad_ventana, motivo_calidad
) VALUES (
    %(imei)s, %(ventana_inicio)s, %(ventana_fin)s, %(muestras_totales)s, %(muestras_validas)s,
    %(sensor_mediana)s, %(sensor_promedio)s, %(sensor_min)s, %(sensor_max)s,
    %(inclinacion_mediana)s, %(angulo_corregido)s, %(porcentaje_angulo_valido)s,
    %(velocidad_promedio)s, %(ignicion_dominante)s, %(odometro_total)s,
    %(calidad_ventana)s, %(motivo_calidad)s
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
    motivo_calidad           = EXCLUDED.motivo_calidad
RETURNING (xmax = 0) AS insertada
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
    muestras_con_ignicion: int
    muestras_ignicion_on: int
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
            muestras_con_ignicion=int(row["muestras_con_ignicion"]),
            muestras_ignicion_on=int(row["muestras_ignicion_on"]),
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
        }


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
) -> ConsolidatedWindow:
    """Convierte un agregado en la fila de fuel_windows, aplicando las reglas de calidad.

    FCC, ángulo máximo y mínimo de muestras vienen de fuel_config (por vehículo); el resto
    de reglas opcionales, de ``base_rules``.
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
        muestras_con_ignicion=agg.muestras_con_ignicion,
        muestras_ignicion_on=agg.muestras_ignicion_on,
    )
    q = evaluate_window(stats, rules)
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
        ignicion_dominante=q.ignicion_dominante,
        odometro_total=_r(agg.odometro_total),
        calidad_ventana=q.calidad.value,
        motivo_calidad=q.motivo,
    )


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

    cursor = desde
    while cursor < hasta:
        end = min(cursor + timedelta(hours=chunk_hours), hasta)
        aggs = fetch_aggregates(conn, cursor, end, imei=imei, minutes=minutes)
        windows = [build_window(a, minutes, base_rules) for a in aggs]

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
    "WindowAggregate",
    "align_down",
    "build_window",
    "closed_window_limit",
    "consolidate",
    "fetch_aggregates",
    "plan_range",
    "upsert_windows",
]
