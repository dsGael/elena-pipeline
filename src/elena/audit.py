"""Auditoría de solo lectura del esquema fuel_* y de la configuración por vehículo.

No modifica nada. Sirve para responder, antes de crear o migrar estructuras:
  * qué tablas fuel_* existen y cuáles del diseño faltan,
  * si las columnas y tipos coinciden con lo que espera el código,
  * si existen los índices únicos que usan los upserts,
  * qué parámetros de fuel_config siguen pendientes (NULL),
  * qué IMEI están mapeados a un autobús con configuración y cuándo llegó su último dato.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from psycopg import Connection

from .calibration import CalibrationError, CalibrationTable

# Generado a partir del esquema probado (sql/11_schema_elena_v2.sql).
EXPECTED_COLUMNS: dict[str, dict[str, str]] = {
    "fuel_config": {
        "idautobus": "varchar",
        "activo": "bool",
        "ventana_minutos": "int4",
        "fcc_inclinacion": "numeric",
        "angulo_max": "numeric",
        "min_muestras_ventana": "int4",
        "umbral_carga_dudosa": "numeric",
        "umbral_carga_candidata": "numeric",
        "umbral_carga_confirmada": "numeric",
        "umbral_extraccion": "numeric",
        "velocidad_max_carga": "numeric",
        "ventanas_sostenidas": "int4",
        "rendimiento_default": "numeric",
        "calibracion": "jsonb",
        "calibracion_version": "int4",
        "updated_at": "timestamptz",
    },
    "fuel_estado_confiable": {
        "idautobus": "varchar",
        "imei_ultimo": "varchar",
        "ultimo_litro_confiable": "numeric",
        "fecha_ultimo_confiable": "timestamptz",
        "odometro_ultimo_confiable": "numeric",
        "pendiente_tipo": "varchar",
        "pendiente_desde": "timestamptz",
        "pendiente_litros": "numeric",
        "pendiente_ventanas": "int4",
        "updated_at": "timestamptz",
    },
    "fuel_events": {
        "id": "uuid",
        "idautobus": "varchar",
        "imei": "varchar",
        "tipo_evento": "varchar",
        "estado": "varchar",
        "fecha_inicio": "timestamptz",
        "fecha_fin": "timestamptz",
        "litros_antes": "numeric",
        "litros_despues": "numeric",
        "litros_evento": "numeric",
        "latitude": "float8",
        "longitude": "float8",
        "ubicacion": "text",
        "calidad_evento": "varchar",
        "motivo": "text",
        "ventana_origen_id": "uuid",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
    "fuel_processed_result": {
        "id": "uuid",
        "idautobus": "varchar",
        "imei": "varchar",
        "ventana_id": "uuid",
        "ventana_inicio": "timestamptz",
        "litros_calculados": "numeric",
        "litros_finales": "numeric",
        "ultimo_valor_confiable_anterior": "numeric",
        "diferencia_litros": "numeric",
        "km_desde_ultimo_confiable": "numeric",
        "rendimiento_usado": "numeric",
        "consumo_esperado": "numeric",
        "exceso_no_explicado": "numeric",
        "estado_resultado": "varchar",
        "actualizo_final": "bool",
        "motivo": "text",
        "calibracion_version_usada": "int4",
        "fecha_proceso": "timestamptz",
    },
    "fuel_rejected_data": {
        "id": "uuid",
        "imei": "varchar",
        "ventana_id": "uuid",
        "raw_data_id": "uuid",
        "fecha_hora": "timestamptz",
        "s_analogo": "numeric",
        "inclinacion": "numeric",
        "angulo_corregido": "numeric",
        "litros_candidato": "numeric",
        "motivo_rechazo": "varchar",
        "regla_aplicada": "varchar",
        "observacion": "text",
        "created_at": "timestamptz",
    },
    "fuel_rendimiento_100km": {
        "id": "uuid",
        "idautobus": "varchar",
        "imei": "varchar",
        "fecha_inicio": "timestamptz",
        "fecha_fin": "timestamptz",
        "odometro_inicio": "numeric",
        "odometro_fin": "numeric",
        "km_recorridos": "numeric",
        "litros_inicio": "numeric",
        "litros_fin": "numeric",
        "litros_cargados": "numeric",
        "litros_consumidos": "numeric",
        "rendimiento_km_l": "numeric",
        "estado": "varchar",
        "observacion": "text",
        "created_at": "timestamptz",
    },
    "fuel_resumen_diario": {
        "id": "uuid",
        "idautobus": "varchar",
        "fecha": "date",
        "km_recorridos": "numeric",
        "litros_consumidos": "numeric",
        "rendimiento_promedio": "numeric",
        "litros_cargados": "numeric",
        "num_cargas": "int4",
        "num_posibles_extracciones": "int4",
        "tiempo_ralenti_minutos": "int4",
        "voltaje_minimo": "numeric",
        "total_alertas": "int4",
        "ventanas_validas": "int4",
        "ventanas_totales": "int4",
        "calidad_medicion_pct": "numeric",
        "created_at": "timestamptz",
        "updated_at": "timestamptz",
    },
    "fuel_windows": {
        "id": "uuid",
        "imei": "varchar",
        "ventana_inicio": "timestamptz",
        "ventana_fin": "timestamptz",
        "muestras_totales": "int4",
        "muestras_validas": "int4",
        "sensor_mediana": "numeric",
        "sensor_promedio": "numeric",
        "sensor_min": "numeric",
        "sensor_max": "numeric",
        "inclinacion_mediana": "numeric",
        "angulo_corregido": "numeric",
        "porcentaje_angulo_valido": "numeric",
        "velocidad_promedio": "numeric",
        "ignicion_dominante": "bool",
        "odometro_total": "numeric",
        "calidad_ventana": "varchar",
        "motivo_calidad": "text",
        "created_at": "timestamptz",
    },
}

# fuel_config: parámetros que el documento no define; mientras sean NULL, esa regla no se aplica.
PARAMETROS_PENDIENTES = (
    "min_muestras_ventana",
    "velocidad_max_carga",
    "ventanas_sostenidas",
    "rendimiento_default",
)

_SHORT_TYPES = {
    "character varying": "varchar",
    "timestamp with time zone": "timestamptz",
    "double precision": "float8",
    "integer": "int4",
    "boolean": "bool",
    "smallint": "int2",
}


@dataclass(slots=True)
class AuditReport:
    tablas_fuel: dict[str, int] = field(default_factory=dict)  # tabla -> nº de columnas
    bloqueos: list[str] = field(default_factory=list)  # impiden consolidar
    avisos: list[str] = field(default_factory=list)
    info: list[str] = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.bloqueos

    def render(self) -> str:
        lineas: list[str] = ["== Tablas fuel_* encontradas =="]
        for tabla in sorted(self.tablas_fuel):
            marca = (
                "esperada"
                if tabla in EXPECTED_COLUMNS
                else "NO esperada (legado u otro)"
            )
            lineas.append(
                f"  {tabla:<28} {self.tablas_fuel[tabla]:>3} columnas  [{marca}]"
            )
        faltan = sorted(set(EXPECTED_COLUMNS) - set(self.tablas_fuel))
        for tabla in faltan:
            lineas.append(f"  {tabla:<28} FALTA")
        for titulo, items in (
            ("BLOQUEOS (hay que resolverlos antes de consolidar)", self.bloqueos),
            ("AVISOS", self.avisos),
            ("INFORMACION", self.info),
        ):
            lineas.append("")
            lineas.append(f"== {titulo} ==")
            if items:
                lineas.extend(f"  - {x}" for x in items)
            else:
                lineas.append("  (ninguno)")
        lineas.append("")
        lineas.append(
            "RESULTADO: " + ("LISTO para consolidar" if self.ok else "NO listo")
        )
        return "\n".join(lineas)


def _columns(conn: Connection) -> dict[str, dict[str, str]]:
    rows = conn.execute(
        "SELECT table_name, column_name, data_type FROM information_schema.columns "
        "WHERE table_schema = 'public' AND table_name LIKE 'fuel\\_%' "
        "ORDER BY table_name, ordinal_position"
    ).fetchall()
    out: dict[str, dict[str, str]] = {}
    for tabla, col, tipo in rows:
        out.setdefault(tabla, {})[col] = _SHORT_TYPES.get(tipo, tipo)
    return out


def _unique_index_columns(conn: Connection, tabla: str) -> list[tuple[str, ...]]:
    """Columnas de cada índice único NO parcial de la tabla (los upserts exigen no parcial)."""
    rows = conn.execute(
        """
        SELECT array_agg(a.attname ORDER BY k.ord)
        FROM pg_index i
        JOIN LATERAL unnest(i.indkey) WITH ORDINALITY AS k(attnum, ord) ON true
        JOIN pg_attribute a ON a.attrelid = i.indrelid AND a.attnum = k.attnum
        WHERE i.indrelid = to_regclass(%(t)s) AND i.indisunique AND i.indpred IS NULL
        GROUP BY i.indexrelid
        """,
        {"t": f"public.{tabla}"},
    ).fetchall()
    return [tuple(r[0]) for r in rows]


def run_audit(conn: Connection, *, now: datetime | None = None) -> AuditReport:
    now = now or datetime.now(UTC)
    rep = AuditReport()
    cols = _columns(conn)
    rep.tablas_fuel = {t: len(c) for t, c in cols.items()}

    # --- tablas y columnas ---------------------------------------------------------------
    for tabla, esperadas in EXPECTED_COLUMNS.items():
        if tabla not in cols:
            nivel = (
                rep.bloqueos if tabla in ("fuel_windows", "fuel_config") else rep.avisos
            )
            nivel.append(f"{tabla}: no existe.")
            continue
        reales = cols[tabla]
        faltan = sorted(set(esperadas) - set(reales))
        sobran = sorted(set(reales) - set(esperadas))
        distintos = sorted(
            c for c in set(esperadas) & set(reales) if esperadas[c] != reales[c]
        )
        nivel = rep.bloqueos if tabla in ("fuel_windows", "fuel_config") else rep.avisos
        if faltan:
            nivel.append(f"{tabla}: faltan columnas {faltan}")
        if distintos:
            nivel.append(
                f"{tabla}: tipos distintos "
                + ", ".join(
                    f"{c} (esperado {esperadas[c]}, real {reales[c]})"
                    for c in distintos
                )
            )
        if sobran:
            rep.avisos.append(f"{tabla}: columnas no previstas {sobran}")
    for tabla in sorted(set(cols) - set(EXPECTED_COLUMNS)):
        rep.avisos.append(
            f"{tabla}: tabla fuel_* fuera del diseño (probable legado); no se toca."
        )

    # --- índices necesarios para los upserts -------------------------------------------------
    if "fuel_windows" in cols and (
        "imei",
        "ventana_inicio",
    ) not in _unique_index_columns(conn, "fuel_windows"):
        rep.bloqueos.append(
            "fuel_windows: falta un UNIQUE (imei, ventana_inicio) no parcial "
            "(lo exige ON CONFLICT del upsert)."
        )
    uniques = _unique_index_columns(conn, "dispositivos")
    partial_imei = conn.execute(
        "SELECT count(*) FROM pg_index i JOIN pg_attribute a "
        "ON a.attrelid = i.indrelid AND a.attnum = i.indkey[0] "
        "WHERE i.indrelid = to_regclass('public.dispositivos') AND i.indisunique "
        "AND i.indnatts = 1 AND a.attname = 'imei'"
    ).fetchone()
    if ("imei",) not in uniques and not (partial_imei and partial_imei[0]):
        rep.avisos.append(
            "dispositivos: no hay índice único sobre imei (el mapa IMEI->autobús puede ser ambiguo)."
        )
    dup = conn.execute(
        "SELECT count(*) FROM (SELECT imei FROM public.dispositivos WHERE imei IS NOT NULL "
        "GROUP BY imei HAVING count(*) > 1) x"
    ).fetchone()
    if dup and dup[0]:
        rep.bloqueos.append(
            f"dispositivos: {dup[0]} IMEI repetidos; el mapeo a autobús es ambiguo."
        )

    # --- configuración por vehículo ------------------------------------------------------------
    if "fuel_config" in cols and "calibracion" in cols["fuel_config"]:
        pend = ", ".join(
            f"c.{p}" for p in PARAMETROS_PENDIENTES if p in cols["fuel_config"]
        )
        filas = conn.execute(
            f"SELECT c.idautobus, c.activo, c.fcc_inclinacion, c.angulo_max, c.calibracion"
            f"{', ' + pend if pend else ''} FROM public.fuel_config c ORDER BY c.idautobus"
        ).fetchall()
        if not filas:
            rep.bloqueos.append("fuel_config: no hay ningún vehículo configurado.")
        nombres = [p for p in PARAMETROS_PENDIENTES if p in cols["fuel_config"]]
        for fila in filas:
            bus, activo, fcc, amax, calib, *valores = fila
            rep.info.append(
                f"fuel_config[{bus}]: activo={activo}, FCC={float(fcc)}, ángulo máx.={float(amax)}"
            )
            try:
                t = CalibrationTable.from_json(calib)
                rep.info.append(
                    f"fuel_config[{bus}]: calibración con {len(t.points)} puntos "
                    f"({t.litros_zona_baja:g} L ... {t.litros_maximos_calibrados:g} L)"
                )
            except CalibrationError as exc:
                rep.bloqueos.append(f"fuel_config[{bus}]: calibración inválida: {exc}")
            nulos = [n for n, v in zip(nombres, valores, strict=True) if v is None]
            if nulos:
                rep.avisos.append(
                    f"fuel_config[{bus}]: parámetros pendientes (NULL): {', '.join(nulos)}"
                )

        # --- dispositivos y último dato -------------------------------------------------------
        mapeados = conn.execute(
            "SELECT d.imei, d.idautobus FROM public.dispositivos d "
            "JOIN public.fuel_config c ON c.idautobus = d.idautobus AND c.activo "
            "WHERE d.imei IS NOT NULL ORDER BY d.idautobus, d.imei"
        ).fetchall()
        if not mapeados:
            rep.bloqueos.append(
                "ningún IMEI está asignado a un autobús con fuel_config activa."
            )
        for imei, bus in mapeados:
            row = conn.execute(
                "SELECT max(recorded_at) FROM public.gps_records WHERE imei = %s",
                (imei,),
            ).fetchone()
            ultimo = row[0] if row else None
            if ultimo is None:
                rep.avisos.append(f"IMEI {imei} ({bus}): sin registros en gps_records.")
            else:
                mins = (now - ultimo).total_seconds() / 60
                rep.info.append(
                    f"IMEI {imei} ({bus}): último dato {ultimo.astimezone(UTC):%Y-%m-%d %H:%M} UTC (hace {mins:.0f} min)"
                )
        sin_config = conn.execute(
            "SELECT count(*) FROM public.dispositivos d WHERE d.imei IS NOT NULL AND NOT EXISTS ("
            "SELECT 1 FROM public.fuel_config c WHERE c.idautobus = d.idautobus AND c.activo)"
        ).fetchone()
        if sin_config and sin_config[0]:
            rep.info.append(
                f"{sin_config[0]} dispositivos con IMEI no se consolidan (sin autobús o sin fuel_config activa)."
            )
    return rep
