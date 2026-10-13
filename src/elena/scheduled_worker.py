"""Ciclo programado (Windows Task Scheduler): consolidar ventanas y procesarlas.

Un ciclo hace, en este orden:
  1. Toma un lock advisory de SESIÓN (``elena_fuel`` / ``ciclo``). Si otro ciclo ya corre, sale
     sin hacer nada (código 0): el Task Scheduler puede lanzar una segunda instancia.
  2. Consolida gps_records -> fuel_windows desde la última ventana guardada (con traslape para
     datos tardíos) hasta la última ventana cerrada, y confirma.
  3. Procesa las ventanas nuevas de cada autobús (pipeline.ejecutar_pipeline).
  4. Libera el lock, también si algo falla.

Códigos de salida: 0 = bien u omitido por ciclo en curso; 1 = error en algún autobús o fallo del
ciclo; 2 = no hay punto de partida (fuel_windows vacía: corre antes ``consolidate --desde``).
"""

from __future__ import annotations

import logging
from dataclasses import dataclass, field
from datetime import UTC, datetime
from logging.handlers import RotatingFileHandler
from pathlib import Path

from psycopg import Connection

from .consolidation import (
    WINDOW_MINUTES,
    ConsolidationSummary,
    NoStartError,
    consolidate,
    plan_range,
)
from .pipeline import ResumenPipeline, ejecutar_pipeline
from .quality import QualityRules

log = logging.getLogger("elena.worker")

LOCK_NAMESPACE = "elena_fuel"
LOCK_CICLO = "ciclo"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_SIN_INICIO = 2


@dataclass(slots=True)
class ResultadoCiclo:
    codigo: int = EXIT_OK
    omitido: bool = False  # otro ciclo estaba corriendo
    consolidacion: ConsolidationSummary | None = None
    pipeline: ResumenPipeline | None = None
    mensajes: list[str] = field(default_factory=list)


def configurar_logging(log_dir: Path | None, verbose: bool = False) -> None:
    """Consola siempre; archivo rotativo (5 MB x 5) si se indica ``log_dir``."""
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)
    fmt = logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s")
    for h in list(root.handlers):  # evita duplicar líneas si se llama dos veces
        root.removeHandler(h)
    consola = logging.StreamHandler()
    consola.setFormatter(fmt)
    root.addHandler(consola)
    if log_dir is not None:
        log_dir.mkdir(parents=True, exist_ok=True)
        archivo = RotatingFileHandler(
            log_dir / "elena_fuel.log",
            maxBytes=5_000_000,
            backupCount=5,
            encoding="utf-8",
        )
        archivo.setFormatter(fmt)
        root.addHandler(archivo)


def _try_lock_ciclo(conn: Connection) -> bool:
    row = conn.execute(
        "SELECT pg_try_advisory_lock(hashtext(%s), hashtext(%s))",
        (LOCK_NAMESPACE, LOCK_CICLO),
    ).fetchone()
    conn.commit()
    return bool(row and row[0])


def _unlock_ciclo(conn: Connection) -> None:
    conn.rollback()  # por si el ciclo falló a medio camino con una transacción abierta
    conn.execute(
        "SELECT pg_advisory_unlock(hashtext(%s), hashtext(%s))",
        (LOCK_NAMESPACE, LOCK_CICLO),
    )
    conn.commit()


def ejecutar_ciclo(
    conn: Connection,
    *,
    now: datetime | None = None,
    lag_minutes: int = 2,
    overlap_minutes: int = 60,
    max_ventanas: int | None = None,
) -> ResultadoCiclo:
    res = ResultadoCiclo()
    if not _try_lock_ciclo(conn):
        res.omitido = True
        res.mensajes.append("Otro ciclo está en curso: se omite.")
        log.info(res.mensajes[-1])
        return res
    try:
        ahora = now or datetime.now(UTC)
        try:
            desde, hasta = plan_range(
                conn,
                now=ahora,
                desde=None,
                hasta=None,
                lag_minutes=lag_minutes,
                overlap_minutes=overlap_minutes,
            )
        except NoStartError as exc:
            res.codigo = EXIT_SIN_INICIO
            res.mensajes.append(
                f"{exc} Corre antes: elena-fuel consolidate --desde ..."
            )
            log.error(res.mensajes[-1])
            return res

        if desde < hasta:
            res.consolidacion = consolidate(
                conn,
                desde,
                hasta,
                minutes=WINDOW_MINUTES,
                base_rules=QualityRules(fcc_inclinacion=0.0),
                descartar_sin_ignicion=None,  # lo decide fuel_config.descartar_sin_ignicion por autobús
            )
            conn.commit()
            c = res.consolidacion
            log.info(
                "Consolidación %s -> %s: %d ventanas (nuevas %d, actualizadas %d)",
                desde.isoformat(timespec="minutes"),
                hasta.isoformat(timespec="minutes"),
                c.ventanas,
                c.insertadas,
                c.actualizadas,
            )
            for a in c.advertencias:
                log.warning("Consolidación: %s", a)
        else:
            log.info("Nada que consolidar (%s >= %s)", desde, hasta)

        res.pipeline = ejecutar_pipeline(conn, max_ventanas=max_ventanas)
        for a in res.pipeline.autobuses:
            log.info(
                "%s %s: ventanas=%d eventos=%d tardías=%d %s",
                a.idautobus,
                a.resultado,
                a.procesadas,
                a.eventos,
                a.omitidas_tardias,
                dict(a.por_estado),
            )
            if a.error:
                log.error("%s: %s", a.idautobus, a.error)
        if not res.pipeline.ok:
            res.codigo = EXIT_ERROR
        return res
    except Exception:
        log.exception("El ciclo falló")
        res.codigo = EXIT_ERROR
        return res
    finally:
        try:
            _unlock_ciclo(conn)
        except Exception:  # la conexión pudo romperse; el lock de sesión muere con ella
            log.exception("No se pudo liberar el lock del ciclo")
