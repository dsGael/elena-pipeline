"""Lecturas de solo lectura de la base que comparten el pipeline y los comandos.

SQL manual (sin ORM). Nada aquí escribe en la base.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, tzinfo
from typing import Any

from psycopg import Connection

from .calibration import CalibrationTable
from .processor import ProcesadorConfig
from .rendimiento import PuntoRendimiento

CONFIG_SQL = """
SELECT calibracion, calibracion_version, umbral_carga_dudosa, umbral_carga_candidata,
       umbral_carga_confirmada, umbral_extraccion, velocidad_max_carga,
       ventanas_sostenidas, rendimiento_default
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
    """El autobús no tiene fuel_config o su calibración es inválida."""


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


def cargar_config(conn: Connection, idautobus: str) -> ProcesadorConfig:
    row = conn.execute(CONFIG_SQL, {"idautobus": idautobus}).fetchone()
    if row is None:
        raise ConfigError(f"El autobús {idautobus!r} no tiene fila en fuel_config.")
    (calib, version, dudosa, candidata, confirmada, extraccion, vmax, sost, rend) = row
    return ProcesadorConfig(
        calibracion=CalibrationTable.from_json(calib),
        calibracion_version=int(version),
        umbral_carga_dudosa=float(dudosa),
        umbral_carga_candidata=float(candidata),
        umbral_carga_confirmada=float(confirmada),
        umbral_extraccion=float(extraccion),
        velocidad_max_carga=_f(vmax),
        ventanas_sostenidas=None if sost is None else int(sost),
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


# --------------------------------------------------------------------------------------
# Eventos (solo lectura)
# --------------------------------------------------------------------------------------

EVENTOS_SQL = """
SELECT e.fecha_inicio, e.fecha_fin, e.idautobus, e.tipo_evento, e.estado,
       e.litros_antes, e.litros_despues, e.litros_evento,
       e.latitude, e.longitude, e.calidad_evento, e.motivo
FROM public.fuel_events e
WHERE (%(bus)s::text IS NULL OR e.idautobus = %(bus)s::text)
  AND (%(desde)s::timestamptz IS NULL OR e.fecha_inicio >= %(desde)s::timestamptz)
  AND (%(hasta)s::timestamptz IS NULL OR e.fecha_inicio <  %(hasta)s::timestamptz)
  AND (%(estado)s::text IS NULL OR e.estado = %(estado)s::text)
  AND (%(tipo)s::text IS NULL OR e.tipo_evento = %(tipo)s::text)
ORDER BY e.fecha_inicio DESC, e.created_at DESC
LIMIT %(limite)s
"""


@dataclass(frozen=True, slots=True)
class EventoFila:
    fecha_inicio: datetime
    fecha_fin: datetime | None
    idautobus: str
    tipo_evento: str
    estado: str
    litros_antes: float
    litros_despues: float
    litros_evento: float
    latitude: float | None
    longitude: float | None
    calidad_evento: str | None
    motivo: str | None


def listar_eventos(
    conn: Connection,
    *,
    idautobus: str | None = None,
    desde: datetime | None = None,
    hasta: datetime | None = None,
    estado: str | None = None,
    tipo: str | None = None,
    limite: int = 50,
) -> list[EventoFila]:
    filas = conn.execute(
        EVENTOS_SQL,
        {
            "bus": idautobus,
            "desde": desde,
            "hasta": hasta,
            "estado": estado,
            "tipo": tipo,
            "limite": limite,
        },
    ).fetchall()
    return [
        EventoFila(
            fecha_inicio=r[0],
            fecha_fin=r[1],
            idautobus=r[2],
            tipo_evento=r[3],
            estado=r[4],
            litros_antes=float(r[5]),
            litros_despues=float(r[6]),
            litros_evento=float(r[7]),
            latitude=_f(r[8]),
            longitude=_f(r[9]),
            calidad_evento=r[10],
            motivo=r[11],
        )
        for r in filas
    ]


def formatear_eventos(filas: Sequence[EventoFila], tz: tzinfo) -> list[str]:
    """Tabla de texto (una línea por evento) con las horas en la zona ``tz``."""
    if not filas:
        return ["(sin eventos)"]
    cab = (
        f"{'inicio (local)':<16} {'bus':<6} {'tipo':<34} {'estado':<11} "
        f"{'antes':>6} {'despues':>7} {'litros':>7}  ubicación / motivo"
    )
    out = [cab]
    for e in filas:
        ini = e.fecha_inicio.astimezone(tz).strftime("%Y-%m-%d %H:%M")
        ubic = (
            f"{e.latitude:.5f},{e.longitude:.5f}"
            if e.latitude is not None and e.longitude is not None
            else "sin posición"
        )
        out.append(
            f"{ini:<16} {e.idautobus:<6} {e.tipo_evento:<34} {e.estado:<11} "
            f"{e.litros_antes:>6.2f} {e.litros_despues:>7.2f} {e.litros_evento:>7.2f}  "
            f"{ubic}  {e.motivo or ''}"
        )
    return out