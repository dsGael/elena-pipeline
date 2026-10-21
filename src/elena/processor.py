"""Clasificación de una ventana consolidada (documento ELENA, secciones 9 a 12).

Módulo PURO: recibe una ventana, la configuración del vehículo y el último valor confiable, y
devuelve el resultado (fuel_processed_result), un evento opcional (fuel_events) y el estado
nuevo (fuel_estado_confiable). No toca base de datos; eso es trabajo de pipeline.py.

Convenciones (siguen el PDF y el esquema SQL, no el .md de borrador):
  * diferencia_litros = litros_consolidados - ultimo_litro_confiable
    (positiva = sube el nivel = posible carga; negativa = baja = consumo o extracción).
  * caida_real = ultimo - litros; consumo_esperado = km / rendimiento (rendimiento en km/L);
    exceso_no_explicado = caida_real - consumo_esperado.
  * Los litros se redondean a 2 decimales ANTES de decidir, para que lo guardado coincida con
    la decisión (misma idea que quality.py).

Reglas sin umbral definido (NULL en fuel_config) NO se aplican:
  * rendimiento_default None -> no se puede calcular consumo_esperado. Una caída de hasta
    umbral_extraccion es consumo normal aunque el consumo real fuera 0 (el exceso nunca podría
    superar el umbral); una caída mayor queda SIN_ACTUALIZACION hasta tener rendimiento.
  * velocidad_max_carga None -> la carga no se condiciona a la velocidad.
  * ventanas_sostenidas None -> la carga se confirma sin exigir persistencia; la extracción
    NUNCA se confirma (la persistencia es lo único que separa "posible" de "confirmada").

Qué ventanas cuentan para "sostenido": el contador (pendiente_ventanas) solo avanza con
ventanas válidas que repiten la misma condición contra el MISMO último valor confiable. Las
ventanas dudosas/descartadas no lo rompen ni lo avanzan. Si la ventana anterior fue un
pendiente por inclinación, el contador reinicia en 1 (esa ventana no era evidencia válida).

Un evento se emite solo cuando el estado del resultado CAMBIA respecto a la ventana anterior
(``estado_resultado_previo``), para no duplicar eventos mientras la condición persiste.

Sin "trinquete": una subida menor a ``umbral_carga_dudosa`` en una ventana válida es ruido (el
líquido se mueve, la lectura arranca inestable...). La ventana sale ``confiable``, pero el
último valor confiable NO sube (``actualizo_final`` False, motivo ``nivel_conservado``): solo
una carga confirmada puede subir el nivel. Si cada subidita se aceptara como nueva referencia,
las oscilaciones se acumularían escalón por escalón. Las bajadas explicadas por consumo siguen
actualizando el nivel. Con diferencia exactamente 0 sí se refresca la referencia (fecha y
odómetro). Si la ventana de ruido resuelve un pendiente (carga o extracción sin confirmar),
el pendiente se limpia aunque el nivel no cambie.

Concordancia (opcional; NULL en fuel_config = desactivada): una ventana que por sí sola no
alcanza para actualizar (en movimiento, o parada con pocas muestras porque el motor se apagó)
puede BAJAR el nivel si las últimas ``concordancia_ventanas`` lecturas utilizables, dentro de
``concordancia_max_minutos``, coinciden entre sí una vez descontado el consumo por kilómetro
(rango <= ``concordancia_tolerancia`` L). El nuevo nivel es la mediana de esas lecturas
llevadas al odómetro actual. Nunca sube el nivel (sin trinquete) ni confirma cargas o
extracciones: si la bajada no la explica el consumo (exceso > umbral_extraccion) no decide.
Requiere rendimiento.

Nivel estimado (``litros_estimados``): ``litros_finales`` solo cambia cuando hay una lectura
confiable, así que durante un viaje se queda fijo y al llegar baja de golpe todo lo consumido.
Para mostrar el nivel "en vivo", cada ventana guarda además
``último confiable - km desde entonces / rendimiento`` (también las descartadas, que tienen
odómetro aunque no tengan sensor). Es solo informativo: no interviene en ninguna decisión. Sin
rendimiento u odómetro vale lo mismo que ``litros_finales``.
"""

