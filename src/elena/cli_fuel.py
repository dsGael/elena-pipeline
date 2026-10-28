"""Comandos de CLI del procesamiento de combustible.

Se registran en el CLI principal (cli.py) con dos líneas:

    from .cli_fuel import app as fuel_app
    app.add_typer(fuel_app)

Comandos:
    elena-fuel audit-schema
    elena-fuel consolidate [--desde ...] [--hasta ...] [--imei ...] [--dry-run] [--show N]
    elena-fuel estimar-rendimiento --idautobus X [--desde ...] [--hasta ...]
    elena-fuel rendimiento-reciente [--idautobus X] [--km 100] [--dias 30] [--guardar]
    elena-fuel historial-rendimiento [--idautobus X] [--km-bloque 50] [--desde ...] [--guardar]
    elena-fuel run-cycle [--log-dir logs]      (lo que ejecuta el Task Scheduler)
"""

from __future__ import annotations

import logging
from datetime import UTC, datetime, timedelta
from typing import Annotated
from zoneinfo import ZoneInfo

import typer

from .audit import run_audit
from .consolidation import (
    WINDOW_MINUTES,
    ConsolidationSummary,
    NoStartError,
    consolidate,
    plan_range,
)
from .db import get_connection
from .fuel_repo import (
    ConfigError,
    autobuses_activos,
    bloques_rendimiento_nuevos,
    cargar_config,
    cargar_puntos_rendimiento,
    guardar_bloques,
    guardar_rendimiento_reciente,
    rendimiento_reciente,
)
from .quality import QualityRules
from .rendimiento import BloqueRendimiento, RendimientoReciente, estimar_rendimiento
from .scheduled_worker import configurar_logging, ejecutar_ciclo
from .settings import settings

app = typer.Typer(no_args_is_help=True, add_completion=False)
log = logging.getLogger("elena.cli")


def _tz() -> ZoneInfo:
    return ZoneInfo(settings.app_timezone)


def _parse_local(texto: str | None, option: str) -> datetime | None:
    """'2026-10-08 10:00' se interpreta en la zona local (settings.app_timezone)."""
    if texto is None:
        return None
    try:
        dt = datetime.fromisoformat(texto)
    except ValueError as exc:
        raise typer.BadParameter(
            f"{option}: usa el formato 'AAAA-MM-DD HH:MM'."
        ) from exc
    return dt.replace(tzinfo=_tz()) if dt.tzinfo is None else dt


def _fmt(ts: datetime) -> str:
    return ts.astimezone(_tz()).strftime("%Y-%m-%d %H:%M")


def _setup_logging(verbose: bool) -> None:
    logging.basicConfig(
        level=logging.DEBUG if verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )


@app.command("audit-schema")
def audit_schema() -> None:
    """Revisa (solo lectura) las tablas fuel_*, índices, configuración e IMEI mapeados."""
    with get_connection() as conn:
        report = run_audit(conn)
    typer.echo(report.render())
    raise typer.Exit(code=0 if report.ok else 1)


def _print_summary(s: ConsolidationSummary, show: int) -> None:
    typer.echo(
        f"Rango:        {_fmt(s.desde)} -> {_fmt(s.hasta)} ({settings.app_timezone}, fin exclusivo)"
    )
    typer.echo(f"Dispositivos: {len(s.imeis)}")
    typer.echo(
        f"Ventanas:     {s.ventanas}  (nuevas {s.insertadas}, actualizadas {s.actualizadas})"
    )
    if s.por_calidad:
        typer.echo(
            "Calidad:      "
            + "  ".join(f"{k}={v}" for k, v in sorted(s.por_calidad.items()))
        )
    if s.por_motivo:
        typer.echo(
            "Motivos:      "
            + "  ".join(f"{k}={v}" for k, v in s.por_motivo.most_common())
        )
    for a in s.advertencias:
        typer.echo(f"ADVERTENCIA:  {a}")
    if s.muestra and show > 0:
        typer.echo("")
        typer.echo(
            f"{'inicio (local)':<17} {'imei':<16} {'n/val':>7} {'sensor':>8} {'litros':>7} {'graf.':>7} {'ang.corr':>9} {'%ang':>6}  calidad"
        )
        for w in s.muestra[:show]:
            ang = "-" if w.angulo_corregido is None else f"{w.angulo_corregido:.2f}"
            pct = (
                "-"
                if w.porcentaje_angulo_valido is None
                else f"{w.porcentaje_angulo_valido:.0f}"
            )
            sensor = "-" if w.sensor_mediana is None else f"{w.sensor_mediana:.0f}"
            litros = "-" if w.litros is None else f"{w.litros:.2f}"
            graf = "-" if w.litros_grafica is None else f"{w.litros_grafica:.2f}"
            motivo = f" ({w.motivo_calidad})" if w.motivo_calidad else ""
            calib = (
                f" [calibración: {w.estado_calibracion or 'inválida'}]"
                if w.estado_calibracion != "ok"
                else ""
            )
            typer.echo(
                f"{_fmt(w.ventana_inicio):<17} {w.imei:<16} "
                f"{w.muestras_totales:>3}/{w.muestras_validas:<3} {sensor:>8} {litros:>7} {graf:>7} {ang:>9} {pct:>6}  "
                f"{w.calidad_ventana}{motivo}{calib}"
            )


