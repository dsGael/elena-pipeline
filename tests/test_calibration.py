import json
from decimal import Decimal

import pytest

from elena.calibration import CalibrationError, CalibrationTable, CalibStatus

# Sensor ascendente (a más combustible, mayor lectura): CORBER, gps_records.fuel
CORBER = [
    {"litros": 0, "sensor": 2},
    {"litros": 1, "sensor": 151},
    {"litros": 2, "sensor": 289},
    {"litros": 3, "sensor": 425},
    {"litros": 4, "sensor": 560},
    {"litros": 5, "sensor": 700},
    {"litros": 6, "sensor": 832},
    {"litros": 7, "sensor": 967},
    {"litros": 8, "sensor": 1103},
    {"litros": 9, "sensor": 1232},
    {"litros": 10, "sensor": 1374},
    {"litros": 11, "sensor": 1511},
    {"litros": 12, "sensor": 1651},
    {"litros": 13, "sensor": 1777},
    {"litros": 14, "sensor": 1918},
]


@pytest.fixture
def corber() -> CalibrationTable:
    return CalibrationTable.from_json(CORBER)


@pytest.mark.parametrize("p", CORBER)
def test_puntos_exactos(corber, p):
    r = corber.to_liters(p["sensor"])
    assert r.litros == pytest.approx(p["litros"])
    assert r.estado in (CalibStatus.OK, CalibStatus.ZONA_BAJA)


def test_interpola(corber):
    # 1300 está entre 9 L (1232) y 10 L (1374)
    r = corber.to_liters(1300)
    assert r.estado is CalibStatus.OK
    assert r.litros == pytest.approx(9 + (1300 - 1232) / (1374 - 1232))


def test_acepta_decimal_y_float(corber):
    assert corber.to_liters(Decimal(1374)).litros == pytest.approx(10)
    assert corber.to_liters(1374.0).litros == pytest.approx(10)


def test_extremos_son_cotas_sin_extrapolar(corber):
    bajo = corber.to_liters(0)
    assert bajo.estado is CalibStatus.ZONA_BAJA and bajo.litros == 0
    alto = corber.to_liters(2500)
    assert alto.estado is CalibStatus.ARRIBA_DE_CALIBRACION and alto.litros == 14
    assert not bajo.utilizable and not alto.utilizable


def test_ultimo_punto_es_valido(corber):
    r = corber.to_liters(1918)
    assert r.estado is CalibStatus.OK
    assert r.litros == pytest.approx(14)


def test_sin_lectura(corber):
    assert corber.to_liters(None).estado is CalibStatus.SIN_LECTURA
    assert corber.to_liters(float("nan")).estado is CalibStatus.SIN_LECTURA


def test_monotonia(corber):
    prev = -1.0
    for s in range(3, 1918, 7):
        litros = corber.to_liters(s).litros
        assert litros is not None and litros >= prev
        prev = litros


def test_from_json_texto_y_formato_con_puntos():
    assert CalibrationTable.from_json(json.dumps(CORBER)).to_liters(
        1374
    ).litros == pytest.approx(10)
    alt = {
        "version": 1,
        "puntos": [
            {"litros": 4, "sensor_adc": 560},
            {"litros": 5, "sensor_adc": 700},
        ],
    }
    assert CalibrationTable.from_json(alt).to_liters(700).litros == pytest.approx(5)


@pytest.mark.parametrize(
    "puntos, texto",
    [
        ([{"litros": 4, "sensor": 560}], "al menos 2"),
        ([{"litros": 5, "sensor": 700}, {"litros": 4, "sensor": 560}], "ascendente"),
        # sensor descendente (como el antiguo s_analogo): ya no se acepta
        ([{"litros": 4, "sensor": 5735}, {"litros": 5, "sensor": 5604}], "debe subir"),
        (
            [
                {"litros": 4, "sensor": 560},
                {"litros": 5, "sensor": 700},
                {"litros": 6, "sensor": 650},
            ],
            "debe subir",
        ),
        (
            [{"litros": 0, "sensor": 560}, {"litros": 1, "sensor": 560}],
            "Sensor repetido",
        ),
        ([{"litros": 4, "sensor": 560}, {"litros": 4, "sensor": 700}], "ascendente"),
        ([{"litros": 4}, {"litros": 5, "sensor": 700}], "faltan"),
        ([{"litros": "x", "sensor": 1}, {"litros": 5, "sensor": 2}], "no numéricos"),
        ({"a": 1}, "arreglo"),
        ("no es json de puntos", None),
    ],
)
def test_validacion(puntos, texto):
    with pytest.raises((CalibrationError, ValueError)) as exc:
        CalibrationTable.from_json(puntos)
    if texto:
        assert texto in str(exc.value)
