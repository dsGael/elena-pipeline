from dataclasses import replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from elena.calibration import CalibrationTable
from elena.processor import (
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


def test_aumento_menor_a_1_litro_es_ruido_y_se_acepta():
    out = run(ventana(sensor=5000))  # ~10.4 L (sensor entre 5036 y 4953)
    assert out.resultado.diferencia_litros < 1
    assert out.resultado.estado_resultado is R.CONFIABLE
    assert out.evento is None and out.resultado.actualizo_final


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
