"""Conversión sensor analógico -> litros mediante tabla de calibración.

Reglas (documento ELENA, sección 8):
  * No se usa fórmula lineal única: se interpola entre los puntos medidos.
  * La calibración es por vehículo y vive en ``fuel_config.calibracion`` (JSONB):

        [{"litros": 4, "sensor": 5735}, {"litros": 5, "sensor": 5604}, ...]

    litros estrictamente ascendentes y sensor estrictamente descendente
    (a más combustible, menor lectura del sensor).

Casos de borde, sin extrapolar:
  * sensor >= primer punto  -> ZONA_BAJA: el nivel es "<= litros del primer punto".
  * sensor <  último punto  -> FUERA_DE_CALIBRACION: no hay datos para estimar.

Este módulo es puro: no toca base de datos ni configuración.
"""

from __future__ import annotations

import json
import math
from collections.abc import Iterable, Mapping, Sequence
from dataclasses import dataclass
from decimal import Decimal
from enum import StrEnum
from itertools import pairwise
from typing import Any


class CalibrationError(ValueError):
    """La tabla de calibración es inválida."""


class CalibStatus(StrEnum):
    OK = "ok"
    ZONA_BAJA = "zona_baja"
    FUERA_DE_CALIBRACION = "fuera_de_calibracion"
    SIN_LECTURA = "sin_lectura"


@dataclass(frozen=True, slots=True)
class CalibrationPoint:
    litros: float
    sensor: float


@dataclass(frozen=True, slots=True)
class LitersResult:
    """Resultado de convertir una lectura de sensor.

    ``litros`` es None cuando no se puede estimar (fuera de calibración o sin lectura).
    En ZONA_BAJA ``litros`` vale el primer punto de la tabla y significa "<= ese valor".
    """

    litros: float | None
    estado: CalibStatus

    @property
    def utilizable(self) -> bool:
        return self.estado is CalibStatus.OK


class CalibrationTable:
    def __init__(self, points: Iterable[CalibrationPoint]) -> None:
        pts = list(points)
        if len(pts) < 2:
            raise CalibrationError("La calibración necesita al menos 2 puntos.")
        for prev, cur in pairwise(pts):
            if cur.litros <= prev.litros:
                raise CalibrationError(
                    f"Los litros deben ir en orden ascendente sin repetirse "
                    f"({prev.litros} -> {cur.litros})."
                )
            if cur.sensor == prev.sensor:
                raise CalibrationError(
                    f"Sensor repetido ({cur.sensor}) en {prev.litros} y {cur.litros} L: "
                    f"deja un solo punto (el de menos litros) para esa lectura."
                )
            if cur.sensor > prev.sensor:
                raise CalibrationError(
                    f"El sensor debe bajar al subir los litros "
                    f"({prev.litros} L={prev.sensor} -> {cur.litros} L={cur.sensor})."
                )
        self._points: tuple[CalibrationPoint, ...] = tuple(pts)

    # -- construcción ---------------------------------------------------------

    @classmethod
    def from_json(
        cls, raw: str | bytes | Sequence[Any] | Mapping[str, Any]
    ) -> CalibrationTable:
        """Acepta el JSON de fuel_config.calibracion (texto o ya parseado).

        Tolera también ``{"puntos": [...]}`` y la llave ``sensor_adc`` en lugar de ``sensor``.
        """
        data: Any = json.loads(raw) if isinstance(raw, (str, bytes)) else raw
        if isinstance(data, Mapping):
            data = data.get("puntos")
        if not isinstance(data, Sequence) or isinstance(data, (str, bytes)):
            raise CalibrationError("La calibración debe ser un arreglo de puntos.")
        points: list[CalibrationPoint] = []
        for i, item in enumerate(data):
            if not isinstance(item, Mapping):
                raise CalibrationError(
                    f"Punto {i}: debe ser un objeto con litros y sensor."
                )
            sensor = item.get("sensor", item.get("sensor_adc"))
            litros = item.get("litros")
            if sensor is None or litros is None:
                raise CalibrationError(f"Punto {i}: faltan 'litros' o 'sensor'.")
            try:
                points.append(CalibrationPoint(float(litros), float(sensor)))
            except (TypeError, ValueError) as exc:
                raise CalibrationError(f"Punto {i}: valores no numéricos.") from exc
        return cls(points)

    # -- consulta -------------------------------------------------------------

    @property
    def points(self) -> tuple[CalibrationPoint, ...]:
        return self._points

    @property
    def litros_zona_baja(self) -> float:
        """Litros del primer punto: por debajo de este nivel el sensor no distingue."""
        return self._points[0].litros

    @property
    def litros_maximos_calibrados(self) -> float:
        """Último punto: por encima de este nivel no hay calibración."""
        return self._points[-1].litros

    def to_liters(self, sensor: float | Decimal | None) -> LitersResult:
        if sensor is None:
            return LitersResult(None, CalibStatus.SIN_LECTURA)
        x = float(sensor)
        if math.isnan(x):
            return LitersResult(None, CalibStatus.SIN_LECTURA)

        pts = self._points
        if x >= pts[0].sensor:
            return LitersResult(pts[0].litros, CalibStatus.ZONA_BAJA)
        if x < pts[-1].sensor:
            return LitersResult(None, CalibStatus.FUERA_DE_CALIBRACION)

        for a, b in pairwise(pts):
            if a.sensor >= x >= b.sensor:
                fraccion = (a.sensor - x) / (a.sensor - b.sensor)
                return LitersResult(
                    a.litros + fraccion * (b.litros - a.litros), CalibStatus.OK
                )
        # Inalcanzable: los límites ya se descartaron arriba.
        raise AssertionError("sensor dentro de rango sin tramo")  # pragma: no cover
