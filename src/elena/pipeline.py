"""Orquestador: fuel_windows -> processor -> resultado + evento + estado confiable.

Dos capas:
  * ``procesar_autobus``: lógica de orquestación sobre un ``Repositorio`` (interfaz). No conoce
    PostgreSQL, así que se prueba con un repositorio en memoria.
  * ``ejecutar_pipeline``: recorre los autobuses; por cada uno abre UNA transacción, toma un lock
    advisory (si otra instancia del Task Scheduler ya lo tiene, se omite) y llama a
    ``procesar_autobus`` con ``PgRepositorio`` (pipeline_repo.py). Un error en un autobús
    revierte solo a ese autobús.

Reglas de orden e idempotencia:
  * Las ventanas se procesan en orden cronológico por autobús y solo las que aún no tienen
    resultado (UNIQUE ventana_id en fuel_processed_result).
  * Una ventana anterior a la última ya procesada (llegó tarde) NO se procesa: hacerlo rompería
    el orden contra el último valor confiable. Se cuenta como ``omitidas_tardias``. Reprocesar
    históricos queda como tarea aparte.
  * Si una ventana ya procesada se re-consolida con datos tardíos, su resultado no cambia.

Eventos (a lo más UNO abierto por autobús y familia):
  * Una confirmación convierte el evento abierto de su familia en confirmado (no duplica).
  * Un evento abierto nuevo reemplaza (descarta) a los abiertos anteriores de su familia.
  * Si el pendiente se resuelve sin confirmarse (el nivel regresa, o cambia de dirección), el
    evento abierto se descarta con motivo ``resuelto_sin_confirmar``.
  Familia "aumento": carga_probable, aumento_pendiente_por_inclinacion (-> carga_confirmada).
  Familia "caida":   posible_extraccion, caida_pendiente_por_inclinacion (-> extraccion_confirmada).
"""

from __future__ import annotations

import logging
from collections import Counter
from dataclasses import dataclass, field, replace
from datetime import datetime
from typing import Protocol

from psycopg import Connection

from .fuel_repo import cargar_config
from .processor import (
    EstadoConfiable,
    EventoCombustible,
    PendienteTipo,
    ProcesadorConfig,
    ResultadoEstado,
    ResultadoProcesado,
    VentanaEntrada,
    procesar_ventana,
)

log = logging.getLogger("elena.pipeline")

TIPOS_FAMILIA: dict[PendienteTipo, tuple[str, ...]] = {
    PendienteTipo.AUMENTO: (
        "carga_probable",
        "aumento_pendiente_por_inclinacion",
        "carga_confirmada",
    ),
    PendienteTipo.CAIDA: (
        "posible_extraccion",
        "caida_pendiente_por_inclinacion",
        "extraccion_confirmada",
    ),
}
_FAMILIA_DE_TIPO = {t: f for f, tipos in TIPOS_FAMILIA.items() for t in tipos}
_CONFIRMACION_DE = {
    ResultadoEstado.CARGA_CONFIRMADA: PendienteTipo.AUMENTO,
    ResultadoEstado.EXTRACCION_CONFIRMADA: PendienteTipo.CAIDA,
}

MOTIVO_REEMPLAZADO = "reemplazado_por_evento_posterior"
# Cuántas ventanas anteriores se le pasan al procesador para la concordancia. Sobra: el
# procesador además las limita por concordancia_max_minutos.
VENTANAS_PREVIAS = 24
MOTIVO_SIN_CONFIRMAR = "resuelto_sin_confirmar"


@dataclass(frozen=True, slots=True)
class EventoAbierto:
    id: str
    fecha_inicio: datetime


class Repositorio(Protocol):
    """Acceso a datos de UN autobús, dentro de una transacción ya abierta."""

    def cargar_estado(self, idautobus: str) -> EstadoConfiable: ...

    def ultimo_resultado(
        self, idautobus: str
    ) -> tuple[ResultadoEstado, datetime] | None:
        """(estado_resultado, ventana_inicio) de la última ventana procesada, si hay."""
        ...

    def ventanas_pendientes(
        self, idautobus: str, despues_de: datetime | None, limite: int | None
    ) -> tuple[list[VentanaEntrada], int]:
        """Ventanas sin resultado posteriores a ``despues_de`` (orden cronológico) y la
        cantidad de ventanas sin resultado que NO lo son (llegaron tarde)."""
        ...

    def ventanas_previas(
        self, idautobus: str, antes_de: datetime, limite: int
    ) -> list[VentanaEntrada]:
        """Las ``limite`` ventanas inmediatamente anteriores a ``antes_de`` (cualquier calidad,
        orden cronológico). Las usa la concordancia."""
        ...

    def posicion(
        self, imei: str, desde: datetime, hasta: datetime
    ) -> tuple[float, float] | None: ...

    def guardar_resultado(self, resultado: ResultadoProcesado) -> None: ...

    def eventos_abiertos(
        self, idautobus: str, tipos: tuple[str, ...]
    ) -> list[EventoAbierto]:
        """Más reciente primero."""
        ...

    def insertar_evento(self, evento: EventoCombustible) -> None: ...

    def confirmar_evento(self, id_evento: str, evento: EventoCombustible) -> None: ...

    def descartar_eventos(self, ids: list[str], motivo: str) -> None: ...

    def guardar_estado(
        self, idautobus: str, imei: str, estado: EstadoConfiable
    ) -> None: ...


@dataclass(slots=True)
class ResumenAutobus:
    idautobus: str
    resultado: str = "ok"  # ok | ocupado | error
    error: str | None = None
    procesadas: int = 0
    omitidas_tardias: int = 0
    eventos: int = 0
    por_estado: Counter[str] = field(default_factory=Counter)


