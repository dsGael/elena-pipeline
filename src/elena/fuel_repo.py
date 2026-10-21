"""Lecturas de solo lectura de la base que comparten el pipeline y los comandos.

SQL manual (sin ORM). Nada aquí escribe en la base.
"""

from __future__ import annotations

from collections.abc import Iterable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, tzinfo
from typing import Any

from psycopg import Connection

from .calibration import CalibrationTable
from .processor import ProcesadorConfig
from .rendimiento import (
    BloqueRendimiento,
    PuntoRendimiento,
    RendimientoReciente,
    calcular_bloques,
    estimar_rendimiento_reciente,
)

CONFIG_SQL = """
SELECT calibracion, calibracion_version, umbral_carga_dudosa, umbral_carga_candidata,
       umbral_carga_confirmada, umbral_extraccion, velocidad_max_carga,
       ventanas_sostenidas, rendimiento_default,
       concordancia_ventanas, concordancia_tolerancia, concordancia_max_minutos
FROM public.fuel_config
WHERE idautobus = %(idautobus)s
"""

# Rendimiento más reciente del autobús (lo escribe el run-cycle; ver sql/12).
RENDIMIENTO_RECIENTE_SQL = """
SELECT rendimiento_km_l
FROM public.fuel_rendimiento_reciente
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


def rendimiento_guardado(conn: Connection, idautobus: str) -> float | None:
    """Rendimiento (km/L) de ``fuel_rendimiento_reciente``; None si no hay un valor usable."""
    row = conn.execute(RENDIMIENTO_RECIENTE_SQL, {"idautobus": idautobus}).fetchone()
    valor = _f(row[0]) if row else None
    return valor if valor is not None and valor > 0 else None


def cargar_config(
    conn: Connection, idautobus: str, *, rendimiento_de_tabla: bool = False
) -> ProcesadorConfig:
    """Config del autobús desde fuel_config.

    Con ``rendimiento_de_tabla`` el rendimiento sale de ``fuel_rendimiento_reciente`` y
    ``fuel_config.rendimiento_default`` queda solo como respaldo (si la tabla no tiene un valor).
    Quien estima el rendimiento NO debe pedirlo así: dependería de su propio resultado.
    """
    row = conn.execute(CONFIG_SQL, {"idautobus": idautobus}).fetchone()
    if row is None:
        raise ConfigError(f"El autobús {idautobus!r} no tiene fila en fuel_config.")
    (
        calib,
        version,
        dudosa,
        candidata,
        confirmada,
        extraccion,
        vmax,
        sost,
        rend,
        conc_k,
        conc_tol,
        conc_min,
    ) = row
    cfg = ProcesadorConfig(
        calibracion=CalibrationTable.from_json(calib),
        calibracion_version=int(version),
        umbral_carga_dudosa=float(dudosa),
        umbral_carga_candidata=float(candidata),
        umbral_carga_confirmada=float(confirmada),
        umbral_extraccion=float(extraccion),
        velocidad_max_carga=_f(vmax),
        ventanas_sostenidas=None if sost is None else int(sost),
        rendimiento_default=_f(rend),
        rendimiento_origen="config" if _f(rend) is not None else None,
        concordancia_ventanas=None if conc_k is None else int(conc_k),
        concordancia_tolerancia=_f(conc_tol),
        concordancia_max_minutos=_f(conc_min),
    )
    if rendimiento_de_tabla:
        reciente = rendimiento_guardado(conn, idautobus)
        if reciente is not None:
            cfg = replace(
                cfg, rendimiento_default=reciente, rendimiento_origen="reciente"
            )
    return cfg


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
