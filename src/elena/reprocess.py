"""Reprocesar un autobús: borra lo DERIVADO y deja que el pipeline lo recalcule.

Sirve cuando cambia un parámetro (umbrales, rendimiento, ventanas_sostenidas), la calibración o
se re-consolidaron ventanas. Solo toca fuel_processed_result, fuel_events y
fuel_estado_confiable de UN autobús; nunca gps_records ni fuel_windows.

  * Sin ``desde``: se borra todo lo derivado del autobús y se recalcula desde la primera ventana.
  * Con ``desde``: se borran los resultados con ventana_inicio >= desde y los eventos que
    empiezan en ese rango (o cuya ventana de origen cae en él). El estado confiable se
    restaura al último valor actualizado ANTES de ``desde``. Un pendiente (carga/extracción en
    curso) que cruce ``desde`` se reinicia: la persistencia vuelve a contarse desde cero.

Es una operación en una transacción con el lock advisory del autobús: si otro proceso lo está
procesando, no hace nada. La re-consolidación de ventanas es un paso aparte
(``elena-fuel consolidate --desde ...``): este módulo recalcula con las ventanas que ya existen.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

from psycopg import Connection

from .pipeline_repo import PgRepositorio, try_lock_autobus
from .processor import EstadoConfiable

CONTAR_RESULTADOS_SQL = """
SELECT count(*) FROM public.fuel_processed_result
WHERE idautobus = %(bus)s
  AND (%(desde)s::timestamptz IS NULL OR ventana_inicio >= %(desde)s::timestamptz)
"""

# Eventos a borrar: empiezan en el rango, o fueron confirmados/creados por una ventana del rango.
CONTAR_EVENTOS_SQL = """
SELECT count(*) FROM public.fuel_events e
WHERE e.idautobus = %(bus)s
  AND (%(desde)s::timestamptz IS NULL
       OR e.fecha_inicio >= %(desde)s::timestamptz
       OR e.ventana_origen_id IN (
            SELECT w.id FROM public.fuel_windows w
            WHERE w.ventana_inicio >= %(desde)s::timestamptz))
"""

BORRAR_EVENTOS_SQL = CONTAR_EVENTOS_SQL.replace(
    "SELECT count(*) FROM public.fuel_events e",
    "DELETE FROM public.fuel_events e",
)

BASE_SQL = """
SELECT r.imei, r.litros_finales, r.ventana_inicio, w.odometro_total
FROM public.fuel_processed_result r
JOIN public.fuel_windows w ON w.id = r.ventana_id
WHERE r.idautobus = %(bus)s
  AND r.actualizo_final
  AND r.litros_finales IS NOT NULL
  AND r.ventana_inicio < %(desde)s::timestamptz
ORDER BY r.ventana_inicio DESC
LIMIT 1
"""


class AutobusOcupado(RuntimeError):
    """Otra instancia tiene el lock del autobús."""


@dataclass(frozen=True, slots=True)
class PlanReproceso:
    resultados: int
    eventos: int
    base: EstadoConfiable | None  # estado al que se restaura (None = empezar de cero)
    base_imei: str | None = None


def _base(
    conn: Connection, idautobus: str, desde: datetime
) -> tuple[EstadoConfiable | None, str | None]:
    row = conn.execute(BASE_SQL, {"bus": idautobus, "desde": desde}).fetchone()
    if row is None:
        return None, None
    imei, litros, inicio, odo = row
    return (
        EstadoConfiable(
            ultimo_litro_confiable=float(litros),
            fecha_ultimo_confiable=inicio,
            odometro_ultimo_confiable=None if odo is None else float(odo),
        ),
        imei,
    )


def planificar(
    conn: Connection, idautobus: str, desde: datetime | None
) -> PlanReproceso:
    """Cuenta lo que se borraría y calcula el estado base. Solo lectura."""
    params = {"bus": idautobus, "desde": desde}
    n_res = conn.execute(CONTAR_RESULTADOS_SQL, params).fetchone()[0]
    n_ev = conn.execute(CONTAR_EVENTOS_SQL, params).fetchone()[0]
    base, imei = (None, None) if desde is None else _base(conn, idautobus, desde)
    return PlanReproceso(int(n_res), int(n_ev), base, imei)


def reiniciar(
    conn: Connection, idautobus: str, desde: datetime | None
) -> PlanReproceso:
    """Borra lo derivado y restaura el estado base. Una sola transacción con lock.

    Después hay que correr el pipeline (``ejecutar_pipeline``) para recalcular.
    """
    conn.commit()  # transacción limpia: el lock y los borrados se confirman juntos
    with conn.transaction():
        if not try_lock_autobus(conn, idautobus):
            raise AutobusOcupado(f"{idautobus}: otra instancia lo está procesando.")
        plan = planificar(conn, idautobus, desde)
        params = {"bus": idautobus, "desde": desde}
        conn.execute(BORRAR_EVENTOS_SQL, params)
        conn.execute(
            "DELETE FROM public.fuel_processed_result WHERE idautobus = %(bus)s "
            "AND (%(desde)s::timestamptz IS NULL OR ventana_inicio >= %(desde)s::timestamptz)",
            params,
        )
        if plan.base is None:
            conn.execute(
                "DELETE FROM public.fuel_estado_confiable WHERE idautobus = %s",
                (idautobus,),
            )
        else:
            PgRepositorio(conn).guardar_estado(
                idautobus, plan.base_imei or "", plan.base
            )
    return plan