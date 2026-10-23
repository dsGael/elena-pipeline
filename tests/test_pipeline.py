"""Pruebas de la orquestación con un repositorio en memoria (sin PostgreSQL)."""

from dataclasses import asdict, replace
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from elena.pipeline import (
    MOTIVO_REEMPLAZADO,
    MOTIVO_SIN_CONFIRMAR,
    EventoAbierto,
    procesar_autobus,
)
from elena.processor import (
    EstadoConfiable,
    EventoCombustible,
    ProcesadorConfig,
    ResultadoProcesado,
    VentanaEntrada,
)
from elena.processor import (
    ResultadoEstado as R,
)
from elena.quality import M_ANGULO, WindowQuality
from tests.test_processor import SENSOR, YARIS

T0 = datetime(2026, 10, 8, 10, 0, tzinfo=ZoneInfo("America/Hermosillo"))
CFG = ProcesadorConfig(
    calibracion=YARIS,
    calibracion_version=1,
    ventanas_sostenidas=3,
    rendimiento_default=12.0,
    velocidad_max_carga=5.0,
)


def vent(
    i, litros, *, odo=100_000.0, calidad=WindowQuality.VALIDA, motivo=None, vel=0.0
):
    ini = T0 + timedelta(minutes=5 * i)
    return VentanaEntrada(
        ventana_id=f"w{i:03d}",
        imei="350000000000001",
        ventana_inicio=ini,
        ventana_fin=ini + timedelta(minutes=5),
        calidad=calidad,
        motivo_calidad=motivo,
        sensor_mediana=SENSOR[litros],
        velocidad_promedio=vel,
        odometro_total=odo,
    )


class RepoMemoria:
    """Implementa el Protocolo Repositorio guardando todo en listas/dicts."""

    def __init__(self, ventanas, posicion=(29.07, -110.95)):
        self.ventanas = list(ventanas)
        self.pos = posicion
        self.estado = EstadoConfiable()
        self.imei_ultimo = None
        self.resultados: dict[str, ResultadoProcesado] = {}
        self.eventos: dict[str, dict] = {}
        self._n = 0

    def cargar_estado(self, idautobus):
        return self.estado

    def ultimo_resultado(self, idautobus):
        if not self.resultados:
            return None
        r = max(self.resultados.values(), key=lambda x: x.ventana_inicio)
        return r.estado_resultado, r.ventana_inicio

    def ventanas_pendientes(self, idautobus, despues_de, limite):
        sin = [v for v in self.ventanas if v.ventana_id not in self.resultados]
        nuevas = [v for v in sin if despues_de is None or v.ventana_inicio > despues_de]
        nuevas.sort(key=lambda v: v.ventana_inicio)
        tardias = len(sin) - len(nuevas)
        return (nuevas if limite is None else nuevas[:limite]), tardias

    def ventanas_previas(self, idautobus, antes_de, limite):
        prev = sorted(
            (v for v in self.ventanas if v.ventana_inicio < antes_de),
            key=lambda v: v.ventana_inicio,
        )
        return prev[-limite:]

    def posicion(self, imei, desde, hasta):
        return self.pos

    def guardar_resultado(self, r):
        self.resultados.setdefault(r.ventana_id, r)

    def eventos_abiertos(self, idautobus, tipos):
        ab = [
            (k, e)
            for k, e in self.eventos.items()
            if e["estado"] == "abierto" and e["tipo_evento"] in tipos
        ]
        ab.sort(key=lambda kv: (kv[1]["fecha_inicio"], kv[1]["n"]), reverse=True)
        return [EventoAbierto(k, e["fecha_inicio"]) for k, e in ab]

    def insertar_evento(self, e: EventoCombustible):
        self._n += 1
        self.eventos[f"e{self._n}"] = {**asdict(e), "n": self._n}

    def confirmar_evento(self, id_evento, e):
        ini = self.eventos[id_evento]["fecha_inicio"]
        self.eventos[id_evento].update({**asdict(e), "fecha_inicio": ini})

    def descartar_eventos(self, ids, motivo):
        for i in ids:
            self.eventos[i]["estado"] = "descartado"
            self.eventos[i]["motivo"] += f"; {motivo}"

    def guardar_estado(self, idautobus, imei, estado):
        self.estado, self.imei_ultimo = estado, imei


def correr(repo, cfg=CFG, **kw):
    return procesar_autobus(repo, "YARIS1", cfg, **kw)


def estados(repo):
    return [
        r.estado_resultado
        for r in sorted(repo.resultados.values(), key=lambda x: x.ventana_inicio)
    ]


