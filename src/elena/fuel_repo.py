"""Acceso a la base para el rendimiento (km/L): config, puntos y escritura de resultados.

SQL manual (sin ORM). Lee fuel_config, fuel_windows, dispositivos y gps_records; escribe solo en
fuel_rendimiento_reciente y fuel_rendimiento_historial.
"""

from __future__ import annotations

from collections.abc import Iterable
from dataclasses import dataclass
from datetime import datetime, timedelta
from typing import Any

from psycopg import Connection

from .calibration import CalibrationTable
from .rendimiento import (
    BloqueRendimiento,
    PuntoRendimiento,
    RendimientoReciente,
    calcular_bloques,
    estimar_rendimiento_reciente,
)

CONFIG_SQL = """
SELECT calibracion, umbral_carga_candidata, rendimiento_default
FROM public.fuel_config
WHERE idautobus = %(idautobus)s
"""

# Ventanas válidas de un autobús (puede haber cambiado de GPS: se une por dispositivos).
VENTANAS_VALIDAS_SQL = """
SELECT w.ventana_inicio, w.sensor_mediana, w.odometro_total
FROM public.fuel_windows w
JOIN public.dispositivos d ON d.imei = w.imei
WHERE d.idautobus = %(idautobus)s
  AND w.calidad_ventana = 'valida'
  AND w.ventana_inicio >= %(desde)s
  AND w.ventana_inicio <  %(hasta)s
ORDER BY w.ventana_inicio
"""


class ConfigError(RuntimeError):
    """El autobús no tiene fuel_config."""


@dataclass(frozen=True, slots=True)
class ConfigRendimiento:
    calibracion: CalibrationTable
    # Salto de nivel que corta un tramo (carga/extracción).
    umbral_carga_candidata: float
    rendimiento_default: float | None = None  # km/L esperado para atravesar huecos
    # Factor para pasar odometro_total a km. gps_records lo guarda en METROS (Teltonika).
    odometro_a_km: float = 0.001


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def cargar_config(conn: Connection, idautobus: str) -> ConfigRendimiento:
    """Config del autobús desde fuel_config."""
    row = conn.execute(CONFIG_SQL, {"idautobus": idautobus}).fetchone()
    if row is None:
        raise ConfigError(f"El autobús {idautobus!r} no tiene fila en fuel_config.")
    calib, candidata, rend = row
    return ConfigRendimiento(
        calibracion=CalibrationTable.from_json(calib),
        umbral_carga_candidata=float(candidata),
        rendimiento_default=_f(rend),
    )


def puntos_desde_filas(
    filas: Iterable[tuple[datetime, Any, Any]], calibracion: CalibrationTable
) -> list[PuntoRendimiento]:
    """(ventana_inicio, sensor_mediana, odometro_total) -> puntos con litros.

    Se omiten las ventanas cuyo sensor no cae dentro de la calibración (zona baja o fuera).
    """
    puntos: list[PuntoRendimiento] = []
    for inicio, sensor, odo in filas:
        conv = calibracion.to_liters(sensor)
        if conv.utilizable and conv.litros is not None:
            puntos.append(PuntoRendimiento(inicio, round(conv.litros, 2), _f(odo)))
    return puntos


def cargar_puntos_rendimiento(
    conn: Connection,
    idautobus: str,
    desde: datetime,
    hasta: datetime,
    calibracion: CalibrationTable,
) -> list[PuntoRendimiento]:
    filas = conn.execute(
        VENTANAS_VALIDAS_SQL, {"idautobus": idautobus, "desde": desde, "hasta": hasta}
    ).fetchall()
    return puntos_desde_filas(filas, calibracion)


AUTOBUSES_ACTIVOS_SQL = """
SELECT idautobus FROM public.fuel_config
WHERE activo AND (%(bus)s::text IS NULL OR idautobus = %(bus)s::text)
ORDER BY idautobus
"""


def autobuses_activos(conn: Connection, idautobus: str | None = None) -> list[str]:
    return [r[0] for r in conn.execute(AUTOBUSES_ACTIVOS_SQL, {"bus": idautobus})]


def rendimiento_reciente(
    conn: Connection,
    idautobus: str,
    *,
    ahora: datetime,
    km_ventana: float = 100.0,
    dias: int = 30,
) -> RendimientoReciente:
    """Rendimiento del autobús en sus últimos ``km_ventana`` km (mira ``dias`` hacia atrás).

    Usa los umbrales ya configurados del autobús: ``umbral_carga_candidata`` como salto de nivel
    y ``rendimiento_default`` (si existe) como consumo esperado al atravesar huecos.
    """
    cfg = cargar_config(conn, idautobus)
    puntos = cargar_puntos_rendimiento(
        conn, idautobus, ahora - timedelta(days=dias), ahora, cfg.calibracion
    )
    return estimar_rendimiento_reciente(
        puntos,
        km_ventana=km_ventana,
        odometro_a_km=cfg.odometro_a_km,
        salto_litros=cfg.umbral_carga_candidata,
        rendimiento_esperado=cfg.rendimiento_default,
    )