from __future__ import annotations

import statistics
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from datetime import datetime, timedelta
from enum import StrEnum

from .calibration import CalibrationTable, CalibStatus
from .quality import M_ANGULO, M_MUESTRAS, WindowQuality


class ResultadoEstado(StrEnum):
    """Valores permitidos por ck de fuel_processed_result.estado_resultado."""

    CONFIABLE = "confiable"
    DUDOSO = "dudoso"
    DESCARTADO = "descartado"
    CARGA_CONFIRMADA = "carga_confirmada"
    CARGA_PROBABLE = "carga_probable"
    AUMENTO_PENDIENTE = "aumento_pendiente_por_inclinacion"
    CONSUMO_NORMAL = "consumo_normal"
    POSIBLE_EXTRACCION = "posible_extraccion"
    EXTRACCION_CONFIRMADA = "extraccion_confirmada"
    CAIDA_PENDIENTE = "caida_pendiente_por_inclinacion"
    SIN_ACTUALIZACION = "sin_actualizacion"


class PendienteTipo(StrEnum):
    """Valores permitidos por ck de fuel_estado_confiable.pendiente_tipo."""

    CAIDA = "caida"
    AUMENTO = "aumento"


# estado_resultado -> (tipo_evento, estado del evento). Los demás estados no generan evento.
_EVENTOS: dict[ResultadoEstado, tuple[str, str]] = {
    ResultadoEstado.CARGA_CONFIRMADA: ("carga_confirmada", "confirmado"),
    ResultadoEstado.CARGA_PROBABLE: ("carga_probable", "abierto"),
    ResultadoEstado.POSIBLE_EXTRACCION: ("posible_extraccion", "abierto"),
    ResultadoEstado.EXTRACCION_CONFIRMADA: ("extraccion_confirmada", "confirmado"),
    ResultadoEstado.AUMENTO_PENDIENTE: (
        "aumento_pendiente_por_inclinacion",
        "abierto",
    ),
    ResultadoEstado.CAIDA_PENDIENTE: ("caida_pendiente_por_inclinacion", "abierto"),
}
_PENDIENTES_POR_INCLINACION = frozenset(
    {ResultadoEstado.AUMENTO_PENDIENTE, ResultadoEstado.CAIDA_PENDIENTE}
)

# Códigos de motivo estables (se guardan en fuel_processed_result.motivo / fuel_events.motivo).
M_PRIMERA_REFERENCIA = "primera_referencia"
M_SIN_REFERENCIA = "sin_referencia_ventana_no_valida"
M_RUIDO = "variacion_menor_al_umbral_de_ruido"
M_NIVEL_CONSERVADO = "nivel_conservado"
M_AUMENTO_DUDOSO = "aumento_dudoso"
M_VELOCIDAD_DESCONOCIDA = "velocidad_desconocida"
M_NO_CONFIRMADA_MAGNITUD = "aumento_menor_al_umbral_de_confirmacion"
M_NO_SOSTENIDA = "aumento_aun_no_sostenido"
M_CONSUMO_COMPATIBLE = "caida_compatible_con_kilometros"
M_CAIDA_NO_EXPLICADA = "caida_mayor_al_consumo_esperado"
M_EXTRACCION_SOSTENIDA = "caida_no_explicada_sostenida"
M_EXTRACCION_SIN_PERSISTENCIA = "caida_no_explicada_sin_persistencia_definida"
M_EXTRACCION_NO_SOSTENIDA = "caida_aun_no_sostenida"
M_INCLINADA_AUMENTO = "aumento_con_vehiculo_inclinado"
M_INCLINADA_CAIDA = "caida_fuerte_con_vehiculo_inclinado"
M_DUDOSA_SIN_ACCION = "ventana_dudosa_sin_actualizacion"
M_DESCARTADA = "ventana_descartada"
M_RENDIMIENTO_NO_DEFINIDO = "rendimiento_no_definido"
M_ODOMETRO_NO_DISPONIBLE = "odometro_no_disponible"
M_ODOMETRO_INCONSISTENTE = "odometro_inconsistente"
M_CALIBRACION = "calibracion_"  # + sin_lectura
M_NIVEL_COTA = "nivel_cota_"  # + zona_baja | arriba_de_calibracion
M_DIFERENCIA_MINIMA = "diferencia_es_minimo"
M_EN_MOVIMIENTO = "velocidad_alta_nivel_no_confiable"
M_DESDE_ARRIBA_CALIBRACION = "descenso_desde_arriba_de_calibracion"
M_CONCORDANCIA = "nivel_por_concordancia"

