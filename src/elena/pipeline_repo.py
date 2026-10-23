"""Implementación PostgreSQL del ``Repositorio`` del pipeline (SQL manual, sin ORM).

Escribe solo en fuel_processed_result, fuel_events, fuel_estado_confiable y
fuel_rendimiento_reciente y fuel_rendimiento_historial; lee fuel_windows,
dispositivos y gps_records (nunca modifica gps_records).
"""

from __future__ import annotations

from datetime import datetime
from typing import Any

from psycopg import Connection

from .pipeline import EventoAbierto
from .processor import (
    EstadoConfiable,
    EventoCombustible,
    PendienteTipo,
    ResultadoEstado,
    ResultadoProcesado,
    VentanaEntrada,
)
from .quality import WindowQuality
from .rendimiento import BloqueRendimiento, RendimientoReciente

LOCK_NAMESPACE = "elena_fuel"


def try_lock_autobus(conn: Connection, idautobus: str) -> bool:
    """Lock advisory a nivel de transacción: se libera solo al confirmar o revertir."""
    row = conn.execute(
        "SELECT pg_try_advisory_xact_lock(hashtext(%s), hashtext(%s))",
        (LOCK_NAMESPACE, idautobus),
    ).fetchone()
    return bool(row and row[0])


def _f(v: Any) -> float | None:
    return None if v is None else float(v)


ESTADO_SQL = """
SELECT ultimo_litro_confiable, fecha_ultimo_confiable, odometro_ultimo_confiable,
       pendiente_tipo, pendiente_desde, pendiente_litros, pendiente_ventanas
FROM public.fuel_estado_confiable WHERE idautobus = %s
"""

ULTIMO_RESULTADO_SQL = """
SELECT estado_resultado, ventana_inicio
FROM public.fuel_processed_result
WHERE idautobus = %s
ORDER BY ventana_inicio DESC LIMIT 1
"""

# Ventanas del autobús (por sus dispositivos) que aún no tienen resultado.
VENTANAS_SQL = """
SELECT w.id::text, w.imei, w.ventana_inicio, w.ventana_fin, w.calidad_ventana,
       w.motivo_calidad, w.sensor_mediana, w.velocidad_promedio, w.odometro_total,
       w.muestras_validas, w.ignicion_dominante
FROM public.fuel_windows w
JOIN public.dispositivos d ON d.imei = w.imei
WHERE d.idautobus = %(bus)s
  AND (%(despues)s::timestamptz IS NULL OR w.ventana_inicio > %(despues)s::timestamptz)
  AND NOT EXISTS (SELECT 1 FROM public.fuel_processed_result r WHERE r.ventana_id = w.id)
ORDER BY w.ventana_inicio
LIMIT %(limite)s
"""

# Ventanas inmediatamente anteriores a una fecha (para la concordancia), la más reciente primero.
VENTANAS_PREVIAS_SQL = """
SELECT w.id::text, w.imei, w.ventana_inicio, w.ventana_fin, w.calidad_ventana,
       w.motivo_calidad, w.sensor_mediana, w.velocidad_promedio, w.odometro_total,
       w.muestras_validas, w.ignicion_dominante
FROM public.fuel_windows w
JOIN public.dispositivos d ON d.imei = w.imei
WHERE d.idautobus = %(bus)s
  AND w.ventana_inicio < %(antes)s::timestamptz
ORDER BY w.ventana_inicio DESC
LIMIT %(limite)s
"""


def _ventana(r: Any) -> VentanaEntrada:
    return VentanaEntrada(
        ventana_id=r[0],
        imei=r[1],
        ventana_inicio=r[2],
        ventana_fin=r[3],
        calidad=WindowQuality(r[4]),
        motivo_calidad=r[5],
        sensor_mediana=_f(r[6]),
        velocidad_promedio=_f(r[7]),
        odometro_total=_f(r[8]),
        muestras_validas=None if r[9] is None else int(r[9]),
        ignicion_dominante=r[10],
    )