# --------------------------------------------------------------------------------------
# Historial de rendimiento por bloques
# --------------------------------------------------------------------------------------

ULTIMO_FIN_HISTORIAL_SQL = """
SELECT max(fecha_fin) FROM public.fuel_rendimiento_historial WHERE idautobus = %s
"""

# Las últimas ventanas válidas ANTES de un instante: contexto para detectar cargas al retomar.
VENTANAS_PREVIAS_SQL = """
SELECT w.ventana_inicio, w.sensor_mediana, w.odometro_total
FROM public.fuel_windows w
JOIN public.dispositivos d ON d.imei = w.imei
WHERE d.idautobus = %(idautobus)s
  AND w.calidad_ventana = 'valida'
  AND w.ventana_inicio < %(antes)s
ORDER BY w.ventana_inicio DESC
LIMIT %(n)s
"""

# Con k_salto=5 y recorte=1 hacen falta 11 lecturas; sobra margen por las que la calibración omite.
CONTEXTO_VENTANAS = 24


def ultimo_fin_historial(conn: Connection, idautobus: str) -> datetime | None:
    row = conn.execute(ULTIMO_FIN_HISTORIAL_SQL, (idautobus,)).fetchone()
    return row[0] if row else None


def cargar_puntos_previos(
    conn: Connection,
    idautobus: str,
    antes: datetime,
    n: int,
    calibracion: CalibrationTable,
) -> list[PuntoRendimiento]:
    filas = conn.execute(
        VENTANAS_PREVIAS_SQL, {"idautobus": idautobus, "antes": antes, "n": n}
    ).fetchall()
    return puntos_desde_filas(reversed(filas), calibracion)


def bloques_rendimiento_nuevos(
    conn: Connection,
    idautobus: str,
    *,
    ahora: datetime,
    km_bloque: float = 50.0,
    dias_inicial: int = 30,
    desde: datetime | None = None,
) -> list[BloqueRendimiento]:
    """Bloques definitivos que aún no están en el historial del autobús.

    Retoma en el ``fin`` del último bloque guardado. Si el autobús no tiene historial, empieza
    en ``desde`` (para rellenar hacia atrás) o, si no se indica, ``dias_inicial`` días atrás.
    """
    cfg = cargar_config(conn, idautobus)
    ancla = ultimo_fin_historial(conn, idautobus)
    inicio = ancla or desde or ahora - timedelta(days=dias_inicial)
    puntos = cargar_puntos_rendimiento(conn, idautobus, inicio, ahora, cfg.calibracion)
    if ancla is not None:
        previos = cargar_puntos_previos(
            conn, idautobus, ancla, CONTEXTO_VENTANAS, cfg.calibracion
        )
        puntos = previos + puntos
    return calcular_bloques(
        puntos,
        km_bloque=km_bloque,
        desde=ancla,
        odometro_a_km=cfg.odometro_a_km,
        salto_litros=cfg.umbral_carga_candidata,
        rendimiento_esperado=cfg.rendimiento_default,
    )


# fuel_windows no guarda posición: se toma de gps_records dentro de la ventana de 5 min que
# empieza en el instante dado (la primera muestra para el inicio, la última para el fin).
# Se guarda como texto 'lat,lon' (5 decimales). Si no hay muestra con coordenadas, queda NULL.
_POS_INICIO = """
LEFT JOIN LATERAL (
    SELECT concat(round(g.latitude::numeric, 5), ',', round(g.longitude::numeric, 5)) AS coord
    FROM public.gps_records g
    JOIN public.dispositivos d ON d.imei = g.imei
    WHERE d.idautobus = %(bus)s
      AND g.recorded_at >= {t} AND g.recorded_at < {t} + interval '5 minutes'
      AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL
    ORDER BY g.recorded_at ASC LIMIT 1
) p_ini ON true"""
_POS_FIN = """
LEFT JOIN LATERAL (
    SELECT concat(round(g.latitude::numeric, 5), ',', round(g.longitude::numeric, 5)) AS coord
    FROM public.gps_records g
    JOIN public.dispositivos d ON d.imei = g.imei
    WHERE d.idautobus = %(bus)s
      AND g.recorded_at >= {t} AND g.recorded_at < {t} + interval '5 minutes'
      AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL
    ORDER BY g.recorded_at DESC LIMIT 1
) p_fin ON true"""


