import json
from decimal import Decimal

import pytest

from elena.calibration import CalibrationError, CalibrationTable, CalibStatus

YARIS = [
    {"litros": 4, "sensor": 5735},
    {"litros": 5, "sensor": 5604},
    {"litros": 6, "sensor": 5510},
    {"litros": 7, "sensor": 5344},
    {"litros": 8, "sensor": 5233},
    {"litros": 9, "sensor": 5143},
    {"litros": 10, "sensor": 5036},
    {"litros": 11, "sensor": 4953},
    {"litros": 12, "sensor": 4866},
    {"litros": 13, "sensor": 4779},
    {"litros": 14, "sensor": 4692},
    {"litros": 15, "sensor": 4618},
]


@pytest.fixture
def tabla() -> CalibrationTable:
    return CalibrationTable.from_json(YARIS)


@pytest.mark.parametrize("p", YARIS)
def test_puntos_exactos(tabla, p):
    r = tabla.to_liters(p["sensor"])
    assert r.estado in (CalibStatus.OK, CalibStatus.ZONA_BAJA)
    assert r.litros == pytest.approx(p["litros"])


def test_interpolacion_punto_medio(tabla):
    # entre 5 L (5604) y 6 L (5510): 5557 queda justo a la mitad
    r = tabla.to_liters(5557)
    assert r.estado is CalibStatus.OK
    assert r.litros == pytest.approx(5.5)


def test_interpolacion_tramo_desigual(tabla):
    # entre 6 L (5510) y 7 L (5344): 166 puntos de sensor por litro
    r = tabla.to_liters(5510 - 166 * 0.25)
    assert r.litros == pytest.approx(6.25)


def test_acepta_decimal_y_float(tabla):
    assert tabla.to_liters(Decimal(5036)).litros == pytest.approx(10)
    assert tabla.to_liters(5036.0).litros == pytest.approx(10)


def test_zona_baja_no_distingue(tabla):
    for s in (5735, 5736, 6000):
        r = tabla.to_liters(s)
        assert r.estado is CalibStatus.ZONA_BAJA
        assert r.litros == 4
        assert not r.utilizable


def test_arriba_de_calibracion_es_cota_sin_extrapolar(tabla):
    for s in (4617, 4235):
        r = tabla.to_liters(s)
        assert r.estado is CalibStatus.ARRIBA_DE_CALIBRACION
        assert r.litros == 15
        assert not r.utilizable


def test_ultimo_punto_es_valido(tabla):
    r = tabla.to_liters(4618)
    assert r.estado is CalibStatus.OK
    assert r.litros == pytest.approx(15)


def test_sin_lectura(tabla):
    assert tabla.to_liters(None).estado is CalibStatus.SIN_LECTURA
    assert tabla.to_liters(float("nan")).estado is CalibStatus.SIN_LECTURA


def test_monotonia_litros_no_baja_al_bajar_el_sensor(tabla):
    prev = -1.0
    for s in range(5604, 4618, -7):
        litros = tabla.to_liters(s).litros
        assert litros is not None and litros >= prev
        prev = litros


def test_from_json_texto_y_formato_con_puntos():
    assert CalibrationTable.from_json(json.dumps(YARIS)).to_liters(
        5036
    ).litros == pytest.approx(10)
    alt = {
        "version": 1,
        "puntos": [
            {"litros": 4, "sensor_adc": 5735},
            {"litros": 5, "sensor_adc": 5604},
        ],
    }
    assert CalibrationTable.from_json(alt).to_liters(5604).litros == pytest.approx(5)


@pytest.mark.parametrize(
    "puntos, texto",
    [
        ([{"litros": 4, "sensor": 5735}], "al menos 2"),
        ([{"litros": 5, "sensor": 5604}, {"litros": 4, "sensor": 5735}], "ascendente"),
        ([{"litros": 4, "sensor": 5600}, {"litros": 5, "sensor": 5700}], "debe bajar"),
        (
            [{"litros": 0, "sensor": 5735}, {"litros": 1, "sensor": 5735}],
            "Sensor repetido",
        ),
        ([{"litros": 4, "sensor": 5735}, {"litros": 4, "sensor": 5604}], "ascendente"),
        ([{"litros": 4}, {"litros": 5, "sensor": 5604}], "faltan"),
        ([{"litros": "x", "sensor": 1}, {"litros": 5, "sensor": 0}], "no numéricos"),
        ({"a": 1}, "arreglo"),
        ("no es json de puntos", None),
    ],
)
def test_validacion(puntos, texto):
    with pytest.raises((CalibrationError, ValueError)) as exc:
        CalibrationTable.from_json(puntos)
    if texto:
        assert texto in str(exc.value)
