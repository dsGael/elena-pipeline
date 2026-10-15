-- 13_fuel_rendimiento_historial.sql
-- Historial de rendimiento por autobús: bloques de ~km_bloque km, sin traslape, inmutables.
-- Lo llena el run-cycle y `elena-fuel historial-rendimiento --guardar`. Se puede correr más de una vez.
-- Para reportes: rendimiento de un periodo = SUM(km_recorridos) / SUM(litros_consumidos).
-- (NO promedies la columna rendimiento_km_l: pesa igual un bloque corto que uno largo.)

CREATE TABLE IF NOT EXISTS public.fuel_rendimiento_historial (
    id                  bigserial   PRIMARY KEY,
    idautobus           varchar     NOT NULL,
    fecha_inicio        timestamptz NOT NULL,
    fecha_fin           timestamptz NOT NULL,
    odometro_inicio_km  numeric     NOT NULL,
    odometro_fin_km     numeric     NOT NULL,
    km_recorridos       numeric     NOT NULL,
    litros_inicio       numeric,
    litros_fin          numeric,
    litros_consumidos   numeric,
    rendimiento_km_l    numeric,
    error_pct           numeric,           -- ± % (un error estándar); NULL si no hay estimación
    puntos              integer     NOT NULL DEFAULT 0,
    cierre              text,              -- km | carga_o_extraccion | odometro | hueco_largo
    completo            boolean     NOT NULL DEFAULT true,   -- false = bloque parcial (cortado por un evento)
    estado              text        NOT NULL,                -- ok | sin_consumo_visible | pocos_puntos | sin_recorrido
    km_bloque           numeric     NOT NULL,                -- tamaño de bloque con que se generó
    coordenadas_inicio  text,              -- 'lat,lon' al inicio del bloque (NULL si no hubo muestra GPS)
    coordenadas_fin     text,              -- 'lat,lon' al final del bloque
    creado_en           timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT fuel_rend_hist_uq UNIQUE (idautobus, fecha_inicio),
    CONSTRAINT fuel_rend_hist_orden_chk CHECK (fecha_fin >= fecha_inicio)
);

-- Si la tabla ya existía sin coordenadas (y limpia las 4 columnas de la versión anterior):
ALTER TABLE public.fuel_rendimiento_historial
    ADD COLUMN IF NOT EXISTS coordenadas_inicio text,
    ADD COLUMN IF NOT EXISTS coordenadas_fin    text,
    DROP COLUMN IF EXISTS latitud_inicio,
    DROP COLUMN IF EXISTS longitud_inicio,
    DROP COLUMN IF EXISTS latitud_fin,
    DROP COLUMN IF EXISTS longitud_fin;

CREATE INDEX IF NOT EXISTS fuel_rend_hist_bus_fin_idx
    ON public.fuel_rendimiento_historial (idautobus, fecha_fin DESC);

COMMENT ON TABLE public.fuel_rendimiento_historial IS
    'Historial de rendimiento (km/L) por autobús en bloques sin traslape. Lo calcula elena-fuel.';

-- Resumen diario (hora de Hermosillo), ponderado por distancia: base para reportes de manejo.
CREATE OR REPLACE VIEW public.fuel_rendimiento_diario AS
SELECT idautobus,
       (fecha_fin AT TIME ZONE 'America/Hermosillo')::date           AS dia,
       count(*)                                                      AS bloques,
       sum(km_recorridos)                                            AS km,
       sum(litros_consumidos)                                        AS litros,
       sum(km_recorridos) / NULLIF(sum(litros_consumidos), 0)        AS rendimiento_km_l
FROM public.fuel_rendimiento_historial
WHERE estado = 'ok' AND litros_consumidos > 0
GROUP BY idautobus, (fecha_fin AT TIME ZONE 'America/Hermosillo')::date;

-- Rellena las coordenadas de bloques guardados antes de existir esta columna (idempotente).
WITH calc AS (
    SELECT h.id, p_ini.coord AS c_ini, p_fin.coord AS c_fin
    FROM public.fuel_rendimiento_historial h
    LEFT JOIN LATERAL (
        SELECT concat(round(g.latitude::numeric, 5), ',', round(g.longitude::numeric, 5)) AS coord
        FROM public.gps_records g
        JOIN public.dispositivos d ON d.imei = g.imei
        WHERE d.idautobus = h.idautobus
          AND g.recorded_at >= h.fecha_inicio AND g.recorded_at < h.fecha_inicio + interval '5 minutes'
          AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL
        ORDER BY g.recorded_at ASC LIMIT 1) p_ini ON true
    LEFT JOIN LATERAL (
        SELECT concat(round(g.latitude::numeric, 5), ',', round(g.longitude::numeric, 5)) AS coord
        FROM public.gps_records g
        JOIN public.dispositivos d ON d.imei = g.imei
        WHERE d.idautobus = h.idautobus
          AND g.recorded_at >= h.fecha_fin AND g.recorded_at < h.fecha_fin + interval '5 minutes'
          AND g.latitude IS NOT NULL AND g.longitude IS NOT NULL
        ORDER BY g.recorded_at DESC LIMIT 1) p_fin ON true
    WHERE h.coordenadas_inicio IS NULL OR h.coordenadas_fin IS NULL
)
UPDATE public.fuel_rendimiento_historial h
SET coordenadas_inicio = COALESCE(h.coordenadas_inicio, calc.c_ini),
    coordenadas_fin    = COALESCE(h.coordenadas_fin, calc.c_fin)
FROM calc
WHERE h.id = calc.id;