GUARDAR_RENDIMIENTO_RECIENTE_SQL = (
    """
INSERT INTO public.fuel_rendimiento_reciente (
    idautobus, calculado_en, km_ventana, rendimiento_km_l, error_pct, km, litros,
    puntos, segmentos, desde, hasta, motivo,
    coordenadas_inicio, coordenadas_fin)
SELECT %(bus)s::varchar, now(), %(km_ventana)s::numeric, %(rend)s::numeric,
       %(error)s::numeric, %(km)s::numeric, %(litros)s::numeric,
       %(puntos)s::integer, %(segmentos)s::integer, %(desde)s::timestamptz,
       %(hasta)s::timestamptz, %(motivo)s::text,
       p_ini.coord, p_fin.coord
FROM (SELECT 1) x"""
    + _POS_INICIO.format(t="%(desde)s::timestamptz")
    + _POS_FIN.format(t="%(hasta)s::timestamptz")
    + """
ON CONFLICT (idautobus) DO UPDATE SET
    calculado_en = now(),
    km_ventana = EXCLUDED.km_ventana,
    rendimiento_km_l = EXCLUDED.rendimiento_km_l,
    error_pct = EXCLUDED.error_pct,
    km = EXCLUDED.km,
    litros = EXCLUDED.litros,
    puntos = EXCLUDED.puntos,
    segmentos = EXCLUDED.segmentos,
    desde = EXCLUDED.desde,
    hasta = EXCLUDED.hasta,
    motivo = EXCLUDED.motivo,
    coordenadas_inicio = EXCLUDED.coordenadas_inicio,
    coordenadas_fin = EXCLUDED.coordenadas_fin
"""
)

# Sin estimación: se conserva el último valor bueno y solo se avisa el motivo.
SIN_ESTIMACION_RENDIMIENTO_RECIENTE_SQL = """
INSERT INTO public.fuel_rendimiento_reciente (idautobus, calculado_en, km_ventana, km, puntos, segmentos, motivo)
VALUES (%(bus)s, now(), %(km_ventana)s, 0, 0, 0, %(motivo)s)
ON CONFLICT (idautobus) DO UPDATE SET calculado_en = now(), motivo = EXCLUDED.motivo
"""


def guardar_rendimiento_reciente(
    conn: Connection, idautobus: str, est: RendimientoReciente, km_ventana: float
) -> None:
    """Una fila por autobús con su rendimiento más reciente (la lee el dashboard)."""
    if est.rendimiento_km_l is None:
        conn.execute(
            SIN_ESTIMACION_RENDIMIENTO_RECIENTE_SQL,
            {"bus": idautobus, "km_ventana": km_ventana, "motivo": est.motivo},
        )
        return
    conn.execute(
        GUARDAR_RENDIMIENTO_RECIENTE_SQL,
        {
            "bus": idautobus,
            "km_ventana": km_ventana,
            "rend": est.rendimiento_km_l,
            "error": est.error_pct,
            "km": est.km,
            "litros": est.litros,
            "puntos": est.puntos,
            "segmentos": est.segmentos,
            "desde": est.desde,
            "hasta": est.hasta,
            "motivo": None,
        },
    )


INSERTAR_BLOQUE_SQL = (
    """
INSERT INTO public.fuel_rendimiento_historial (
    idautobus, fecha_inicio, fecha_fin, odometro_inicio_km, odometro_fin_km, km_recorridos,
    litros_inicio, litros_fin, litros_consumidos, rendimiento_km_l, error_pct, puntos,
    cierre, completo, estado, km_bloque,
    coordenadas_inicio, coordenadas_fin)
SELECT %(bus)s::varchar, %(inicio)s::timestamptz, %(fin)s::timestamptz,
       %(odo_ini)s::numeric, %(odo_fin)s::numeric, %(km)s::numeric,
       %(l_ini)s::numeric, %(l_fin)s::numeric, %(litros)s::numeric, %(rend)s::numeric,
       %(error)s::numeric, %(puntos)s::integer,
       %(cierre)s::text, %(completo)s::boolean, %(estado)s::text, %(km_bloque)s::numeric,
       p_ini.coord, p_fin.coord
FROM (SELECT 1) x"""
    + _POS_INICIO.format(t="%(inicio)s::timestamptz")
    + _POS_FIN.format(t="%(fin)s::timestamptz")
    + """
ON CONFLICT (idautobus, fecha_inicio) DO NOTHING
"""
)


def guardar_bloques(
    conn: Connection,
    idautobus: str,
    bloques: list[BloqueRendimiento],
    km_bloque: float,
) -> int:
    """Agrega bloques al historial (idempotente). Devuelve cuántos eran nuevos."""
    nuevos = 0
    for b in bloques:
        cur = conn.execute(
            INSERTAR_BLOQUE_SQL,
            {
                "bus": idautobus,
                "inicio": b.inicio,
                "fin": b.fin,
                "odo_ini": b.odometro_inicio_km,
                "odo_fin": b.odometro_fin_km,
                "km": b.km,
                "l_ini": b.litros_inicio,
                "l_fin": b.litros_fin,
                "litros": b.litros_consumidos,
                "rend": b.rendimiento_km_l,
                "error": b.error_pct,
                "puntos": b.puntos,
                "cierre": b.cierre,
                "completo": b.completo,
                "estado": b.estado,
                "km_bloque": km_bloque,
            },
        )
        nuevos += cur.rowcount
    return nuevos