@app.command("consolidate")
def consolidate_cmd(
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Sin él, retoma desde la última ventana guardada."
        ),
    ] = None,
    hasta: Annotated[
        str | None,
        typer.Option(help="Fin local exclusivo. Sin él, la última ventana ya cerrada."),
    ] = None,
    imei: Annotated[str | None, typer.Option(help="Procesa solo este IMEI.")] = None,
    lag: Annotated[
        int, typer.Option(help="Colchón en minutos para datos tardíos.")
    ] = 2,
    overlap: Annotated[
        int, typer.Option(help="Minutos que se reprocesan hacia atrás al retomar.")
    ] = 60,
    dry_run: Annotated[
        bool,
        typer.Option(
            "--dry-run", help="Calcula y muestra, sin escribir en fuel_windows."
        ),
    ] = False,
    show: Annotated[
        int, typer.Option(help="Muestra las primeras N ventanas calculadas.")
    ] = 0,
    max_rango_sensor: Annotated[
        float | None,
        typer.Option(help="Regla opcional: máx. (max-min) del sensor en la ventana."),
    ] = None,
    porcentaje_angulo_min: Annotated[
        float | None,
        typer.Option(help="Regla opcional: % mínimo de muestras con ángulo válido."),
    ] = None,
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Agrupa gps_records en ventanas de 5 min con sus litros y las guarda (o simula) en fuel_windows."""
    _setup_logging(verbose)
    reglas = QualityRules(
        fcc_inclinacion=0.0,  # el FCC real viene de fuel_config por vehículo
        max_rango_sensor=max_rango_sensor,
        porcentaje_angulo_min=porcentaje_angulo_min,
    )
    now = datetime.now(UTC)
    with get_connection() as conn:
        try:
            d, h = plan_range(
                conn,
                now=now,
                desde=_parse_local(desde, "--desde"),
                hasta=_parse_local(hasta, "--hasta"),
                lag_minutes=lag,
                overlap_minutes=overlap,
                imei=imei,
            )
        except NoStartError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        if d >= h:
            typer.echo(f"Nada que consolidar: {_fmt(d)} >= {_fmt(h)}.")
            return
        summary = consolidate(
            conn,
            d,
            h,
            imei=imei,
            minutes=WINDOW_MINUTES,
            base_rules=reglas,
            dry_run=dry_run,
            keep_sample=show,
        )
        # el bloque 'with' confirma la transacción al salir sin errores; con --dry-run no hay nada que confirmar
    _print_summary(summary, show)
    if dry_run:
        typer.echo("\n(dry-run: no se escribió nada)")


@app.command("estimar-rendimiento")
def estimar_rendimiento_cmd(
    idautobus: Annotated[str, typer.Option(help="Autobús (idautobus) a analizar.")],
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM'. Por defecto, 14 días atrás."
        ),
    ] = None,
    hasta: Annotated[
        str | None, typer.Option(help="Fin local exclusivo. Por defecto, ahora.")
    ] = None,
    max_hueco: Annotated[
        float, typer.Option(help="Minutos máximos entre ventanas para usar el par.")
    ] = 15.0,
    min_litros: Annotated[
        float, typer.Option(help="Consumo acumulado mínimo para dar una estimación.")
    ] = 10.0,
    odometro_a_km: Annotated[
        float,
        typer.Option(help="Factor odometro_total -> km (0.001 si está en metros)."),
    ] = 0.001,
) -> None:
    """Estima el rendimiento (km/L) con ventanas válidas ya consolidadas. Solo lectura."""
    ahora = datetime.now(UTC)
    d = _parse_local(desde, "--desde") or ahora - timedelta(days=14)
    h = _parse_local(hasta, "--hasta") or ahora
    with get_connection() as conn:
        try:
            cfg = cargar_config(conn, idautobus)
        except ConfigError as exc:
            typer.echo(f"ERROR: {exc}", err=True)
            raise typer.Exit(code=2) from exc
        puntos = cargar_puntos_rendimiento(conn, idautobus, d, h, cfg.calibracion)
    est = estimar_rendimiento(
        puntos,
        odometro_a_km=odometro_a_km,
        salto_litros=cfg.umbral_carga_candidata,
        max_hueco_minutos=max_hueco,
        min_litros=min_litros,
    )
    typer.echo(f"Autobús:      {idautobus}")
    typer.echo(f"Rango:        {_fmt(d)} -> {_fmt(h)} ({settings.app_timezone})")
    typer.echo(f"Ventanas:     {est.puntos} válidas con litros calculables")
    typer.echo(
        f"Tramos:       {est.tramos_usados} usados ({est.puntos_usados} ventanas)"
    )
    if est.descartes:
        typer.echo(
            "Descartes:    "
            + "  ".join(f"{k}={v}" for k, v in est.descartes.most_common())
        )
    typer.echo(f"Recorrido:    {est.km:.2f} km (solo tramos usados)")
    typer.echo(f"Consumo:      {est.litros:.2f} L (estimado por regresión)")
    if est.rendimiento_km_l is None:
        typer.echo(
            f"\nSin estimación: se necesitan al menos {min_litros:g} L de consumo "
            "acumulado en tramos limpios (con al menos 10 km cada uno). "
            "Amplía el rango o espera más datos."
        )
        raise typer.Exit(code=1)
    typer.echo(f"Rendimiento:  {est.rendimiento_km_l:.2f} km/L")
    typer.echo(
        "\nPara usarlo (no se aplica automáticamente):\n"
        f"  UPDATE fuel_config SET rendimiento_default = {est.rendimiento_km_l:.2f}\n"
        f"   WHERE idautobus = '{idautobus}';"
    )


def _linea_rendimiento_reciente(
    idautobus: str, est: RendimientoReciente, tz: ZoneInfo
) -> str:
    if est.rendimiento_km_l is None:
        return f"{idautobus:<12} sin estimación ({est.motivo})"
    error = "" if est.error_pct is None else f" ±{est.error_pct:.1f}%"
    desde = est.desde.astimezone(tz).strftime("%Y-%m-%d %H:%M") if est.desde else "-"
    hasta = est.hasta.astimezone(tz).strftime("%Y-%m-%d %H:%M") if est.hasta else "-"
    return (
        f"{idautobus:<12} {est.rendimiento_km_l:6.2f} km/L{error:<9} "
        f"{est.km:7.1f} km  {est.litros:6.1f} L  {est.puntos:>4} ventanas  "
        f"{est.segmentos:>2} tramos  {desde} -> {hasta}"
    )


@app.command("rendimiento-reciente")
def rendimiento_reciente_cmd(
    idautobus: Annotated[
        str | None,
        typer.Option(help="Solo este autobús (por defecto, todos los activos)."),
    ] = None,
    km: Annotated[
        float, typer.Option(help="Ventana: los últimos N km recorridos.")
    ] = 100.0,
    dias: Annotated[
        int, typer.Option(help="Días hacia atrás que se miran para juntar esos km.")
    ] = 30,
    guardar: Annotated[
        bool,
        typer.Option(
            "--guardar",
            help="Guarda el resultado en fuel_rendimiento_reciente (sin esto solo lo muestra).",
        ),
    ] = False,
) -> None:
    """Cómo está rindiendo cada autobús en sus últimos km, con su margen de error."""
    ahora = datetime.now(UTC)
    hubo_error = False
    with get_connection() as conn:
        buses = autobuses_activos(conn, idautobus)
        if not buses:
            typer.echo("No hay autobuses activos en fuel_config.")
            raise typer.Exit(code=1)
        for bus in buses:
            try:
                est = rendimiento_reciente(
                    conn, bus, ahora=ahora, km_ventana=km, dias=dias
                )
            except ConfigError as exc:
                typer.echo(f"{bus:<12} ERROR: {exc}", err=True)
                hubo_error = True
                continue
            typer.echo(_linea_rendimiento_reciente(bus, est, _tz()))
            if est.descartes:
                typer.echo(
                    "             descartes: "
                    + "  ".join(f"{k}={v}" for k, v in est.descartes.most_common())
                )
            if guardar:
                guardar_rendimiento_reciente(conn, bus, est, km)
    if guardar:
        typer.echo(f"\nGuardado en fuel_rendimiento_reciente ({len(buses)} autobuses).")
    raise typer.Exit(code=1 if hubo_error else 0)


def _linea_bloque(b: BloqueRendimiento, tz: ZoneInfo) -> str:
    ini = b.inicio.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    fin = b.fin.astimezone(tz).strftime("%Y-%m-%d %H:%M")
    if b.rendimiento_km_l is None:
        medida = f"sin rendimiento ({b.estado})"
    else:
        error = "" if b.error_pct is None else f" ±{b.error_pct:.1f}%"
        medida = f"{b.rendimiento_km_l:6.2f} km/L{error}  {b.litros_consumidos:5.1f} L"
    cierre = "" if b.completo else f"  [incompleto: {b.cierre}]"
    return f"  {ini} -> {fin}  {b.km:6.1f} km  {b.puntos:>3} ventanas  {medida}{cierre}"


@app.command("historial-rendimiento")
def historial_rendimiento_cmd(
    idautobus: Annotated[
        str | None,
        typer.Option(help="Solo este autobús (por defecto, todos los activos)."),
    ] = None,
    km_bloque: Annotated[
        float, typer.Option(help="Tamaño de cada bloque del historial, en km.")
    ] = 50.0,
    desde: Annotated[
        str | None,
        typer.Option(
            help="Inicio local 'AAAA-MM-DD HH:MM' para rellenar historial hacia atrás. "
            "Solo aplica a un autobús que todavía no tiene bloques guardados."
        ),
    ] = None,
    guardar: Annotated[
        bool,
        typer.Option(
            "--guardar",
            help="Agrega los bloques a fuel_rendimiento_historial (sin esto solo los muestra).",
        ),
    ] = False,
) -> None:
    """Bloques de rendimiento que faltan en el historial (los que ya son definitivos)."""
    ahora = datetime.now(UTC)
    d = _parse_local(desde, "--desde")
    hubo_error = False
    with get_connection() as conn:
        buses = autobuses_activos(conn, idautobus)
        if not buses:
            typer.echo("No hay autobuses activos en fuel_config.")
            raise typer.Exit(code=1)
        for bus in buses:
            try:
                bloques = bloques_rendimiento_nuevos(
                    conn, bus, ahora=ahora, km_bloque=km_bloque, desde=d
                )
            except ConfigError as exc:
                typer.echo(f"{bus:<12} ERROR: {exc}", err=True)
                hubo_error = True
                continue
            km = sum(b.km for b in bloques)
            typer.echo(f"{bus}: {len(bloques)} bloques nuevos ({km:.1f} km)")
            for b in bloques:
                typer.echo(_linea_bloque(b, _tz()))
            if guardar and bloques:
                n = guardar_bloques(conn, bus, bloques, km_bloque)
                typer.echo(f"  guardados: {n}")
    if not guardar:
        typer.echo("\n(simulación: agrega --guardar para escribir en el historial)")
    raise typer.Exit(code=1 if hubo_error else 0)


@app.command("run-cycle")
def run_cycle_cmd(
    log_dir: Annotated[
        str, typer.Option(help="Carpeta del log rotativo (vacío = solo consola).")
    ] = "logs",
    verbose: Annotated[bool, typer.Option("--verbose", "-v")] = False,
) -> None:
    """Un ciclo completo (consolidar + rendimiento). Pensado para el Task Scheduler."""
    from pathlib import Path

    configurar_logging(Path(log_dir) if log_dir else None, verbose)
    with get_connection() as conn:
        resultado = ejecutar_ciclo(conn)
    raise typer.Exit(code=resultado.codigo)
