"""Detección de eventos por autobús -> fuel_events.

Qué detecta en [desde, hasta) (con ``CONTEXTO`` hacia atrás para ver el inicio de lo ya empezado)
  * carga / extracción: rachas de ventanas estacionadas en las que ``litros_grafica`` cambia.
    Estacionado, ``litros_grafica`` solo se mueve con un salto real (ver
    ``consolidation.litros_para_grafica``), así que el cambio neto de la racha es lo cargado o
    extraído. Cuenta si llega a ``fuel_config.umbral_carga_candidata``.
  * aceleración / frenado / giro brusco: cada registro con ``gps_records.maniobra`` 1-3 (AVL 253).
  * ralentí: muestras seguidas con la ignición encendida y quieto (``movimiento = 0`` o
    velocidad < ``VELOCIDAD_RALENTI_KMH``) durante al menos ``fuel_config.minutos_ralenti``.
    Es "o" y no "y": el Teltonika puede marcar movimiento con el motor encendido (fuente de
    movimiento = ignición, o la vibración del diésel en el acelerómetro).

El upsert es idempotente por (idautobus, fecha, evento): un ralentí o una carga que sigue en curso
al cerrar el ciclo se guarda con lo que lleva y el siguiente ciclo actualiza la cantidad.
"""

from __future__ import annotations

from collections import Counter
from collections.abc import Iterable
from datetime import datetime, timedelta
from itertools import groupby, pairwise
from operator import attrgetter
from typing import Any, NamedTuple

from psycopg import Connection

from .consolidation import ODOMETRO_PARADO_M
from .fuel_repo import _POS_INICIO

# ~ ODOMETRO_PARADO_M en 5 min (3.6 km/h): por debajo es deriva del GPS o maniobra.
VELOCIDAD_RALENTI_KMH = 3.0
# Más tiempo sin muestras corta el ralentí (equipo apagado o sin datos). Por calibrar con el
# intervalo real que manda el GPS encendido y quieto.
HUECO_MAX_RALENTI = timedelta(minutes=30)
# shortcut: un evento que empezó más de 12 h antes de ``desde`` no se ve completo y se descarta;
# ampliar si aparecen ralentís más largos.
CONTEXTO = timedelta(hours=12)
MANIOBRAS = {1: "aceleracion", 2: "frenado", 3: "giro_brusco"}

# --------------------------------------------------------------------------------------
# SQL
# --------------------------------------------------------------------------------------

_CFG = """
WITH cfg AS (
    SELECT d.imei, d.idautobus, c.minutos_ralenti, c.umbral_carga_candidata
    FROM public.dispositivos d
    JOIN public.fuel_config c ON c.idautobus = d.idautobus
    WHERE d.imei IS NOT NULL
      AND c.activo
      AND (%(imei)s::text IS NULL OR d.imei = %(imei)s::text)
)"""

# movimiento::int: igual si la columna es boolean o 0/1.
MUESTRAS_SQL = (
    _CFG
    + """
SELECT DISTINCT ON (g.imei, g.recorded_at)
       g.imei, cfg.idautobus, g.recorded_at, g.ignicion, g.speed,
       g.movimiento::int, g.maniobra, cfg.minutos_ralenti
FROM public.gps_records g
JOIN cfg ON cfg.imei = g.imei
WHERE g.recorded_at >= %(desde)s
  AND g.recorded_at <  %(hasta)s
ORDER BY g.imei, g.recorded_at, g.created_at DESC NULLS LAST
"""
)

# Sin odómetro no se sabe si está estacionado (mismo criterio que PREVIAS_SQL).
VENTANAS_SQL = (
    _CFG
    + """
SELECT w.imei, cfg.idautobus, w.ventana_inicio, w.odometro_total, w.litros_grafica,
       cfg.umbral_carga_candidata
FROM public.fuel_windows w
JOIN cfg ON cfg.imei = w.imei
WHERE w.ventana_inicio >= %(desde)s
  AND w.ventana_inicio <  %(hasta)s
  AND w.odometro_total IS NOT NULL
ORDER BY w.imei, w.ventana_inicio
"""
)

UPSERT_EVENTO_SQL = (
    """
INSERT INTO public.fuel_events (idautobus, fecha, evento, cantidad, ubicacion)
SELECT %(bus)s::varchar, %(fecha)s::timestamptz, %(evento)s::varchar, %(cantidad)s::numeric,
       p_ini.coord
FROM (SELECT 1) x"""
    + _POS_INICIO.format(t="%(fecha)s::timestamptz")
    + """
ON CONFLICT (idautobus, fecha, evento) DO UPDATE SET cantidad = EXCLUDED.cantidad
WHERE fuel_events.cantidad IS DISTINCT FROM EXCLUDED.cantidad
"""
)

# --------------------------------------------------------------------------------------
# Modelos
# --------------------------------------------------------------------------------------


class Muestra(NamedTuple):
    imei: str
    idautobus: str
    recorded_at: datetime
    ignicion: bool | None
    speed: float | None
    movimiento: int | None
    maniobra: int | None
    minutos_ralenti: int


class Ventana(NamedTuple):
    imei: str
    idautobus: str
    ventana_inicio: datetime
    odometro_total: float
    litros_grafica: float | None
    umbral: float | None  # fuel_config.umbral_carga_candidata