TARDIAS_SQL = """
SELECT count(*)
FROM public.fuel_windows w
JOIN public.dispositivos d ON d.imei = w.imei
WHERE d.idautobus = %(bus)s
  AND w.ventana_inicio <= %(despues)s::timestamptz
  AND NOT EXISTS (SELECT 1 FROM public.fuel_processed_result r WHERE r.ventana_id = w.id)
"""


class PgRepositorio:
    def __init__(self, conn: Connection) -> None:
        self.conn = conn

    def cargar_estado(self, idautobus: str) -> EstadoConfiable:
        row = self.conn.execute(ESTADO_SQL, (idautobus,)).fetchone()
        if row is None:
            return EstadoConfiable()
        ult, fecha, odo, ptipo, pdesde, plitros, pvent = row
        return EstadoConfiable(
            ultimo_litro_confiable=_f(ult),
            fecha_ultimo_confiable=fecha,
            odometro_ultimo_confiable=_f(odo),
            pendiente_tipo=PendienteTipo(ptipo) if ptipo else None,
            pendiente_desde=pdesde,
            pendiente_litros=_f(plitros),
            pendiente_ventanas=int(pvent or 0),
        )

    def ultimo_resultado(
        self, idautobus: str
    ) -> tuple[ResultadoEstado, datetime] | None:
        row = self.conn.execute(ULTIMO_RESULTADO_SQL, (idautobus,)).fetchone()
        return None if row is None else (ResultadoEstado(row[0]), row[1])

    def ventanas_pendientes(
        self, idautobus: str, despues_de: datetime | None, limite: int | None
    ) -> tuple[list[VentanaEntrada], int]:
        params = {"bus": idautobus, "despues": despues_de, "limite": limite}
        filas = self.conn.execute(VENTANAS_SQL, params).fetchall()
        ventanas = [_ventana(r) for r in filas]
        tardias = 0
        if despues_de is not None:
            row = self.conn.execute(TARDIAS_SQL, params).fetchone()
            tardias = int(row[0]) if row else 0
        return ventanas, tardias

    def ventanas_previas(
        self, idautobus: str, antes_de: datetime, limite: int
    ) -> list[VentanaEntrada]:
        params = {"bus": idautobus, "antes": antes_de, "limite": limite}
        filas = self.conn.execute(VENTANAS_PREVIAS_SQL, params).fetchall()
        return [_ventana(r) for r in reversed(filas)]

    def posicion(
        self, imei: str, desde: datetime, hasta: datetime
    ) -> tuple[float, float] | None:
        # recorded_at (nunca created_at); última muestra de la ventana.
        row = self.conn.execute(
            "SELECT latitude, longitude FROM public.gps_records "
            "WHERE imei = %s AND recorded_at >= %s AND recorded_at < %s "
            "ORDER BY recorded_at DESC LIMIT 1",
            (imei, desde, hasta),
        ).fetchone()
        return None if row is None else (float(row[0]), float(row[1]))

    def guardar_resultado(self, r: ResultadoProcesado) -> None:
        self.conn.execute(
            """
            INSERT INTO public.fuel_processed_result (
                idautobus, imei, ventana_id, ventana_inicio, litros_calculados,
                litros_finales, ultimo_valor_confiable_anterior, diferencia_litros,
                km_desde_ultimo_confiable, rendimiento_usado, consumo_esperado,
                exceso_no_explicado, estado_resultado, actualizo_final, motivo,
                calibracion_version_usada, litros_estimados)
            VALUES (%s,%s,%s::uuid,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
            ON CONFLICT (ventana_id) DO NOTHING
            """,
            (
                r.idautobus,
                r.imei,
                r.ventana_id,
                r.ventana_inicio,
                r.litros_calculados,
                r.litros_finales,
                r.ultimo_valor_confiable_anterior,
                r.diferencia_litros,
                r.km_desde_ultimo_confiable,
                r.rendimiento_usado,
                r.consumo_esperado,
                r.exceso_no_explicado,
                r.estado_resultado.value,
                r.actualizo_final,
                r.motivo,
                r.calibracion_version_usada,
                r.litros_estimados,
            ),
        )

    def eventos_abiertos(
        self, idautobus: str, tipos: tuple[str, ...]
    ) -> list[EventoAbierto]:
        filas = self.conn.execute(
            "SELECT id::text, fecha_inicio FROM public.fuel_events "
            "WHERE idautobus = %s AND estado = 'abierto' AND tipo_evento = ANY(%s) "
            "ORDER BY fecha_inicio DESC, created_at DESC",
            (idautobus, list(tipos)),
        ).fetchall()
        return [EventoAbierto(r[0], r[1]) for r in filas]

    def insertar_evento(self, e: EventoCombustible) -> None:
        self.conn.execute(
            """
            INSERT INTO public.fuel_events (
                idautobus, imei, tipo_evento, estado, fecha_inicio, fecha_fin,
                litros_antes, litros_despues, litros_evento, latitude, longitude,
                calidad_evento, motivo, ventana_origen_id)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s::uuid)
            """,
            (
                e.idautobus,
                e.imei,
                e.tipo_evento,
                e.estado,
                e.fecha_inicio,
                e.fecha_fin,
                e.litros_antes,
                e.litros_despues,
                e.litros_evento,
                e.latitude,
                e.longitude,
                e.calidad_evento,
                e.motivo,
                e.ventana_origen_id,
            ),
        )

    def confirmar_evento(self, id_evento: str, e: EventoCombustible) -> None:
        # Conserva fecha_inicio del evento abierto (cuándo empezó el cambio).
        self.conn.execute(
            """
            UPDATE public.fuel_events SET
                tipo_evento = %s, estado = 'confirmado', fecha_fin = %s,
                litros_antes = %s, litros_despues = %s, litros_evento = %s,
                latitude = COALESCE(%s, latitude), longitude = COALESCE(%s, longitude),
                calidad_evento = %s, motivo = %s, ventana_origen_id = %s::uuid,
                updated_at = now()
            WHERE id = %s::uuid
            """,
            (
                e.tipo_evento,
                e.fecha_fin,
                e.litros_antes,
                e.litros_despues,
                e.litros_evento,
                e.latitude,
                e.longitude,
                e.calidad_evento,
                e.motivo,
                e.ventana_origen_id,
                id_evento,
            ),
        )

    def descartar_eventos(self, ids: list[str], motivo: str) -> None:
        if not ids:
            return
        self.conn.execute(
            "UPDATE public.fuel_events SET estado = 'descartado', "
            "motivo = CASE WHEN motivo IS NULL OR motivo = '' THEN %s "
            "ELSE motivo || '; ' || %s END, updated_at = now() "
            "WHERE id = ANY(%s::uuid[])",
            (motivo, motivo, ids),
        )

    def guardar_estado(self, idautobus: str, imei: str, e: EstadoConfiable) -> None:
        self.conn.execute(
            """
            INSERT INTO public.fuel_estado_confiable (
                idautobus, imei_ultimo, ultimo_litro_confiable, fecha_ultimo_confiable,
                odometro_ultimo_confiable, pendiente_tipo, pendiente_desde,
                pendiente_litros, pendiente_ventanas, updated_at)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s, now())
            ON CONFLICT (idautobus) DO UPDATE SET
                imei_ultimo = EXCLUDED.imei_ultimo,
                ultimo_litro_confiable = EXCLUDED.ultimo_litro_confiable,
                fecha_ultimo_confiable = EXCLUDED.fecha_ultimo_confiable,
                odometro_ultimo_confiable = EXCLUDED.odometro_ultimo_confiable,
                pendiente_tipo = EXCLUDED.pendiente_tipo,
                pendiente_desde = EXCLUDED.pendiente_desde,
                pendiente_litros = EXCLUDED.pendiente_litros,
                pendiente_ventanas = EXCLUDED.pendiente_ventanas,
                updated_at = now()
            """,
            (
                idautobus,
                imei,
                e.ultimo_litro_confiable,
                e.fecha_ultimo_confiable,
                e.odometro_ultimo_confiable,
                e.pendiente_tipo.value if e.pendiente_tipo else None,
                e.pendiente_desde,
                e.pendiente_litros,
                e.pendiente_ventanas,
            ),
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