def tipos(repo, estado=None):
    return [
        e["tipo_evento"]
        for e in repo.eventos.values()
        if estado is None or e["estado"] == estado
    ]


def test_primera_corrida_fija_referencia_y_guarda_estado():
    repo = RepoMemoria([vent(0, 10), vent(1, 10)])
    res = correr(repo)
    assert res.procesadas == 2 and res.por_estado["confiable"] == 2
    assert repo.estado.ultimo_litro_confiable == 10.0
    assert repo.imei_ultimo == "350000000000001"
    assert len(repo.resultados) == 2 and not repo.eventos


def test_idempotente_segunda_corrida_no_hace_nada():
    repo = RepoMemoria([vent(0, 10), vent(1, 10)])
    correr(repo)
    antes = (dict(repo.resultados), repo.estado)
    res = correr(repo)
    assert res.procesadas == 0 and (repo.resultados, repo.estado) == antes


def test_corrida_incremental_continua_donde_quedo():
    repo = RepoMemoria([vent(0, 10), vent(1, 10)])
    correr(repo)
    repo.ventanas.append(vent(2, 10))
    res = correr(repo)
    assert res.procesadas == 1 and len(repo.resultados) == 3


def test_carga_sostenida_un_solo_evento_que_se_confirma():
    # 10 L, luego sube a 13 L y se mantiene; ventanas_sostenidas = 3
    repo = RepoMemoria([vent(0, 10)] + [vent(i, 13) for i in (1, 2, 3)])
    correr(repo)
    assert estados(repo) == [
        R.CONFIABLE,
        R.CARGA_PROBABLE,
        R.CARGA_PROBABLE,
        R.CARGA_CONFIRMADA,
    ]
    assert tipos(repo) == ["carga_confirmada"]  # un solo evento, ya confirmado
    ev = next(iter(repo.eventos.values()))
    assert ev["estado"] == "confirmado" and ev["litros_evento"] == 3.0
    assert ev["fecha_inicio"] == T0 + timedelta(
        minutes=5
    )  # arrancó en la primera ventana
    assert ev["latitude"] == 29.07 and ev["longitude"] == -110.95
    assert (
        repo.estado.ultimo_litro_confiable == 13.0
        and repo.estado.pendiente_tipo is None
    )


def test_pico_aislado_descarta_el_evento_abierto():
    repo = RepoMemoria([vent(0, 10), vent(1, 13), vent(2, 10)])
    correr(repo)
    assert estados(repo) == [R.CONFIABLE, R.CARGA_PROBABLE, R.CONFIABLE]
    (ev,) = repo.eventos.values()
    assert ev["estado"] == "descartado" and MOTIVO_SIN_CONFIRMAR in ev["motivo"]


def test_corridas_separadas_producen_lo_mismo_que_una_sola():
    ventanas = [vent(0, 10)] + [vent(i, 13) for i in (1, 2, 3)]
    una = RepoMemoria(ventanas)
    correr(una)
    varias = RepoMemoria(ventanas)
    for tope in (1, 2, 1):
        correr(varias, max_ventanas=tope)
    assert estados(una) == estados(varias)
    assert tipos(una) == tipos(varias) and una.estado == varias.estado


def test_extraccion_se_confirma_a_la_tercera_ventana():
    repo = RepoMemoria([vent(0, 12)] + [vent(i, 5) for i in (1, 2, 3)])
    correr(repo)
    assert estados(repo)[1:] == [
        R.POSIBLE_EXTRACCION,
        R.POSIBLE_EXTRACCION,
        R.EXTRACCION_CONFIRMADA,
    ]
    assert tipos(repo) == ["extraccion_confirmada"]
    assert repo.estado.ultimo_litro_confiable == 5.0


def test_aumento_pendiente_por_inclinacion_luego_carga_confirmada():
    ventanas = [
        vent(0, 10),
        vent(1, 13, calidad=WindowQuality.DUDOSA, motivo=M_ANGULO),
        vent(2, 13),
        vent(3, 13),
        vent(4, 13),
    ]
    repo = RepoMemoria(ventanas)
    correr(repo)
    assert estados(repo)[1:] == [
        R.AUMENTO_PENDIENTE,
        R.CARGA_PROBABLE,
        R.CARGA_PROBABLE,
        R.CARGA_CONFIRMADA,
    ]
    # un único evento confirmado; el aumento pendiente quedó reemplazado/convertido
    confirmados = [e for e in repo.eventos.values() if e["estado"] == "confirmado"]
    assert len(confirmados) == 1
    assert not [e for e in repo.eventos.values() if e["estado"] == "abierto"]
    assert any(MOTIVO_REEMPLAZADO in e["motivo"] for e in repo.eventos.values())