class Evento(NamedTuple):
    idautobus: str
    fecha: datetime  # inicio
    evento: str
    # litros (carga/extracción), minutos (ralentí), None (maniobras)
    cantidad: float | None


def _f(value: Any) -> float | None:
    return None if value is None else float(value)


# --------------------------------------------------------------------------------------
# Lógica (pura)
# --------------------------------------------------------------------------------------


def maniobras(muestras: Iterable[Muestra]) -> list[Evento]:
    return [
        Evento(m.idautobus, m.recorded_at, MANIOBRAS[m.maniobra], None)
        for m in muestras
        if m.maniobra in MANIOBRAS
    ]


def _quieta(m: Muestra) -> bool:
    return m.ignicion is True and (
        m.movimiento == 0 or (m.speed is not None and m.speed < VELOCIDAD_RALENTI_KMH)
    )


def ralentis(muestras: Iterable[Muestra]) -> list[Evento]:
    """``muestras`` en orden (imei, recorded_at)."""
    out: list[Evento] = []
    for _, grupo in groupby(muestras, key=attrgetter("imei")):
        serie = list(grupo)
        islas: list[list[Muestra]] = []
        for previa, m in zip([None, *serie], serie):
            if not _quieta(m):
                continue
            if (
                previa is None
                or not _quieta(previa)
                or m.recorded_at - previa.recorded_at > HUECO_MAX_RALENTI
            ):
                islas.append([])
            islas[-1].append(m)
        for isla in islas:
            if isla[0] is serie[0]:
                continue  # pudo empezar antes de lo leído
            minutos = (isla[-1].recorded_at - isla[0].recorded_at).total_seconds() / 60
            if minutos >= isla[0].minutos_ralenti:
                out.append(
                    Evento(
                        isla[0].idautobus,
                        isla[0].recorded_at,
                        "ralenti",
                        round(minutos, 1),
                    )
                )
    return out


def _cambio_estacionado(a: Ventana, b: Ventana) -> float:
    """Cambio de ``litros_grafica`` de ``a`` a ``b`` si está estacionado; si no, 0."""
    if (
        a.litros_grafica is None
        or b.litros_grafica is None
        or not 0 <= b.odometro_total - a.odometro_total < ODOMETRO_PARADO_M
    ):
        return 0.0
    return b.litros_grafica - a.litros_grafica


def cargas_extracciones(ventanas: Iterable[Ventana]) -> list[Evento]:
    """``ventanas`` en orden (imei, ventana_inicio). Una racha son ventanas estacionadas seguidas
    con cambio de nivel; una sin cambio o en marcha la cierra."""
    out: list[Evento] = []
    for _, grupo in groupby(ventanas, key=attrgetter("imei")):
        serie = list(grupo)
        rachas: list[list[tuple[Ventana, float]]] = []
        seguida = False
        for a, b in pairwise(serie):
            cambio = _cambio_estacionado(a, b)
            if cambio and not seguida:
                rachas.append([])
            if cambio:
                rachas[-1].append((b, cambio))
            seguida = bool(cambio)
        for racha in rachas:
            inicio = racha[0][0]
            if inicio is serie[1]:
                continue  # el primer par: pudo empezar antes de lo leído
            neto = sum(c for _, c in racha)
            if inicio.umbral is not None and abs(neto) >= inicio.umbral:
                evento = "carga" if neto > 0 else "extraccion"
                out.append(
                    Evento(
                        inicio.idautobus,
                        inicio.ventana_inicio,
                        evento,
                        round(abs(neto), 2),
                    )
                )
    return out


# --------------------------------------------------------------------------------------
# Base
# --------------------------------------------------------------------------------------


def detectar_eventos(
    conn: Connection, desde: datetime, hasta: datetime, *, imei: str | None = None
) -> Counter[str]:
    """Detecta y guarda (upsert) los eventos. Devuelve cuántos de cada tipo empiezan en
    [desde, hasta). No hace commit: lo decide quien llama."""
    params = {"desde": desde - CONTEXTO, "hasta": hasta, "imei": imei}
    muestras = [
        Muestra(i, bus, t, ign, _f(vel), mov, man, mr)
        for i, bus, t, ign, vel, mov, man, mr in conn.execute(MUESTRAS_SQL, params)
    ]
    ventanas = [
        Ventana(i, bus, t, float(odo), _f(lg), _f(umbral))
        for i, bus, t, odo, lg, umbral in conn.execute(VENTANAS_SQL, params)
    ]
    eventos = maniobras(muestras) + ralentis(muestras) + cargas_extracciones(ventanas)
    if eventos:
        with conn.cursor() as cur:
            cur.executemany(
                UPSERT_EVENTO_SQL,
                [
                    {
                        "bus": e.idautobus,
                        "fecha": e.fecha,
                        "evento": e.evento,
                        "cantidad": e.cantidad,
                    }
                    for e in eventos
                ],
            )
    return Counter(e.evento for e in eventos if e.fecha >= desde)


__all__ = [
    "CONTEXTO",
    "Evento",
    "Muestra",
    "Ventana",
    "cargas_extracciones",
    "detectar_eventos",
    "maniobras",
    "ralentis",
]