# Mínimo de muestras para que una ventana parada con pocas muestras entre a la concordancia.
MIN_MUESTRAS_CONCORDANCIA = 3


@dataclass(frozen=True, slots=True)
class VentanaEntrada:
    """Campos de fuel_windows que usa el procesador (+ posición para el evento)."""

    ventana_id: str
    imei: str
    ventana_inicio: datetime
    ventana_fin: datetime
    calidad: WindowQuality
    motivo_calidad: str | None
    sensor_mediana: float | None
    velocidad_promedio: float | None
    odometro_total: float | None
    # fuel_windows no guarda posición: el pipeline la toma de gps_records (opcional).
    latitude: float | None = None
    longitude: float | None = None
    muestras_validas: int | None = None


@dataclass(frozen=True, slots=True)
class ProcesadorConfig:
    calibracion: CalibrationTable
    calibracion_version: int
    umbral_carga_dudosa: float = 1.0
    umbral_carga_candidata: float = 2.0
    umbral_carga_confirmada: float = 3.0
    umbral_extraccion: float = 5.0
    velocidad_max_carga: float | None = None
    ventanas_sostenidas: int | None = None
    # km por litro. Es el valor que se usa, venga de donde venga; ``rendimiento_origen`` dice de
    # dónde: "config" (fuel_config), "reciente" (fuel_rendimiento_reciente) o None si no hay.
    rendimiento_default: float | None = None
    rendimiento_origen: str | None = "config"
    # Factor para pasar odometro_total a km. gps_records lo guarda en METROS (Teltonika).
    odometro_a_km: float = 0.001
    # Concordancia (ver cabecera). Cualquiera en None = desactivada.
    concordancia_ventanas: int | None = None
    concordancia_tolerancia: float | None = None  # litros (rango máx - mín permitido)
    concordancia_max_minutos: float | None = None


@dataclass(frozen=True, slots=True)
class EstadoConfiable:
    """Fila de fuel_estado_confiable (sin imei_ultimo/updated_at, que pone el pipeline)."""

    ultimo_litro_confiable: float | None = None
    fecha_ultimo_confiable: datetime | None = None
    odometro_ultimo_confiable: float | None = None
    pendiente_tipo: PendienteTipo | None = None
    pendiente_desde: datetime | None = None
    pendiente_litros: float | None = None
    pendiente_ventanas: int = 0


@dataclass(frozen=True, slots=True)
class ResultadoProcesado:
    """Fila para fuel_processed_result (sin id ni fecha_proceso)."""

    idautobus: str
    imei: str
    ventana_id: str
    ventana_inicio: datetime
    litros_calculados: float | None
    litros_finales: float | None  # valor vigente DESPUÉS de esta ventana
    ultimo_valor_confiable_anterior: float | None
    diferencia_litros: float | None
    km_desde_ultimo_confiable: float | None
    rendimiento_usado: float | None
    consumo_esperado: float | None
    exceso_no_explicado: float | None
    estado_resultado: ResultadoEstado
    actualizo_final: bool
    motivo: str
    calibracion_version_usada: int
    # Nivel estimado después de esta ventana (ver cabecera). Solo informativo.
    litros_estimados: float | None = None