@dataclass(slots=True)
class ResumenPipeline:
    autobuses: list[ResumenAutobus] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return all(a.resultado != "error" for a in self.autobuses)


def _gestionar_eventos(
    repo: Repositorio,
    idautobus: str,
    estado_antes: EstadoConfiable,
    estado_despues: EstadoConfiable,
    resultado: ResultadoProcesado,
    evento: EventoCombustible | None,
    ventana: VentanaEntrada,
) -> int:
    """Aplica las reglas de eventos de la cabecera. Devuelve cuántos eventos tocó (nuevos o
    confirmados)."""
    tocados = 0
    familia_confirmada = _CONFIRMACION_DE.get(resultado.estado_resultado)

    if evento is not None:
        if evento.latitude is None and evento.longitude is None:
            pos = repo.posicion(
                ventana.imei, ventana.ventana_inicio, ventana.ventana_fin
            )
            if pos is not None:
                evento = replace(evento, latitude=pos[0], longitude=pos[1])
        familia = _FAMILIA_DE_TIPO[evento.tipo_evento]
        abiertos = repo.eventos_abiertos(idautobus, TIPOS_FAMILIA[familia])
        if evento.estado == "confirmado" and abiertos:
            repo.confirmar_evento(abiertos[0].id, evento)
            sobrantes = abiertos[1:]
        else:
            repo.insertar_evento(evento)
            sobrantes = abiertos
        if sobrantes:
            repo.descartar_eventos([e.id for e in sobrantes], MOTIVO_REEMPLAZADO)
        tocados = 1

    vieja = estado_antes.pendiente_tipo
    nueva = estado_despues.pendiente_tipo
    # El pendiente anterior se resolvió sin confirmarse (o cambió de dirección).
    if vieja is not None and nueva is not vieja and vieja is not familia_confirmada:
        abiertos = repo.eventos_abiertos(idautobus, TIPOS_FAMILIA[vieja])
        if abiertos:
            repo.descartar_eventos([e.id for e in abiertos], MOTIVO_SIN_CONFIRMAR)
    return tocados


def procesar_autobus(
    repo: Repositorio,
    idautobus: str,
    cfg: ProcesadorConfig,
    *,
    max_ventanas: int | None = None,
) -> ResumenAutobus:
    resumen = ResumenAutobus(idautobus)
    estado = repo.cargar_estado(idautobus)
    ultimo = repo.ultimo_resultado(idautobus)
    previo = ultimo[0] if ultimo else None
    despues_de = ultimo[1] if ultimo else None

    ventanas, tardias = repo.ventanas_pendientes(idautobus, despues_de, max_ventanas)
    resumen.omitidas_tardias = tardias
    if tardias:
        log.warning(
            "%s: %d ventana(s) llegaron antes de la última procesada y se omiten",
            idautobus,
            tardias,
        )

    # Ventanas anteriores para la concordancia (solo si está activada).
    previas: list[VentanaEntrada] = []
    if ventanas and cfg.concordancia_ventanas:
        previas = list(
            repo.ventanas_previas(
                idautobus, ventanas[0].ventana_inicio, VENTANAS_PREVIAS
            )
        )

    for v in ventanas:
        salida = procesar_ventana(v, cfg, estado, idautobus, previo, previas)
        if cfg.concordancia_ventanas:
            previas = (previas + [v])[-VENTANAS_PREVIAS:]
        repo.guardar_resultado(salida.resultado)
        resumen.eventos += _gestionar_eventos(
            repo, idautobus, estado, salida.estado, salida.resultado, salida.evento, v
        )
        resumen.por_estado[salida.resultado.estado_resultado.value] += 1
        resumen.procesadas += 1
        estado, previo = salida.estado, salida.resultado.estado_resultado

    if ventanas:
        repo.guardar_estado(idautobus, ventanas[-1].imei, estado)
    return resumen


def ejecutar_pipeline(
    conn: Connection,
    *,
    idautobus: str | None = None,
    max_ventanas: int | None = None,
) -> ResumenPipeline:
    """Procesa todos los autobuses activos de fuel_config (o uno), cada uno en su transacción."""
    from .pipeline_repo import PgRepositorio, try_lock_autobus

    filas = conn.execute(
        "SELECT idautobus FROM public.fuel_config "
        "WHERE activo AND (%(bus)s::text IS NULL OR idautobus = %(bus)s::text) "
        "ORDER BY idautobus",
        {"bus": idautobus},
    ).fetchall()
    conn.commit()  # cierra la transacción implícita: cada autobús abre la suya (y su lock)

    resumen = ResumenPipeline()
    for (bus,) in filas:
        r = ResumenAutobus(bus)
        try:
            with conn.transaction():
                if not try_lock_autobus(conn, bus):
                    r.resultado = "ocupado"
                    log.info("%s: otra instancia lo está procesando, se omite", bus)
                else:
                    cfg = cargar_config(conn, bus, rendimiento_de_tabla=True)
                    log.info(
                        "%s: rendimiento %s km/L (origen: %s)",
                        bus,
                        cfg.rendimiento_default,
                        cfg.rendimiento_origen,
                    )
                    r = procesar_autobus(
                        PgRepositorio(conn), bus, cfg, max_ventanas=max_ventanas
                    )
        except Exception as exc:  # un autobús con error no detiene a los demás
            log.exception("%s: error, se revierte solo este autobús", bus)
            r = ResumenAutobus(
                bus, resultado="error", error=f"{type(exc).__name__}: {exc}"
            )
        resumen.autobuses.append(r)
    return resumen