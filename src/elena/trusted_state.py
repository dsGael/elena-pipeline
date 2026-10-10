
"""Inicialización del estado confiable de combustible.

Este módulo busca una secuencia estable de ventanas para inicializar
fuel_estado_confiable. No modifica gps_records ni genera eventos.

La función principal opera en modo simulación por defecto.
La transacción y el commit son responsabilidad del llamador.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime
from decimal import Decimal
from typing import Any


VENTANAS_INICIALES_REQUERIDAS = 3
INTERVALO_MAXIMO_MINUTOS = 10
VARIACION_MAXIMA_LITROS = Decimal("0.75")
VELOCIDAD_IMPLICITA_MAXIMA_KMH = Decimal("140")


@dataclass(frozen=True, slots=True)
class LecturaCandidata:
    resultado_id: str
    idautobus: str
    imei: str
    ventana_id: str
    ventana_inicio: datetime
    litros: Decimal
    odometro_m: Decimal | None


@dataclass(frozen=True, slots=True)
class InicializacionPropuesta:
    idautobus: str
    imei: str
    ventana_inicio: datetime
    litros: Decimal
    odometro_m: Decimal | None
    ventanas_validas: int
    rango_litros: Decimal
    aplicada: bool = False
    motivo: str = ""


@dataclass(frozen=True, slots=True)
class ResumenInicializacion:
    propuestas: tuple[InicializacionPropuesta, ...]
    autobuses_sin_secuencia: tuple[str, ...]
    modo_prueba: bool


def _decimal(valor: Any) -> Decimal | None:
    if valor is None:
        return None
    return Decimal(str(valor))


def _consultar_autobuses_sin_estado(conn) -> set[str]:
    """Obtiene autobuses que todavía no tienen estado confiable."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT a.idautobus
            FROM public.autobuses AS a
            WHERE EXISTS (
                SELECT 1
                FROM public.dispositivos AS d
                JOIN public.fuel_config AS c
                  ON c.idautobus = d.idautobus
                 AND c.activo IS TRUE
                WHERE d.idautobus = a.idautobus
                  AND d.activo IS TRUE
                  AND d.imei IS NOT NULL
            )
            AND NOT EXISTS (
                SELECT 1
                FROM public.fuel_estado_confiable AS e
                WHERE e.idautobus = a.idautobus
                  AND e.ultimo_litro_confiable IS NOT NULL
            )
            """
        )
        return {str(fila[0]) for fila in cur.fetchall()}


def _consultar_lecturas(conn) -> list[LecturaCandidata]:
    """Lee resultados provisionales que tienen litros calculados."""
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT
                r.id::text,
                r.idautobus,
                r.imei,
                r.ventana_id::text,
                r.ventana_inicio,
                r.litros_calculados,
                fw.odometro_total
            FROM public.fuel_processed_result AS r
            JOIN public.fuel_windows AS fw
              ON fw.id = r.ventana_id
            JOIN public.dispositivos AS d
              ON d.imei = r.imei
             AND d.idautobus = r.idautobus
             AND d.activo IS TRUE
            JOIN public.fuel_config AS c
              ON c.idautobus = r.idautobus
             AND c.activo IS TRUE
            WHERE r.actualizo_final IS FALSE
              AND r.estado_resultado = 'sin_actualizacion'
              AND r.litros_calculados IS NOT NULL
              AND fw.calidad_ventana = 'valida'
              AND fw.angulo_corregido IS NOT NULL
              AND ABS(fw.angulo_corregido) <= c.angulo_max
            ORDER BY r.idautobus, r.ventana_inicio, r.imei
            """
        )
        filas = cur.fetchall()

    lecturas = []
    for fila in filas:
        litros = _decimal(fila[5])
        if litros is None:
            continue

        lecturas.append(
            LecturaCandidata(
                resultado_id=str(fila[0]),
                idautobus=str(fila[1]),
                imei=str(fila[2]),
                ventana_id=str(fila[3]),
                ventana_inicio=fila[4],
                litros=litros,
                odometro_m=_decimal(fila[6]),
            )
        )

    return lecturas


def _continuidad_valida(
    anterior: LecturaCandidata,
    actual: LecturaCandidata,
) -> bool:
    """Comprueba el tiempo transcurrido y la coherencia del odómetro."""
    if anterior.idautobus != actual.idautobus:
        return False

    if anterior.imei != actual.imei:
        return False

    segundos = (actual.ventana_inicio - anterior.ventana_inicio).total_seconds()
    if segundos <= 0 or segundos > INTERVALO_MAXIMO_MINUTOS * 60:
        return False

    # Sin ambos odómetros no podemos validar distancia, pero tampoco
    # inventamos una distancia: la secuencia puede continuar sin ella.
    if anterior.odometro_m is None or actual.odometro_m is None:
        return True

    delta_m = actual.odometro_m - anterior.odometro_m

    # Un odómetro decreciente requiere revisión; no se usa esta transición.
    if delta_m < 0:
        return False

    delta_km = delta_m / Decimal("1000")
    horas = Decimal(str(segundos)) / Decimal("3600")
    velocidad_kmh = delta_km / horas

    return velocidad_kmh <= VELOCIDAD_IMPLICITA_MAXIMA_KMH