@dataclass(frozen=True, slots=True)
class EventoCombustible:
    """Fila para fuel_events (sin id/created_at/updated_at)."""

    idautobus: str
    imei: str
    tipo_evento: str
    estado: str  # abierto | confirmado
    fecha_inicio: datetime
    fecha_fin: datetime | None
    litros_antes: float
    litros_despues: float
    litros_evento: float
    latitude: float | None
    longitude: float | None
    calidad_evento: str
    motivo: str
    ventana_origen_id: str


@dataclass(frozen=True, slots=True)
class SalidaProceso:
    resultado: ResultadoProcesado
    evento: EventoCombustible | None
    estado: EstadoConfiable


def _r2(x: float) -> float:
    return round(x, 2)


def _contar(
    estado: EstadoConfiable,
    tipo: PendienteTipo,
    previo: ResultadoEstado | None,
) -> int:
    """Ventanas consecutivas (incluida la actual) que repiten la condición ``tipo``."""
    if estado.pendiente_tipo is tipo and previo not in _PENDIENTES_POR_INCLINACION:
        return estado.pendiente_ventanas + 1
    return 1


def _con_pendiente(
    estado: EstadoConfiable,
    tipo: PendienteTipo,
    ventana: VentanaEntrada,
    litros: float,
    cuenta: int,
) -> EstadoConfiable:
    desde = (
        estado.pendiente_desde
        if estado.pendiente_tipo is tipo and estado.pendiente_desde is not None
        else ventana.ventana_inicio
    )
    return replace(
        estado,
        pendiente_tipo=tipo,
        pendiente_desde=desde,
        pendiente_litros=abs(_r2(litros - (estado.ultimo_litro_confiable or 0.0))),
        pendiente_ventanas=cuenta,
    )


def _actualizado(ventana: VentanaEntrada, litros: float) -> EstadoConfiable:
    """Nuevo último valor confiable: el pendiente se resuelve y se limpia."""
    return EstadoConfiable(
        ultimo_litro_confiable=litros,
        fecha_ultimo_confiable=ventana.ventana_inicio,
        odometro_ultimo_confiable=ventana.odometro_total,
    )


def _litros_estimados(
    ventana: VentanaEntrada,
    cfg: ProcesadorConfig,
    estado: EstadoConfiable,
    actualiza: bool,
) -> float | None:
    """Último confiable menos lo que se debió consumir desde entonces (ver cabecera)."""
    ultimo = estado.ultimo_litro_confiable
    if ultimo is None:
        return None
    if actualiza:
        return ultimo
    rend = cfg.rendimiento_default
    odo, base = ventana.odometro_total, estado.odometro_ultimo_confiable
    if not rend or rend <= 0 or odo is None or base is None or odo < base:
        return ultimo
    return _r2(max(0.0, ultimo - (odo - base) * cfg.odometro_a_km / rend))


def _sin_pendiente(estado: EstadoConfiable) -> EstadoConfiable:
    """Limpia el pendiente (carga/extracción) pero conserva el último valor confiable."""
    return replace(
        estado,
        pendiente_tipo=None,
        pendiente_desde=None,
        pendiente_litros=None,
        pendiente_ventanas=0,
    )


def _motivos_calidad(motivo_calidad: str | None) -> set[str]:
    if not motivo_calidad:
        return set()
    return {m.strip() for m in motivo_calidad.split(";") if m.strip()}


def _lectura_concordancia(v: VentanaEntrada, cfg: ProcesadorConfig) -> float | None:
    """Litros de una ventana si sirve para la concordancia (None si no).

    Sirven las válidas (a cualquier velocidad) y las dudosas cuyo ÚNICO problema es tener pocas
    muestras (con al menos MIN_MUESTRAS_CONCORDANCIA). Las cotas de calibración no sirven.
    """
    if v.sensor_mediana is None or v.odometro_total is None:
        return None
    if v.calidad is WindowQuality.DESCARTADA:
        return None
    if v.calidad is WindowQuality.DUDOSA and _motivos_calidad(v.motivo_calidad) != {
        M_MUESTRAS
    }:
        return None
    if (
        v.muestras_validas is not None
        and v.muestras_validas < MIN_MUESTRAS_CONCORDANCIA
    ):
        return None
    conv = cfg.calibracion.to_liters(v.sensor_mediana)
    if not conv.utilizable or conv.litros is None:
        return None
    return _r2(conv.litros)