def test_cambio_de_direccion_descarta_el_pendiente_anterior():
    repo = RepoMemoria([vent(0, 12), vent(1, 15), vent(2, 6)])
    correr(repo)
    assert estados(repo)[1:] == [R.CARGA_PROBABLE, R.POSIBLE_EXTRACCION]
    por_tipo = {e["tipo_evento"]: e for e in repo.eventos.values()}
    assert por_tipo["carga_probable"]["estado"] == "descartado"
    assert MOTIVO_SIN_CONFIRMAR in por_tipo["carga_probable"]["motivo"]
    assert por_tipo["posible_extraccion"]["estado"] == "abierto"


def test_ventana_tardia_se_omite_y_se_cuenta():
    repo = RepoMemoria([vent(0, 10), vent(5, 10)])
    correr(repo)
    repo.ventanas.append(vent(2, 10))  # llega después, pero es anterior a la última
    res = correr(repo)
    assert res.procesadas == 0 and res.omitidas_tardias == 1
    assert "w002" not in repo.resultados


def test_ventanas_descartadas_no_mueven_el_estado_pero_se_registran():
    malas = replace(
        vent(1, 10),
        calidad=WindowQuality.DESCARTADA,
        motivo_calidad="sin_lectura_sensor",
        sensor_mediana=None,
    )
    repo = RepoMemoria([vent(0, 10), malas])
    correr(repo)
    assert estados(repo) == [R.CONFIABLE, R.DESCARTADO]
    assert repo.estado.ultimo_litro_confiable == 10.0


def test_odometro_sin_ignicion_entre_corridas_no_cuenta():
    # referencia en 100 km; 12 km "fantasma" apagado; la corrida siguiente arranca con ignición
    apagada = replace(
        vent(1, 10, odo=112_000.0),
        calidad=WindowQuality.DESCARTADA,
        sensor_mediana=None,
        ignicion_dominante=False,
    )
    encendida = replace(
        apagada,
        ventana_id="w002",
        ventana_inicio=T0 + timedelta(minutes=10),
        ventana_fin=T0 + timedelta(minutes=15),
        odometro_total=124_000.0,
        ignicion_dominante=True,
    )
    repo = RepoMemoria([vent(0, 12), apagada])
    correr(repo, max_ventanas=2)
    repo.ventanas.append(encendida)
    correr(repo)
    est = [repo.resultados[k].litros_estimados for k in ("w001", "w002")]
    assert est == [12.0, 11.0]  # solo los 12 km con ignición: 1 L


def test_sin_ventanas_no_escribe_estado():
    repo = RepoMemoria([])
    res = correr(repo)
    assert res.procesadas == 0 and repo.imei_ultimo is None


# --- concordancia ----------------------------------------------------------------------
CFG_CONC = replace(
    CFG,
    concordancia_ventanas=3,
    concordancia_tolerancia=0.6,
    concordancia_max_minutos=20,
)


def _viaje():
    # referencia 12 L parado; luego 3 ventanas en movimiento que concuerdan en ~9 L
    # (12 km/L y odómetro en metros: 12 km = 1 L)
    return [
        vent(0, 12, odo=100_000.0),
        vent(1, 11, odo=112_000.0, vel=30.0),
        vent(2, 10, odo=124_000.0, vel=30.0),
        vent(3, 9, odo=136_000.0, vel=30.0),
    ]


def test_concordancia_dentro_de_una_corrida():
    repo = RepoMemoria(_viaje())
    correr(repo, CFG_CONC)
    # la lectura parada inicial también cuenta: con 3 lecturas ya concuerda en la 3a ventana
    assert estados(repo) == [
        R.CONFIABLE,
        R.SIN_ACTUALIZACION,
        R.CONSUMO_NORMAL,
        R.CONSUMO_NORMAL,
    ]
    assert repo.estado.ultimo_litro_confiable == 9.0


def test_concordancia_usa_ventanas_de_corridas_anteriores():
    repo = RepoMemoria(_viaje())
    correr(repo, CFG_CONC, max_ventanas=2)  # la 3a llega en el siguiente ciclo
    assert repo.estado.ultimo_litro_confiable == 12.0
    correr(repo, CFG_CONC, max_ventanas=1)  # necesita las 2 anteriores, ya procesadas
    assert estados(repo)[-1] is R.CONSUMO_NORMAL
    assert repo.estado.ultimo_litro_confiable == 10.0
