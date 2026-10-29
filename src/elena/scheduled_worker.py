"""Ciclo programado (Windows Task Scheduler): consolidar ventanas (con litros) y rendimiento.

Un ciclo hace, en este orden:
  1. Toma un lock advisory de SESIÓN (``elena_fuel`` / ``ciclo``). Si otro ciclo ya corre, sale
     sin hacer nada (código 0): el Task Scheduler puede lanzar una segunda instancia.
  2. Consolida gps_records -> fuel_windows (calidad y litros) desde la última ventana guardada
     (con traslape para datos tardíos) hasta la última ventana cerrada, y confirma.
  3. Detecta los eventos del mismo rango (fuel_events). Un fallo aquí se registra pero no cambia
     el código de salida.
  4. Actualiza el rendimiento reciente (fuel_rendimiento_reciente) y agrega al historial los
     bloques de km ya definitivos (fuel_rendimiento_historial). Un fallo aquí se registra pero
     no cambia el código de salida: no es parte de la consolidación.
  5. Libera el lock, también si algo falla.

Códigos de salida: 0 = bien u omitido por ciclo en curso; 1 = fallo del ciclo; 2 = no hay punto
de partida (fuel_windows vacía: corre antes ``consolidate --desde``).
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
from .eventos import detectar_eventos
from .fuel_repo import (
    autobuses_activos,
    bloques_rendimiento_nuevos,
    guardar_bloques,
    guardar_rendimiento_reciente,
    rendimiento_reciente,
)
from .quality import QualityRules

log = logging.getLogger("elena.worker")

LOCK_NAMESPACE = "elena_fuel"
LOCK_CICLO = "ciclo"

EXIT_OK = 0
EXIT_ERROR = 1
EXIT_SIN_INICIO = 2

RENDIMIENTO_KM_VENTANA = 100.0  # "últimos kilómetros" que mira el rendimiento reciente
RENDIMIENTO_DIAS = 30  # cuánto histórico se revisa para juntar esos km
RENDIMIENTO_KM_BLOQUE = 50.0  # tamaño de los bloques del historial


@dataclass(slots=True)
class ResultadoCiclo:
    codigo: int = EXIT_OK
    omitido: bool = False  # otro ciclo estaba corriendo
    consolidacion: ConsolidationSummary | None = None
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


def actualizar_rendimiento_reciente(
    conn: Connection,
    *,
    ahora: datetime,
    km_ventana: float = RENDIMIENTO_KM_VENTANA,
    dias: int = RENDIMIENTO_DIAS,
) -> int:
    """Recalcula y guarda el rendimiento reciente de cada autobús activo. Devuelve cuántos.

    Cada autobús va en su propia transacción: si uno falla se revierte solo ese y siguen los
    demás. Nunca lanza: este paso es secundario y no debe tirar el ciclo.
    """
    try:
        buses = autobuses_activos(conn)
        conn.commit()  # cierra la transacción implícita: cada autobús abre la suya
    except Exception:
        log.exception("Rendimiento reciente: no se pudo listar los autobuses")
        return 0
    guardados = 0
    for bus in buses:
        try:
            with conn.transaction():
                est = rendimiento_reciente(
                    conn, bus, ahora=ahora, km_ventana=km_ventana, dias=dias
                )
                guardar_rendimiento_reciente(conn, bus, est, km_ventana)
            guardados += 1
            if est.rendimiento_km_l is None:
                log.info(
                    "%s rendimiento reciente: sin estimación (%s)", bus, est.motivo
                )
            else:
                log.info(
                    "%s rendimiento reciente: %.2f km/L ±%s%% (%.1f km, %d ventanas)",
                    bus,
                    est.rendimiento_km_l,
                    "?" if est.error_pct is None else f"{est.error_pct:.1f}",
                    est.km,
                    est.puntos,
                )
        except Exception:
            log.exception("%s: falló el rendimiento reciente", bus)
    return guardados


def actualizar_eventos(conn: Connection, desde: datetime, hasta: datetime) -> None:
    """Detecta y guarda los eventos de [desde, hasta) en su propia transacción. Nunca lanza."""
    try:
        with conn.transaction():
            n = detectar_eventos(conn, desde, hasta)
        log.info("Eventos: %s", dict(n) or "ninguno")
    except Exception:
        log.exception("Falló la detección de eventos")


def actualizar_historial_rendimiento(
    conn: Connection,
    *,
    ahora: datetime,
    km_bloque: float = RENDIMIENTO_KM_BLOQUE,
    dias_inicial: int = RENDIMIENTO_DIAS,
) -> int:
    """Agrega al historial los bloques de km que ya son definitivos. Devuelve cuántos.

    Retoma en el último bloque guardado de cada autobús; un autobús sin historial empieza
    ``dias_inicial`` días atrás (para rellenar más, usa ``elena-fuel historial-rendimiento
    --desde``). Igual que el rendimiento reciente: una transacción por autobús y nunca lanza.
    """
    try:
        buses = autobuses_activos(conn)
        conn.commit()
    except Exception:
        log.exception("Historial de rendimiento: no se pudo listar los autobuses")
        return 0
    nuevos = 0
    for bus in buses:
        try:
            with conn.transaction():
                bloques = bloques_rendimiento_nuevos(
                    conn,
                    bus,
                    ahora=ahora,
                    km_bloque=km_bloque,
                    dias_inicial=dias_inicial,
                )
                n = guardar_bloques(conn, bus, bloques, km_bloque)
            nuevos += n
            if n:
                log.info("%s historial de rendimiento: %d bloques nuevos", bus, n)
        except Exception:
            log.exception("%s: falló el historial de rendimiento", bus)
    return nuevos


def ejecutar_ciclo(
    conn: Connection,
    *,
    now: datetime | None = None,
    lag_minutes: int = 2,
    overlap_minutes: int = 60,
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
            actualizar_eventos(conn, desde, hasta)
        else:
            log.info("Nada que consolidar (%s >= %s)", desde, hasta)

        actualizar_rendimiento_reciente(conn, ahora=ahora)
        actualizar_historial_rendimiento(conn, ahora=ahora)
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