def _nivel_por_concordancia(
    ventana: VentanaEntrada,
    recientes: Sequence[VentanaEntrada],
    cfg: ProcesadorConfig,
) -> float | None:
    """Mediana de las últimas lecturas que concuerdan (llevadas al odómetro actual), o None."""
    k = cfg.concordancia_ventanas
    tol = cfg.concordancia_tolerancia
    span = cfg.concordancia_max_minutos
    rend = cfg.rendimiento_default
    if not k or k < 2 or tol is None or not span or not rend or rend <= 0:
        return None
    actual = _lectura_concordancia(ventana, cfg)
    if actual is None or ventana.odometro_total is None:
        return None
    odo = ventana.odometro_total
    desde = ventana.ventana_inicio - timedelta(minutes=span)
    ajustadas = [actual]
    for v in sorted(recientes, key=lambda x: x.ventana_inicio, reverse=True):
        if len(ajustadas) >= k:
            break
        if v.ventana_inicio >= ventana.ventana_inicio:
            continue
        if v.ventana_inicio < desde:
            break
        litros = _lectura_concordancia(v, cfg)
        if litros is None or v.odometro_total is None:
            continue
        if v.odometro_total > odo:
            return None  # odómetro inconsistente
        # lo que se consumió desde esa lectura hasta ahora
        ajustadas.append(litros - (odo - v.odometro_total) * cfg.odometro_a_km / rend)
    if len(ajustadas) < k or max(ajustadas) - min(ajustadas) > tol:
        return None
    return _r2(statistics.median(ajustadas))


