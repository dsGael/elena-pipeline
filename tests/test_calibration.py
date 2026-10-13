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
        (
            [
                {"litros": 4, "sensor": 5600},
                {"litros": 5, "sensor": 5700},
                {"litros": 6, "sensor": 5650},
            ],
            "monótono",
        ),
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


# --- sensor ascendente (a más combustible, mayor lectura): CORBER, gps_records.fuel -------

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
def test_ascendente_puntos_exactos(corber, p):
    r = corber.to_liters(p["sensor"])
    assert r.litros == pytest.approx(p["litros"])
    assert r.estado in (CalibStatus.OK, CalibStatus.ZONA_BAJA)


def test_ascendente_interpola(corber):
    # 1300 está entre 9 L (1232) y 10 L (1374)
    r = corber.to_liters(1300)
    assert r.estado is CalibStatus.OK
    assert r.litros == pytest.approx(9 + (1300 - 1232) / (1374 - 1232))


def test_ascendente_extremos_son_cotas(corber):
    bajo = corber.to_liters(0)
    assert bajo.estado is CalibStatus.ZONA_BAJA and bajo.litros == 0
    alto = corber.to_liters(2500)
    assert alto.estado is CalibStatus.ARRIBA_DE_CALIBRACION and alto.litros == 14
    assert not bajo.utilizable and not alto.utilizable


def test_ascendente_monotonia(corber):
    prev = -1.0
    for s in range(3, 1918, 7):
        litros = corber.to_liters(s).litros
        assert litros is not None and litros >= prev
        prev = litros
