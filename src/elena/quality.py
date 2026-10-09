"""Calidad de una ventana de 5 minutos (documento ELENA, secciones 3, 6, 7 y 12).

La ventana se evalúa COMPLETA. No se borran muestras sueltas: el ángulo se mide sobre la
mediana de la ventana (x_corregido = mediana(inclinación) - FCC, válida si |x| <= angulo_max)
y además se guarda el porcentaje de muestras con ángulo válido para diagnóstico.

Resultado de calidad:
  valida      -> puede usarse para calcular litros y actualizar el valor final.
  dudosa      -> hay dato, pero no basta para actualizar (se conserva para diagnóstico y para
                 los estados "pendiente por inclinación"). Es el caso del ángulo fuera de rango.
  descartada  -> falla una regla principal: no hay lectura de sensor utilizable.

Reglas con umbral que el documento NO define (quedan desactivadas mientras sean None):
  min_muestras, max_rango_sensor, porcentaje_angulo_min.

Ignición apagada NO invalida por defecto: normalmente se carga combustible con el motor
apagado y descartar esas ventanas impediría detectar la carga. Se activa con
``ignicion_apagada_invalida=True`` si se confirma que el sensor no es confiable sin ignición.

Módulo puro: no toca base de datos ni configuración.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum


class WindowQuality(StrEnum):
    VALIDA = "valida"
    DUDOSA = "dudosa"
    DESCARTADA = "descartada"


# Códigos de motivo estables (se guardan en fuel_windows.motivo_calidad).
M_SIN_SENSOR = "sin_lectura_sensor"
M_MUESTRAS = "muestras_insuficientes"
M_SENSOR_INESTABLE = "sensor_inestable"
M_SIN_INCLINACION = "sin_dato_inclinacion"
M_ANGULO = "angulo_fuera_de_rango"
M_ANGULO_INESTABLE = "angulo_inestable"
M_IGNICION = "ignicion_apagada"


@dataclass(frozen=True, slots=True)
class WindowStats:
    """Agregados de una ventana, tal como salen de la consolidación."""

    muestras_totales: int
    muestras_validas: int  # muestras con s_analogo no nulo
    sensor_mediana: float | None
    sensor_min: float | None
    sensor_max: float | None
    inclinacion_mediana: float | None
    muestras_con_angulo: int  # muestras con inclinación no nula
    muestras_angulo_valido: int  # de ellas, |inclinación - FCC| <= angulo_max
    muestras_con_ignicion: int
    muestras_ignicion_on: int


@dataclass(frozen=True, slots=True)
class QualityRules:
    fcc_inclinacion: float
    angulo_max: float = 5.0
    min_muestras: int | None = None
    max_rango_sensor: float | None = None
    porcentaje_angulo_min: float | None = None
    ignicion_apagada_invalida: bool = False


@dataclass(frozen=True, slots=True)
class QualityResult:
    calidad: WindowQuality
    motivos: tuple[str, ...]
    angulo_corregido: float | None
    porcentaje_angulo_valido: float | None
    ignicion_dominante: bool | None

    @property
    def motivo(self) -> str | None:
        """Texto para fuel_windows.motivo_calidad (None si la ventana es válida)."""
        return "; ".join(self.motivos) if self.motivos else None


def evaluate_window(stats: WindowStats, rules: QualityRules) -> QualityResult:
    descartada: list[str] = []
    dudosa: list[str] = []

    # --- sensor -------------------------------------------------------------------------
    if stats.muestras_validas <= 0 or stats.sensor_mediana is None:
        descartada.append(M_SIN_SENSOR)
    else:
        if (
            rules.min_muestras is not None
            and stats.muestras_validas < rules.min_muestras
        ):
            dudosa.append(M_MUESTRAS)
        if (
            rules.max_rango_sensor is not None
            and stats.sensor_min is not None
            and stats.sensor_max is not None
            and (stats.sensor_max - stats.sensor_min) > rules.max_rango_sensor
        ):
            dudosa.append(M_SENSOR_INESTABLE)

    # --- ángulo (regla de oro, sección 7) ------------------------------------------------
    angulo: float | None = None
    porcentaje: float | None = None
    if stats.inclinacion_mediana is None or stats.muestras_con_angulo <= 0:
        dudosa.append(M_SIN_INCLINACION)
    else:
        # Se redondea antes de decidir para que lo guardado coincida con la decisión.
        angulo = round(stats.inclinacion_mediana - rules.fcc_inclinacion, 2)
        if abs(angulo) > rules.angulo_max:
            dudosa.append(M_ANGULO)
        porcentaje = round(
            100.0 * stats.muestras_angulo_valido / stats.muestras_con_angulo, 2
        )
        if (
            rules.porcentaje_angulo_min is not None
            and porcentaje < rules.porcentaje_angulo_min
        ):
            dudosa.append(M_ANGULO_INESTABLE)

    # --- ignición -----------------------------------------------------------------------
    ignicion: bool | None = None
    if stats.muestras_con_ignicion > 0:
        ignicion = stats.muestras_ignicion_on / stats.muestras_con_ignicion >= 0.5
        if rules.ignicion_apagada_invalida and not ignicion:
            dudosa.append(M_IGNICION)

    if descartada:
        calidad = WindowQuality.DESCARTADA
    elif dudosa:
        calidad = WindowQuality.DUDOSA
    else:
        calidad = WindowQuality.VALIDA

    return QualityResult(
        calidad=calidad,
        motivos=tuple(descartada + dudosa),
        angulo_corregido=angulo,
        porcentaje_angulo_valido=porcentaje,
        ignicion_dominante=ignicion,
    )