def procesar_ventana(
    ventana: VentanaEntrada,
    cfg: ProcesadorConfig,
    estado: EstadoConfiable,
    idautobus: str,
    estado_resultado_previo: ResultadoEstado | None = None,
    recientes: Sequence[VentanaEntrada] = (),
) -> SalidaProceso:
    """Clasifica una ventana. Debe llamarse en orden cronológico por autobús.

    ``estado_resultado_previo``: estado_resultado de la ventana anterior ya procesada del mismo
    autobús (el pipeline lo lee de fuel_processed_result); evita duplicar eventos.
    ``recientes``: ventanas anteriores del mismo autobús (cualquier calidad), para la
    concordancia. Si está desactivada se ignoran.
    """
    ultimo = estado.ultimo_litro_confiable

    # --- conversión sensor -> litros ----------------------------------------------------
    litros: float | None = None
    calib_estado: CalibStatus | None = None
    if ventana.sensor_mediana is not None:
        conv = cfg.calibracion.to_liters(ventana.sensor_mediana)
        calib_estado = conv.estado
        if conv.litros is not None:  # OK o cota (zona baja / arriba de calibración)
            litros = _r2(conv.litros)
    cota = calib_estado in (CalibStatus.ZONA_BAJA, CalibStatus.ARRIBA_DE_CALIBRACION)
    ref_baja = cfg.calibracion.litros_zona_baja
    ref_alta = cfg.calibracion.litros_maximos_calibrados

    # --- comparación contra el último valor confiable ----------------------------------
    diferencia: float | None = None
    km: float | None = None
    consumo: float | None = None
    exceso: float | None = None
    motivo_consumo: str | None = None  # por qué no se pudo calcular consumo_esperado
    if litros is not None and ultimo is not None:
        diferencia = _r2(litros - ultimo)
        odo, odo0 = ventana.odometro_total, estado.odometro_ultimo_confiable
        rend = cfg.rendimiento_default
        if odo is None or odo0 is None:
            motivo_consumo = M_ODOMETRO_NO_DISPONIBLE
        elif odo < odo0:
            motivo_consumo = M_ODOMETRO_INCONSISTENTE
        else:
            km = _r2((odo - odo0) * cfg.odometro_a_km)
        if rend is None or rend <= 0:
            motivo_consumo = M_RENDIMIENTO_NO_DEFINIDO
        elif km is not None:
            consumo = _r2(km / rend)
            if diferencia < 0:
                exceso = _r2(-diferencia - consumo)

    def salida(
        est: ResultadoEstado,
        motivos: list[str],
        nuevo: EstadoConfiable | None = None,
        *,
        actualiza: bool = False,
    ) -> SalidaProceso:
        """Construye resultado + evento + estado. ``nuevo`` None = estado sin cambios."""
        estado_nuevo = estado if nuevo is None else nuevo
        motivos = list(motivos)
        if cota and calib_estado is not None:
            motivos.append(M_NIVEL_COTA + calib_estado.value)
        minimo = (
            diferencia is not None
            and diferencia > 0
            and (
                calib_estado is CalibStatus.ARRIBA_DE_CALIBRACION or ultimo == ref_baja
            )
        )
        if minimo:
            motivos.append(M_DIFERENCIA_MINIMA)
        motivo = "; ".join(motivos)
        resultado = ResultadoProcesado(
            idautobus=idautobus,
            imei=ventana.imei,
            ventana_id=ventana.ventana_id,
            ventana_inicio=ventana.ventana_inicio,
            litros_calculados=litros,
            litros_finales=estado_nuevo.ultimo_litro_confiable,
            ultimo_valor_confiable_anterior=ultimo,
            diferencia_litros=diferencia,
            km_desde_ultimo_confiable=km,
            rendimiento_usado=cfg.rendimiento_default if consumo is not None else None,
            consumo_esperado=consumo,
            exceso_no_explicado=exceso,
            estado_resultado=est,
            actualizo_final=actualiza,
            motivo=motivo,
            calibracion_version_usada=cfg.calibracion_version,
            litros_estimados=_litros_estimados(ventana, cfg, estado_nuevo, actualiza),
        )
        evento = None
        if (
            est in _EVENTOS
            and est != estado_resultado_previo
            and litros is not None
            and ultimo is not None
            and diferencia is not None
        ):
            tipo_evento, estado_evento = _EVENTOS[est]
            confirmado = estado_evento == "confirmado"
            mismo_pendiente = estado.pendiente_desde is not None and (
                (
                    est is ResultadoEstado.CARGA_CONFIRMADA
                    and estado.pendiente_tipo is PendienteTipo.AUMENTO
                )
                or (
                    est is ResultadoEstado.EXTRACCION_CONFIRMADA
                    and estado.pendiente_tipo is PendienteTipo.CAIDA
                )
            )
            evento = EventoCombustible(
                idautobus=idautobus,
                imei=ventana.imei,
                tipo_evento=tipo_evento,
                estado=estado_evento,
                fecha_inicio=(
                    estado.pendiente_desde
                    if mismo_pendiente and estado.pendiente_desde is not None
                    else ventana.ventana_inicio
                ),
                fecha_fin=ventana.ventana_fin if confirmado else None,
                litros_antes=ultimo,
                litros_despues=litros,
                litros_evento=abs(diferencia),
                latitude=ventana.latitude,
                longitude=ventana.longitude,
                calidad_evento=ventana.calidad.value,
                motivo=motivo,
                ventana_origen_id=ventana.ventana_id,
            )
        return SalidaProceso(resultado, evento, estado_nuevo)

    def por_concordancia() -> SalidaProceso | None:
        """Baja el nivel si las lecturas recientes concuerdan (ver cabecera)."""
        if ultimo is None or litros is None or cota or ultimo == ref_alta:
            return None
        if consumo is None:  # sin km o sin rendimiento no se puede validar la bajada
            return None
        nivel = _nivel_por_concordancia(ventana, recientes, cfg)
        if nivel is None:
            return None
        caida_c = _r2(ultimo - nivel)
        if caida_c <= 0:  # sin trinquete: la concordancia nunca sube el nivel
            return None
        if caida_c - consumo > cfg.umbral_extraccion:
            # no la explica el consumo: eso solo lo decide una ventana válida
            return None
        return salida(
            ResultadoEstado.CONSUMO_NORMAL,
            [M_CONCORDANCIA],
            _actualizado(ventana, nivel),
            actualiza=True,
        )

    # 1. Ventana descartada: no hay lectura utilizable.
    if ventana.calidad is WindowQuality.DESCARTADA:
        return salida(
            ResultadoEstado.DESCARTADO,
            [ventana.motivo_calidad or M_DESCARTADA],
        )

    # 2. Sin lectura de sensor.
    if litros is None:
        return salida(
            ResultadoEstado.SIN_ACTUALIZACION,
            [M_CALIBRACION + (calib_estado.value if calib_estado else "sin_lectura")],
        )

    # 2b. Compuerta de velocidad: en marcha el líquido se mueve y la lectura sale más baja
    #     (4-5 L en el Yaris), así que el nivel solo se actualiza a baja velocidad.
    vmax = cfg.velocidad_max_carga
    if (
        vmax is not None
        and ventana.velocidad_promedio is not None
        and ventana.velocidad_promedio > vmax
    ):
        return por_concordancia() or salida(
            ResultadoEstado.SIN_ACTUALIZACION, [M_EN_MOVIMIENTO]
        )

    # 3. Primera referencia: solo una ventana válida puede fijar el valor inicial.
    if ultimo is None:
        if ventana.calidad is WindowQuality.VALIDA:
            return salida(
                ResultadoEstado.CONFIABLE,
                [M_PRIMERA_REFERENCIA],
                _actualizado(ventana, litros),
                actualiza=True,
            )
        return salida(ResultadoEstado.DUDOSO, [M_SIN_REFERENCIA])

    assert diferencia is not None  # litros y ultimo existen

    # Caída fuerte no explicada: True / False / None (no se puede decidir).
    caida = _r2(-diferencia)
    fuerte: bool | None = None
    if diferencia < 0:
        if exceso is not None:
            fuerte = exceso > cfg.umbral_extraccion
        elif caida <= cfg.umbral_extraccion:
            fuerte = False  # el exceso nunca podría superar el umbral
        # si no: indecidible (fuerte = None)

    # 4. Ventana dudosa.
    if ventana.calidad is WindowQuality.DUDOSA:
        if M_ANGULO in _motivos_calidad(ventana.motivo_calidad):
            if diferencia >= cfg.umbral_carga_dudosa:
                cuenta = _contar(estado, PendienteTipo.AUMENTO, estado_resultado_previo)
                return salida(
                    ResultadoEstado.AUMENTO_PENDIENTE,
                    [M_INCLINADA_AUMENTO],
                    _con_pendiente(
                        estado, PendienteTipo.AUMENTO, ventana, litros, cuenta
                    ),
                )
            if fuerte is True:
                cuenta = _contar(estado, PendienteTipo.CAIDA, estado_resultado_previo)
                return salida(
                    ResultadoEstado.CAIDA_PENDIENTE,
                    [M_INCLINADA_CAIDA],
                    _con_pendiente(
                        estado, PendienteTipo.CAIDA, ventana, litros, cuenta
                    ),
                )
        if _motivos_calidad(ventana.motivo_calidad) == {M_MUESTRAS}:
            concordada = por_concordancia()
            if concordada is not None:
                return concordada
        return salida(
            ResultadoEstado.DUDOSO,
            [M_DUDOSA_SIN_ACCION]
            + ([ventana.motivo_calidad] if ventana.motivo_calidad else []),
        )

    # 5. Ventana válida: carga, consumo o extracción.
    if diferencia >= 0:
        return _clasificar_aumento(
            ventana, cfg, estado, litros, diferencia, estado_resultado_previo, salida
        )

    if diferencia < 0 and ultimo == ref_alta:
        # Venía de "arriba de calibración" (>= último punto): el valor guardado es una
        # cota, así que consumo y extracción no se pueden distinguir. Se re-ancla.
        return salida(
            ResultadoEstado.CONSUMO_NORMAL,
            [M_DESDE_ARRIBA_CALIBRACION],
            _actualizado(ventana, litros),
            actualiza=True,
        )

    if fuerte is False:
        return salida(
            ResultadoEstado.CONSUMO_NORMAL,
            [M_CONSUMO_COMPATIBLE],
            _actualizado(ventana, litros),
            actualiza=True,
        )
    if fuerte is None:
        return salida(
            ResultadoEstado.SIN_ACTUALIZACION,
            [motivo_consumo or M_RENDIMIENTO_NO_DEFINIDO],
        )

    # Caída no explicada en ventana válida.
    cuenta = _contar(estado, PendienteTipo.CAIDA, estado_resultado_previo)
    necesarias = cfg.ventanas_sostenidas
    if necesarias is None:
        return salida(
            ResultadoEstado.POSIBLE_EXTRACCION,
            [M_CAIDA_NO_EXPLICADA, M_EXTRACCION_SIN_PERSISTENCIA],
            _con_pendiente(estado, PendienteTipo.CAIDA, ventana, litros, cuenta),
        )
    if cuenta >= necesarias:
        return salida(
            ResultadoEstado.EXTRACCION_CONFIRMADA,
            [M_CAIDA_NO_EXPLICADA, M_EXTRACCION_SOSTENIDA],
            _actualizado(ventana, litros),
            actualiza=True,
        )
    return salida(
        ResultadoEstado.POSIBLE_EXTRACCION,
        [M_CAIDA_NO_EXPLICADA, M_EXTRACCION_NO_SOSTENIDA],
        _con_pendiente(estado, PendienteTipo.CAIDA, ventana, litros, cuenta),
    )


