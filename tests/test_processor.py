from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from elena.calibration import CalibrationTable
from elena.processor import (
    M_CARGA_CONCORDANCIA,
    M_CONCORDANCIA,
    M_NIVEL_CONSERVADO,
    EstadoConfiable,
    PendienteTipo,
    ProcesadorConfig,
    VentanaEntrada,
    procesar_ventana,
)
from elena.processor import (
    ResultadoEstado as R,
)
from elena.quality import M_ANGULO, M_MUESTRAS, WindowQuality

TZ = ZoneInfo("America/Hermosillo")
T0 = datetime(2026, 10, 8, 10, 0, tzinfo=TZ)

# Tabla Yaris 2012 (documento, sección 8)
YARIS = CalibrationTable.from_json(
    [
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
)
# sensor exacto para N litros (los puntos de la tabla caen en enteros)
SENSOR = {p.litros: p.sensor for p in YARIS.points}

# Las pruebas usan odómetro en km para leer los números fácilmente; el valor por defecto
# real (metros) se verifica en test_odometro_por_defecto_es_metros.
CFG = ProcesadorConfig(calibracion=YARIS, calibracion_version=1, odometro_a_km=1.0)
# Config con todos los parámetros opcionales definidos (valores de PRUEBA, no del vehículo).
CFG_FULL = replace(
    CFG, rendimiento_default=12.0, velocidad_max_carga=10.0, ventanas_sostenidas=2
)


def estado(litros=10.0, odo=1000.0, **kw) -> EstadoConfiable:
    return EstadoConfiable(
        ultimo_litro_confiable=litros,
        fecha_ultimo_confiable=T0 - timedelta(minutes=5),
        odometro_ultimo_confiable=odo,
        **kw,
    )


_AUTO = object()  # sensor = lectura exacta de la tabla; None = sin lectura
EST12 = estado(litros=12.0)


def ventana(
    litros=10,
    minuto=0,
    odo=1000.0,
    vel=0.0,
    calidad=WindowQuality.VALIDA,
    motivo=None,
    sensor=_AUTO,
) -> VentanaEntrada:
    ini = T0 + timedelta(minutes=minuto)
    return VentanaEntrada(
        ventana_id=f"w{minuto}",
        imei="350000000000001",
        ventana_inicio=ini,
        ventana_fin=ini + timedelta(minutes=5),
        calidad=calidad,
        motivo_calidad=motivo,
        sensor_mediana=SENSOR[litros] if sensor is _AUTO else sensor,
        velocidad_promedio=vel,
        odometro_total=odo,
        latitude=29.07,
        longitude=-110.95,
    )


def run(v, cfg=CFG, est=None, previo=None):
    return procesar_ventana(v, cfg, est or estado(), "YARIS1", previo)


# --- 1. carga ------------------------------------------------------------------------


def test_carga_confirmada_sube_3_litros_ventana_valida():
    out = run(ventana(13))
    r = out.resultado
    assert r.estado_resultado is R.CARGA_CONFIRMADA
    assert r.diferencia_litros == 3.0 and r.actualizo_final
    assert out.estado.ultimo_litro_confiable == 13.0
    assert out.estado.pendiente_tipo is None
    ev = out.evento
    assert ev.tipo_evento == "carga_confirmada" and ev.estado == "confirmado"
    assert (ev.litros_antes, ev.litros_despues, ev.litros_evento) == (10.0, 13.0, 3.0)
    assert ev.fecha_fin is not None and ev.latitude == 29.07


def test_carga_probable_entre_2_y_3_litros_no_actualiza():
    out = run(ventana(12))
    assert out.resultado.estado_resultado is R.CARGA_PROBABLE
    assert not out.resultado.actualizo_final
    assert out.resultado.litros_finales == 10.0  # el valor vigente no cambia
    assert out.estado.ultimo_litro_confiable == 10.0
    assert out.estado.pendiente_tipo is PendienteTipo.AUMENTO
    assert out.evento.tipo_evento == "carga_probable" and out.evento.estado == "abierto"
    assert out.evento.fecha_fin is None


def test_aumento_dudoso_entre_1_y_2_litros_sin_evento():
    out = run(ventana(11))
    assert out.resultado.estado_resultado is R.DUDOSO
    assert out.evento is None and not out.resultado.actualizo_final


def test_aumento_menor_a_1_litro_es_ruido_y_no_sube_el_nivel():
    out = run(ventana(sensor=5000))  # ~10.4 L, referencia 10.0
    assert 0 < out.resultado.diferencia_litros < 1
    assert out.resultado.estado_resultado is R.CONFIABLE
    assert out.evento is None and not out.resultado.actualizo_final
    assert out.resultado.litros_finales == 10.0
    assert out.estado.ultimo_litro_confiable == 10.0
    # la referencia (fecha y odómetro) tampoco se mueve
    assert out.estado.fecha_ultimo_confiable == estado().fecha_ultimo_confiable
    assert out.estado.odometro_ultimo_confiable == 1000.0
    assert M_NIVEL_CONSERVADO in out.resultado.motivo


def test_sin_trinquete_las_oscilaciones_al_alza_no_se_acumulan():
    est, previo = estado(), None
    for i, sensor in enumerate([5000, 4990, 5010, 5000, 4995]):  # 10.4 a 10.6 L
        out = procesar_ventana(
            ventana(sensor=sensor, minuto=5 * i), CFG, est, "YARIS1", previo
        )
        assert out.resultado.estado_resultado is R.CONFIABLE
        assert out.resultado.litros_finales == 10.0
        est, previo = out.estado, out.resultado.estado_resultado


def test_tras_ruido_al_alza_una_bajada_explicada_si_actualiza_el_nivel():
    o1 = run(ventana(sensor=5000))  # ~10.4 L: ruido, el nivel sigue en 10.0
    o2 = procesar_ventana(ventana(9, minuto=5), CFG, o1.estado, "YARIS1", R.CONFIABLE)
    assert o2.resultado.estado_resultado is R.CONSUMO_NORMAL
    assert o2.resultado.actualizo_final and o2.estado.ultimo_litro_confiable == 9.0


def test_lectura_igual_a_la_referencia_refresca_fecha_y_odometro():
    out = run(ventana(10, minuto=5, odo=1200.0))
    assert out.resultado.estado_resultado is R.CONFIABLE
    assert out.resultado.actualizo_final
    assert out.estado.odometro_ultimo_confiable == 1200.0


def test_ruido_que_resuelve_un_pendiente_de_carga_lo_limpia_sin_subir_el_nivel():
    o1 = run(ventana(13), CFG_FULL)  # +3 L, 1 de 2 ventanas necesarias
    assert o1.resultado.estado_resultado is R.CARGA_PROBABLE
    assert o1.estado.pendiente_tipo is PendienteTipo.AUMENTO
    o2 = procesar_ventana(
        ventana(sensor=5000, minuto=5), CFG_FULL, o1.estado, "YARIS1", R.CARGA_PROBABLE
    )
    assert o2.resultado.estado_resultado is R.CONFIABLE
    assert not o2.resultado.actualizo_final
    assert o2.estado.ultimo_litro_confiable == 10.0
    assert o2.estado.pendiente_tipo is None and o2.estado.pendiente_ventanas == 0


# --- 2. consumo y extracción -------------------------------------------------------------


def test_consumo_normal_caida_explicada_por_km():
    # 60 km a 12 km/L = 5 L esperados; cae 5 L -> exceso 0
    out = run(ventana(5, odo=1060.0), CFG_FULL)
    r = out.resultado
    assert r.estado_resultado is R.CONSUMO_NORMAL
    assert (r.km_desde_ultimo_confiable, r.consumo_esperado) == (60.0, 5.0)
    assert r.exceso_no_explicado == 0.0 and r.rendimiento_usado == 12.0
    assert out.estado.odometro_ultimo_confiable == 1060.0 and out.evento is None


def test_posible_extraccion_caida_sin_km_primera_ventana():
    # sin km: caida 7 L (12 -> 5), exceso 7 > 5; con ventanas_sostenidas=2 todavía no se confirma
    out = run(ventana(5, odo=1000.0), CFG_FULL, EST12)
    r = out.resultado
    assert r.estado_resultado is R.POSIBLE_EXTRACCION
    assert r.exceso_no_explicado == 7.0 and not r.actualizo_final
    assert out.estado.ultimo_litro_confiable == 12.0  # no baja el valor final
    assert out.estado.pendiente_tipo is PendienteTipo.CAIDA
    assert out.estado.pendiente_ventanas == 1
    assert out.evento.tipo_evento == "posible_extraccion"


def test_extraccion_confirmada_al_sostenerse_dos_ventanas():
    o1 = run(ventana(5, minuto=0), CFG_FULL, EST12)
    o2 = procesar_ventana(
        ventana(5, minuto=5),
        CFG_FULL,
        o1.estado,
        "YARIS1",
        o1.resultado.estado_resultado,
    )
    assert o2.resultado.estado_resultado is R.EXTRACCION_CONFIRMADA
    assert o2.resultado.actualizo_final and o2.estado.ultimo_litro_confiable == 5.0
    assert o2.estado.pendiente_tipo is None
    ev = o2.evento
    assert ev.tipo_evento == "extraccion_confirmada" and ev.estado == "confirmado"
    assert ev.fecha_inicio == T0  # arranca en la primera ventana de la caída
    assert ev.litros_evento == 7.0


def test_posible_extraccion_no_duplica_evento_mientras_persiste():
    cfg = replace(CFG_FULL, ventanas_sostenidas=3)
    o1 = run(ventana(5, minuto=0), cfg, EST12)
    o2 = procesar_ventana(
        ventana(5, minuto=5), cfg, o1.estado, "YARIS1", R.POSIBLE_EXTRACCION
    )
    assert o2.resultado.estado_resultado is R.POSIBLE_EXTRACCION
    assert o2.estado.pendiente_ventanas == 2 and o2.evento is None


def test_extraccion_diferida_conserva_ultimo_valor_y_compara_contra_el_():
    # Ejemplo del PDF: 12 L confiables, ventanas sin dato, regresa plano con 7 L (caída 5 L).
    est = estado(litros=12.0, odo=1000.0)
    sin_dato = run(
        ventana(
            calidad=WindowQuality.DESCARTADA,
            motivo="sin_lectura_sensor",
            sensor=None,
            minuto=5,
        ),
        CFG_FULL,
        est,
    )
    assert sin_dato.resultado.estado_resultado is R.DESCARTADO
    assert sin_dato.estado == est  # el último valor confiable no se toca
    out = procesar_ventana(
        ventana(7, minuto=15, odo=1000.0),
        CFG_FULL,
        sin_dato.estado,
        "YARIS1",
        R.DESCARTADO,
    )
    # caída 5 L, sin km: exceso 5, NO supera el umbral (> 5) -> consumo normal
    assert out.resultado.exceso_no_explicado == 5.0
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL
    out2 = procesar_ventana(ventana(6, minuto=15, odo=1000.0), CFG_FULL, est, "YARIS1")
    assert out2.resultado.estado_resultado is R.POSIBLE_EXTRACCION  # 6 L > 5 L


def test_caida_que_se_corrige_limpia_el_pendiente():
    o1 = run(ventana(5, minuto=0), CFG_FULL, EST12)
    assert o1.estado.pendiente_tipo is PendienteTipo.CAIDA
    o2 = procesar_ventana(
        ventana(12, minuto=5), CFG_FULL, o1.estado, "YARIS1", R.POSIBLE_EXTRACCION
    )
    assert o2.resultado.estado_resultado is R.CONFIABLE
    assert o2.estado.pendiente_tipo is None and o2.estado.pendiente_ventanas == 0


# --- 3. inclinación ---------------------------------------------------------------------


def test_aumento_pendiente_por_inclinacion():
    out = run(ventana(13, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO))
    assert out.resultado.estado_resultado is R.AUMENTO_PENDIENTE
    assert (
        not out.resultado.actualizo_final and out.estado.ultimo_litro_confiable == 10.0
    )
    assert out.estado.pendiente_tipo is PendienteTipo.AUMENTO
    assert out.evento.tipo_evento == "aumento_pendiente_por_inclinacion"
    assert out.evento.calidad_evento == "dudosa"


def test_caida_pendiente_por_inclinacion():
    out = run(
        ventana(5, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO), CFG_FULL, EST12
    )
    assert out.resultado.estado_resultado is R.CAIDA_PENDIENTE
    assert out.estado.pendiente_tipo is PendienteTipo.CAIDA
    assert out.evento.tipo_evento == "caida_pendiente_por_inclinacion"


def test_aumento_pendiente_se_valida_al_volver_a_plano():
    o1 = run(ventana(13, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO, minuto=0))
    o2 = procesar_ventana(
        ventana(13, minuto=5), CFG, o1.estado, "YARIS1", R.AUMENTO_PENDIENTE
    )
    assert o2.resultado.estado_resultado is R.CARGA_CONFIRMADA
    assert (
        o2.evento is not None and o2.evento.fecha_inicio == T0
    )  # desde que empezó el aumento


def test_inclinada_sin_cambio_relevante_es_dudosa():
    out = run(ventana(10, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO))
    assert out.resultado.estado_resultado is R.DUDOSO and out.evento is None


def test_dudosa_por_otro_motivo_no_actualiza_ni_genera_pendiente():
    out = run(ventana(13, calidad=WindowQuality.DUDOSA, motivo=M_MUESTRAS))
    assert out.resultado.estado_resultado is R.DUDOSO
    assert out.estado == estado() and out.evento is None
    assert M_MUESTRAS in out.resultado.motivo


# --- 4. casos de borde ------------------------------------------------------------------


def test_primera_ventana_sin_estado_previo_fija_referencia():
    out = run(ventana(10), est=EstadoConfiable())
    r = out.resultado
    assert r.estado_resultado is R.CONFIABLE and r.actualizo_final
    assert r.ultimo_valor_confiable_anterior is None and r.diferencia_litros is None
    assert out.estado.ultimo_litro_confiable == 10.0 and out.evento is None


def test_primera_ventana_dudosa_no_fija_referencia():
    out = run(
        ventana(10, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO),
        est=EstadoConfiable(),
    )
    assert out.resultado.estado_resultado is R.DUDOSO
    assert out.estado.ultimo_litro_confiable is None


def test_ventana_sin_sensor_es_descartada():
    out = run(
        ventana(
            calidad=WindowQuality.DESCARTADA, motivo="sin_lectura_sensor", sensor=None
        )
    )
    assert out.resultado.estado_resultado is R.DESCARTADO
    assert (
        out.resultado.litros_calculados is None and out.resultado.litros_finales == 10.0
    )
    assert out.evento is None


def test_zona_baja_es_cota_de_4_litros_y_actualiza():
    out = run(ventana(sensor=5800), CFG, estado(litros=4.0))
    assert out.resultado.estado_resultado is R.CONFIABLE
    assert out.resultado.litros_calculados == 4.0 and out.resultado.actualizo_final
    assert "nivel_cota_zona_baja" in out.resultado.motivo


def test_arriba_de_calibracion_es_cota_de_15_litros():
    out = run(ventana(sensor=4235), CFG, estado(litros=15.0))
    assert out.resultado.estado_resultado is R.CONFIABLE
    assert out.resultado.litros_calculados == 15.0
    assert "nivel_cota_arriba_de_calibracion" in out.resultado.motivo


def test_carga_de_reserva_a_arriba_de_calibracion_es_minimo():
    """El caso real: 20 L desde reserva. Se registra como carga de AL MENOS 11 L."""
    out = run(ventana(sensor=4235), CFG, estado(litros=4.0))
    assert out.resultado.estado_resultado is R.CARGA_CONFIRMADA
    assert out.evento is not None and out.evento.litros_evento == 11.0
    assert out.evento.litros_despues == 15.0
    assert "diferencia_es_minimo" in out.evento.motivo
    assert out.estado.ultimo_litro_confiable == 15.0


def test_carga_desde_reserva_a_nivel_medido_tambien_es_minimo():
    out = run(ventana(13), CFG, estado(litros=4.0))
    assert out.evento is not None and "diferencia_es_minimo" in out.evento.motivo


def test_carga_normal_no_se_marca_como_minimo():
    out = run(ventana(13), CFG, estado(litros=10.0))
    assert "diferencia_es_minimo" not in out.resultado.motivo


def test_descenso_desde_arriba_de_calibracion_reancla_sin_extraccion():
    out = run(ventana(8), CFG_FULL, estado(litros=15.0))  # cae 7 L > umbral
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL
    assert "descenso_desde_arriba_de_calibracion" in out.resultado.motivo
    assert out.evento is None and out.estado.ultimo_litro_confiable == 8.0


def test_sin_lectura_de_sensor_no_actualiza():
    out = run(ventana(sensor=None))
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert "sin_lectura" in out.resultado.motivo


# --- 5. parámetros NULL: la regla no se aplica -------------------------------------------


def test_sin_rendimiento_caida_chica_es_consumo_normal():
    out = run(ventana(7, odo=1100.0), CFG)  # cae 3 L <= 5
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL
    assert (
        out.resultado.consumo_esperado is None
        and out.resultado.exceso_no_explicado is None
    )


def test_sin_rendimiento_caida_grande_queda_sin_actualizacion():
    out = run(ventana(5), CFG, EST12)  # cae 7 L > 5 y no hay rendimiento
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert "rendimiento_no_definido" in out.resultado.motivo
    assert out.evento is None and out.estado == EST12


def test_sin_ventanas_sostenidas_la_extraccion_nunca_se_confirma():
    cfg = replace(CFG, rendimiento_default=12.0)
    o1 = run(ventana(5, minuto=0), cfg, EST12)
    o2 = procesar_ventana(
        ventana(5, minuto=5), cfg, o1.estado, "YARIS1", R.POSIBLE_EXTRACCION
    )
    assert o2.resultado.estado_resultado is R.POSIBLE_EXTRACCION
    assert "sin_persistencia_definida" in o2.resultado.motivo


def test_sin_velocidad_max_la_carga_no_se_condiciona_a_velocidad():
    out = run(ventana(13, vel=60.0), CFG)
    assert out.resultado.estado_resultado is R.CARGA_CONFIRMADA


def test_con_velocidad_max_en_movimiento_no_se_actualiza_el_nivel():
    out = run(ventana(13, vel=40.0), CFG_FULL)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert "velocidad_alta_nivel_no_confiable" in out.resultado.motivo
    assert out.evento is None and not out.resultado.actualizo_final


def test_compuerta_de_velocidad_aplica_tambien_a_consumo():
    out = run(ventana(8, vel=30.0), CFG_FULL)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert out.estado == estado()


def test_a_baja_velocidad_si_actualiza():
    out = run(ventana(13, vel=5.0), CFG_FULL)
    assert out.resultado.estado_resultado is not R.SIN_ACTUALIZACION


def test_carga_exige_sostenimiento_cuando_esta_definido():
    cfg = replace(CFG, ventanas_sostenidas=2)
    o1 = run(ventana(13, minuto=0), cfg)
    assert o1.resultado.estado_resultado is R.CARGA_PROBABLE
    assert "aumento_aun_no_sostenido" in o1.resultado.motivo
    o2 = procesar_ventana(
        ventana(13, minuto=5), cfg, o1.estado, "YARIS1", R.CARGA_PROBABLE
    )
    assert o2.resultado.estado_resultado is R.CARGA_CONFIRMADA
    assert o2.evento.fecha_inicio == T0


def test_pico_aislado_de_carga_no_se_confirma():
    cfg = replace(CFG, ventanas_sostenidas=2)
    o1 = run(ventana(13, minuto=0), cfg)
    o2 = procesar_ventana(
        ventana(10, minuto=5), cfg, o1.estado, "YARIS1", R.CARGA_PROBABLE
    )
    assert o2.resultado.estado_resultado is R.CONFIABLE  # volvió al nivel anterior
    assert o2.estado.pendiente_tipo is None


def test_odometro_en_metros_con_factor():
    cfg = replace(CFG_FULL, odometro_a_km=0.001)
    out = run(ventana(5, odo=1000.0 + 60_000), cfg)
    assert out.resultado.km_desde_ultimo_confiable == 60.0
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL


def test_odometro_inconsistente_caida_grande_sin_actualizacion():
    out = run(ventana(5, odo=500.0), CFG_FULL, EST12)  # odómetro menor al de referencia
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert "odometro_inconsistente" in out.resultado.motivo


def test_odometro_por_defecto_es_metros():
    cfg = ProcesadorConfig(
        calibracion=YARIS, calibracion_version=1, rendimiento_default=12.0
    )
    assert cfg.odometro_a_km == 0.001
    out = run(ventana(5, odo=1000.0 + 60_000), cfg)  # 60 000 m = 60 km
    assert out.resultado.km_desde_ultimo_confiable == 60.0
    assert out.resultado.consumo_esperado == 5.0


def test_procesa_un_autobus_con_sensor_ascendente():
    """CORBER (gps_records.fuel): la lectura sube con el nivel; el flujo no cambia."""
    corber = CalibrationTable.from_json(
        [
            {"litros": 0, "sensor": 2},
            {"litros": 5, "sensor": 700},
            {"litros": 10, "sensor": 1374},
            {"litros": 14, "sensor": 1918},
        ]
    )
    cfg = ProcesadorConfig(calibracion=corber, calibracion_version=1, odometro_a_km=1.0)
    ini = T0
    v0 = VentanaEntrada(
        "a",
        "350",
        ini,
        ini + timedelta(minutes=5),
        WindowQuality.VALIDA,
        None,
        700.0,
        0.0,
        1000.0,
        None,
        None,
    )
    o0 = procesar_ventana(v0, cfg, EstadoConfiable(), "CORBER")
    assert o0.resultado.litros_calculados == 5.0
    v1 = VentanaEntrada(
        "b",
        "350",
        ini + timedelta(minutes=5),
        ini + timedelta(minutes=10),
        WindowQuality.VALIDA,
        None,
        1374.0,
        0.0,
        1000.0,
        None,
        None,
    )
    o1 = procesar_ventana(v1, cfg, o0.estado, "CORBER", o0.resultado.estado_resultado)
    assert o1.resultado.estado_resultado is R.CARGA_CONFIRMADA
    assert o1.evento is not None and o1.evento.litros_evento == 5.0


# --- concordancia ----------------------------------------------------------------------
# CFG_FULL: rendimiento 12 km/L, velocidad_max_carga 10, odómetro en km (factor 1.0).
CFG_CONC = replace(
    CFG_FULL,
    concordancia_ventanas=3,
    concordancia_tolerancia=0.6,
    concordancia_max_minutos=20,
)


def _conc(actual, recientes, cfg=CFG_CONC, est=None, previo=None):
    return procesar_ventana(actual, cfg, est or EST12, "YARIS1", previo, recientes)


def _viaje(*puntos, vel=30.0):
    """(litros, minuto, odómetro) -> ventanas en movimiento."""
    return [ventana(lit, minuto=m, odo=o, vel=vel) for lit, m, o in puntos]


def test_concordancia_en_movimiento_baja_el_nivel():
    # 12 L en odo 1000; a 12 km/L, cada 12 km se gasta 1 L: las 3 lecturas dicen "9 L ahora"
    *prev, actual = _viaje((11, 0, 1012.0), (10, 5, 1024.0), (9, 10, 1036.0))
    out = _conc(actual, prev)
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL
    assert out.resultado.motivo == M_CONCORDANCIA and out.resultado.actualizo_final
    assert out.estado.ultimo_litro_confiable == 9.0
    assert out.estado.odometro_ultimo_confiable == 1036.0


def test_sin_concordancia_en_movimiento_no_actualiza():
    *prev, actual = _viaje((11, 0, 1012.0), (8, 5, 1024.0), (9, 10, 1036.0))
    out = _conc(actual, prev)  # ajustadas 9, 7, 9: rango 2 L > 0.6
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert out.estado.ultimo_litro_confiable == 12.0


def test_concordancia_pide_suficientes_lecturas():
    *prev, actual = _viaje((10, 5, 1024.0), (9, 10, 1036.0))
    out = _conc(actual, prev)  # 2 lecturas, se piden 3
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION


def test_concordancia_ignora_lecturas_viejas():
    *prev, actual = _viaje((11, -30, 1012.0), (10, 5, 1024.0), (9, 10, 1036.0))
    out = _conc(actual, prev)  # la de hace 40 min queda fuera de los 20 min
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION


def test_concordancia_nunca_sube_el_nivel():
    *prev, actual = _viaje((13, 0, 1000.0), (13, 5, 1000.0), (13, 10, 1000.0))
    out = _conc(actual, prev)  # todas dicen 13 L con referencia 12 L
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert out.estado.ultimo_litro_confiable == 12.0


def test_concordancia_no_decide_bajadas_que_el_consumo_no_explica():
    *prev, actual = _viaje((5, 0, 1000.0), (5, 5, 1000.0), (5, 10, 1000.0))
    out = _conc(actual, prev)  # 7 L menos sin km: posible extracción, no es su trabajo
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert out.estado.pendiente_tipo is None


def test_desactivada_se_comporta_como_antes():
    *prev, actual = _viaje((11, 0, 1012.0), (10, 5, 1024.0), (9, 10, 1036.0))
    out = _conc(actual, prev, cfg=CFG_FULL)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION


def test_fin_de_viaje_con_pocas_muestras_entra_por_concordancia():
    prev = _viaje((11, 0, 1012.0), (10, 5, 1024.0))
    actual = replace(
        ventana(
            9, minuto=10, odo=1036.0, calidad=WindowQuality.DUDOSA, motivo=M_MUESTRAS
        ),
        muestras_validas=4,
    )
    out = _conc(actual, prev)
    assert out.resultado.estado_resultado is R.CONSUMO_NORMAL
    assert out.estado.ultimo_litro_confiable == 9.0


def test_ventana_con_muy_pocas_muestras_no_entra_a_la_concordancia():
    prev = _viaje((11, 0, 1012.0), (10, 5, 1024.0))
    actual = replace(
        ventana(
            9, minuto=10, odo=1036.0, calidad=WindowQuality.DUDOSA, motivo=M_MUESTRAS
        ),
        muestras_validas=2,
    )
    out = _conc(actual, prev)
    assert out.resultado.estado_resultado is R.DUDOSO


def test_ventana_inclinada_no_entra_a_la_concordancia():
    prev = _viaje((11, 0, 1012.0), (10, 5, 1024.0))
    actual = ventana(
        9, minuto=10, odo=1036.0, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO
    )
    out = _conc(actual, prev)
    assert out.resultado.estado_resultado is R.DUDOSO



def test_concordancia_tolera_una_lectura_rara_entre_las_recientes():
    # ajustadas: 9 (m0), 9 (m5), 12 (m10, rara), 9 (actual). Las 3 más recientes no
    # concuerdan, pero 3 de las 4 sí.
    prev = _viaje((12, 0, 1000.0), (13, 10, 1024.0))
    prev.insert(1, ventana(10, minuto=5, odo=1030.0, vel=30.0, sensor=5089.5))  # 9.5 L
    actual = ventana(9, minuto=15, odo=1036.0, vel=30.0)
    out = _conc(actual, prev)
    assert out.resultado.motivo == M_CONCORDANCIA
    assert out.estado.ultimo_litro_confiable == 9.0


def test_concordancia_no_baja_del_piso_de_calibracion():
    # ajustadas 3.92, 3.96 y ~4.2 (4.2 L = sensor 5709): la mediana queda bajo 4 L
    prev = _viaje((5, 0, 1037.0), (5, 5, 1037.5))
    actual = ventana(5, minuto=10, odo=1050.0, vel=30.0, sensor=5709.0)
    out = _conc(actual, prev, est=estado(litros=6.0))
    assert out.resultado.motivo == M_CONCORDANCIA
    assert out.estado.ultimo_litro_confiable == 4.0


EST10 = estado(litros=10.0)
CFG_CARGA = replace(CFG_CONC, concordancia_carga=True)


def test_concordancia_confirma_una_carga_con_lecturas_validas():
    *prev, actual = _viaje((14, 0, 1000.0), (14, 5, 1000.0), (14, 10, 1000.0))
    out = _conc(actual, prev, cfg=CFG_CARGA, est=EST10)
    assert out.resultado.estado_resultado is R.CARGA_CONFIRMADA
    assert out.resultado.motivo == M_CARGA_CONCORDANCIA
    assert out.estado.ultimo_litro_confiable == 14.0
    assert out.evento is not None


def test_concordancia_carga_apagada_por_defecto():
    *prev, actual = _viaje((14, 0, 1000.0), (14, 5, 1000.0), (14, 10, 1000.0))
    out = _conc(actual, prev, est=EST10)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION


def test_concordancia_carga_pide_subida_minima():
    *prev, actual = _viaje((12, 0, 1000.0), (12, 5, 1000.0), (12, 10, 1000.0))
    out = _conc(actual, prev, cfg=CFG_CARGA, est=EST10)  # +2 L < umbral confirmada (3)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION


def test_concordancia_carga_ignora_ventanas_con_pocas_muestras():
    prev = _viaje((14, 0, 1000.0), (14, 5, 1000.0))
    actual = replace(
        ventana(14, minuto=10, odo=1000.0, calidad=WindowQuality.DUDOSA, motivo=M_MUESTRAS),
        muestras_validas=5,
    )
    out = _conc(actual, prev, cfg=CFG_CARGA, est=EST10)
    assert out.resultado.estado_resultado is R.DUDOSO


def test_pendiente_vencido_no_suma_a_lo_sostenido():
    viejo = estado(
        litros=10.0,
        pendiente_tipo=PendienteTipo.AUMENTO,
        pendiente_desde=T0 - timedelta(minutes=120),
        pendiente_litros=3.0,
        pendiente_ventanas=1,
    )
    # sin límite: 1 + 1 = 2 ventanas sostenidas -> carga confirmada
    assert run(ventana(13), CFG_FULL, viejo).resultado.estado_resultado is R.CARGA_CONFIRMADA
    cfg = replace(CFG_FULL, pendiente_max_minutos=60)
    out = run(ventana(13), cfg, viejo)
    assert out.resultado.estado_resultado is R.CARGA_PROBABLE
    assert out.estado.pendiente_ventanas == 1


def test_pendiente_reciente_si_suma_con_limite():
    reciente = estado(
        litros=10.0,
        pendiente_tipo=PendienteTipo.AUMENTO,
        pendiente_desde=T0 - timedelta(minutes=10),
        pendiente_litros=3.0,
        pendiente_ventanas=1,
    )
    cfg = replace(CFG_FULL, pendiente_max_minutos=60)
    assert run(ventana(13), cfg, reciente).resultado.estado_resultado is R.CARGA_CONFIRMADA


# --- nivel estimado ----------------------------------------------------------------------
# CFG_FULL: 12 km/L, odómetro en km. Referencia: 12 L en odo 1000.


def test_estimado_baja_con_los_km_aunque_la_ventana_este_descartada():
    v = ventana(minuto=5, odo=1024.0, calidad=WindowQuality.DESCARTADA, sensor=None)
    out = run(v, CFG_FULL, EST12)
    assert out.resultado.estado_resultado is R.DESCARTADO
    assert out.resultado.litros_finales == 12.0
    assert out.resultado.litros_estimados == 10.0  # 24 km / 12 km/L = 2 L


def test_estimado_en_movimiento_sin_actualizar():
    out = run(ventana(11, minuto=5, odo=1006.0, vel=30.0), CFG_FULL, EST12)
    assert out.resultado.estado_resultado is R.SIN_ACTUALIZACION
    assert out.resultado.litros_estimados == 11.5


def test_estimado_es_igual_al_final_cuando_se_actualiza():
    out = run(ventana(10, minuto=5, odo=1024.0), CFG_FULL, EST12)
    assert out.resultado.actualizo_final
    assert out.resultado.litros_estimados == out.resultado.litros_finales == 10.0


def test_estimado_sin_rendimiento_es_el_final():
    v = ventana(minuto=5, odo=1024.0, calidad=WindowQuality.DESCARTADA, sensor=None)
    out = run(v, CFG, EST12)  # CFG no tiene rendimiento
    assert out.resultado.litros_estimados == 12.0


def test_estimado_sin_referencia_es_nulo():
    v = ventana(minuto=5, odo=1024.0, calidad=WindowQuality.DESCARTADA, sensor=None)
    out = procesar_ventana(v, CFG_FULL, EstadoConfiable(), "YARIS1")
    assert out.resultado.litros_estimados is None


def test_estimado_no_baja_de_cero():
    v = ventana(minuto=5, odo=2000.0, calidad=WindowQuality.DESCARTADA, sensor=None)
    out = run(v, CFG_FULL, EST12)  # 1000 km: se "gastarían" 83 L
    assert out.resultado.litros_estimados == 0.0


# --- odómetro sin ignición ---------------------------------------------------------------
# En la cochera el odómetro avanza sin movimiento real: esos km no cuentan.


def _apagada(minuto, odo, ign=False):
    v = ventana(minuto=minuto, odo=odo, calidad=WindowQuality.DESCARTADA, sensor=None)
    return replace(v, ignicion_dominante=ign)


def test_sin_ignicion_el_odometro_no_baja_el_estimado():
    out = procesar_ventana(
        _apagada(5, 1024.0), CFG_FULL, EST12, "YARIS1", odometro_previo=1000.0
    )
    assert out.resultado.litros_estimados == 12.0
    assert out.estado.odometro_ultimo_confiable == 1024.0


def test_viaje_cochera_y_arranque_solo_cuentan_km_con_ignicion():
    # 24 km con ignición, 12 km "fantasma" apagado, luego 12 km reales con ignición
    est, prev, estimados = EST12, 1000.0, []
    for v in (
        _apagada(5, 1024.0, ign=True),
        _apagada(10, 1036.0),
        _apagada(15, 1048.0, ign=True),
    ):
        out = procesar_ventana(v, CFG_FULL, est, "YARIS1", odometro_previo=prev)
        est, prev = out.estado, v.odometro_total
        estimados.append(out.resultado.litros_estimados)
    assert estimados == [10.0, 10.0, 9.0]  # 36 km reales / 12 km/L = 3 L


def test_sin_ignicion_tampoco_cuenta_en_el_consumo_esperado():
    v = replace(ventana(12, minuto=5, odo=1024.0), ignicion_dominante=False)
    out = procesar_ventana(v, CFG_FULL, EST12, "YARIS1", odometro_previo=1000.0)
    assert out.resultado.km_desde_ultimo_confiable == 0.0
    assert out.resultado.consumo_esperado == 0.0


def test_ignicion_desconocida_si_cuenta():
    out = procesar_ventana(
        _apagada(5, 1024.0, ign=None), CFG_FULL, EST12, "YARIS1", odometro_previo=1000.0
    )
    assert out.resultado.litros_estimados == 10.0


def test_sin_odometro_previo_no_se_descuenta():
    out = procesar_ventana(_apagada(5, 1024.0), CFG_FULL, EST12, "YARIS1")
    assert out.resultado.litros_estimados == 10.0