def _encontrar_secuencia(
    lecturas: list[LecturaCandidata],
) -> tuple[LecturaCandidata, list[LecturaCandidata]] | None:
    """Devuelve la primera secuencia estable de tres ventanas."""
    secuencia: list[LecturaCandidata] = []

    for lectura in lecturas:
        if not secuencia:
            secuencia = [lectura]
            continue

        anterior = secuencia[-1]
        if not _continuidad_valida(anterior, lectura):
            secuencia = [lectura]
            continue

        posibles = [*secuencia, lectura]
        litros = [elemento.litros for elemento in posibles]
        rango = max(litros) - min(litros)

        if rango > VARIACION_MAXIMA_LITROS:
            # La lectura actual puede iniciar una nueva secuencia.
            secuencia = [lectura]
            continue

        secuencia = posibles

        if len(secuencia) >= VENTANAS_INICIALES_REQUERIDAS:
            return secuencia[-1], secuencia[:VENTANAS_INICIALES_REQUERIDAS]
    return None


def inicializar_estado_confiable(
    conn,
    *,
    dry_run: bool = True,
    idautobus: str | None = None,
) -> ResumenInicializacion:
    """Propone o aplica la inicialización del estado confiable.

    En modo de prueba solo consulta la base de datos.
    En modo de aplicación inserta el estado inicial y marca como confiable
    únicamente el resultado correspondiente a la tercera ventana.

    No hace commit: la transacción pertenece al llamador.
    """
    autobuses_sin_estado = _consultar_autobuses_sin_estado(conn)
    if idautobus is not None:
        autobuses_sin_estado &= {idautobus}

    lecturas = _consultar_lecturas(conn)
    por_autobus: dict[str, list[LecturaCandidata]] = {}

    for lectura in lecturas:
        if lectura.idautobus in autobuses_sin_estado:
            por_autobus.setdefault(lectura.idautobus, []).append(lectura)

    propuestas: list[InicializacionPropuesta] = []
    sin_secuencia: list[str] = []

    for autobus in sorted(autobuses_sin_estado):
        secuencia = _encontrar_secuencia(por_autobus.get(autobus, []))

        if secuencia is None:
            sin_secuencia.append(autobus)
            continue

        lectura_final, lecturas_secuencia = secuencia
        litros_secuencia = [lectura.litros for lectura in lecturas_secuencia]
        rango = max(litros_secuencia) - min(litros_secuencia)

        propuesta = InicializacionPropuesta(
            idautobus=lectura_final.idautobus,
            imei=lectura_final.imei,
            ventana_inicio=lectura_final.ventana_inicio,
            litros=lectura_final.litros,
            odometro_m=lectura_final.odometro_m,
            ventanas_validas=len(lecturas_secuencia),
            rango_litros=rango,
            motivo=(
                f"Secuencia estable de {len(lecturas_secuencia)} ventanas; "
                f"rango de {rango} L."
            ),
        )

        if not dry_run:
            with conn.cursor() as cur:
                # No reemplazar un estado que otro proceso haya inicializado.
                cur.execute(
                    """
                    INSERT INTO public.fuel_estado_confiable (
                        idautobus,
                        imei_ultimo,
                        ultimo_litro_confiable,
                        fecha_ultimo_confiable,
                        odometro_ultimo_confiable,
                        pendiente_tipo,
                        pendiente_desde,
                        pendiente_litros,
                        pendiente_ventanas,
                        updated_at
                    )
                    VALUES (%s, %s, %s, %s, %s, NULL, NULL, NULL, 0, NOW())
                    ON CONFLICT (idautobus) DO NOTHING
                    RETURNING idautobus
                    """,
                    (
                        lectura_final.idautobus,
                        lectura_final.imei,
                        lectura_final.litros,
                        lectura_final.ventana_inicio,
                        lectura_final.odometro_m,
                    ),
                )
                estado_insertado = cur.fetchone()

                if estado_insertado is None:
                    # Ya existe una fila para el autobús. No sobrescribirla.
                    continue

                cur.execute(
                    """
                    UPDATE public.fuel_processed_result
                    SET litros_finales = litros_calculados,
                        actualizo_final = TRUE,
                        estado_resultado = 'confiable',
                        motivo = %s
                    WHERE id = %s::uuid
                      AND actualizo_final IS FALSE
                    RETURNING id
                    """,
                    (
                        "Inicialización del estado confiable tras "
                        f"{len(lecturas_secuencia)} ventanas estables.",
                        lectura_final.resultado_id,
                    ),
                )

                if cur.fetchone() is None:
                    raise RuntimeError(
                        "No se pudo marcar como confiable el resultado "
                        f"{lectura_final.resultado_id}. "
                        "Revierte la transacción antes de continuar."
                    )

            propuesta = InicializacionPropuesta(
                **{
                    campo: getattr(propuesta, campo)
                    for campo in propuesta.__dataclass_fields__
                    if campo != "aplicada"
                },
                aplicada=True,
            )

        propuestas.append(propuesta)

    return ResumenInicializacion(
        propuestas=tuple(propuestas),
        autobuses_sin_secuencia=tuple(sin_secuencia),
        modo_prueba=dry_run,
    )