def _clasificar_aumento(
    ventana: VentanaEntrada,
    cfg: ProcesadorConfig,
    estado: EstadoConfiable,
    litros: float,
    diferencia: float,
    previo: ResultadoEstado | None,
    salida: Callable[..., SalidaProceso],
) -> SalidaProceso:
    """Ventana válida con diferencia >= 0 (sube o se mantiene)."""
    if diferencia < cfg.umbral_carga_dudosa:
        if diferencia == 0:
            # Lectura igual a la referencia: se refresca (fecha y odómetro), el nivel no cambia.
            return salida(
                ResultadoEstado.CONFIABLE,
                [M_RUIDO],
                _actualizado(ventana, litros),
                actualiza=True,
            )
        # Subida chica = ruido: la ventana es confiable pero el nivel NO sube (sin trinquete).
        return salida(
            ResultadoEstado.CONFIABLE,
            [M_RUIDO, M_NIVEL_CONSERVADO],
            _sin_pendiente(estado),
        )
    if diferencia < cfg.umbral_carga_candidata:
        return salida(ResultadoEstado.DUDOSO, [M_AUMENTO_DUDOSO])

    # Candidata a carga: se evalúa velocidad, magnitud y persistencia.
    motivos: list[str] = []
    if cfg.velocidad_max_carga is not None and ventana.velocidad_promedio is None:
        motivos.append(M_VELOCIDAD_DESCONOCIDA)

    cuenta = _contar(estado, PendienteTipo.AUMENTO, previo)
    necesarias = cfg.ventanas_sostenidas or 1
    if diferencia < cfg.umbral_carga_confirmada:
        motivos.append(M_NO_CONFIRMADA_MAGNITUD)
    if cuenta < necesarias:
        motivos.append(M_NO_SOSTENIDA)

    if not motivos:
        return salida(
            ResultadoEstado.CARGA_CONFIRMADA,
            ["aumento_valido_sostenido"],
            _actualizado(ventana, litros),
            actualiza=True,
        )
    return salida(
        ResultadoEstado.CARGA_PROBABLE,
        motivos,
        _con_pendiente(estado, PendienteTipo.AUMENTO, ventana, litros, cuenta),
    )
