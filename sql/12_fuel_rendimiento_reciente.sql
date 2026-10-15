-- 12_fuel_rendimiento_reciente.sql
-- Una fila por autobús con su rendimiento más reciente (la escribe el run-cycle y el comando
-- `rendimiento-reciente --guardar`; la lee el widget del dashboard).
-- Se puede correr más de una vez. No toca ninguna otra tabla.
--
-- Si no hay estimación, el run-cycle conserva el último valor bueno y solo actualiza `motivo`
-- y `calculado_en`: por eso `desde`/`hasta` indican de cuándo es realmente el dato.

CREATE TABLE IF NOT EXISTS public.fuel_rendimiento_reciente (
    idautobus         varchar     PRIMARY KEY,
    calculado_en      timestamptz NOT NULL DEFAULT now(),
    km_ventana        numeric     NOT NULL,            -- ventana pedida (últimos N km)
    rendimiento_km_l  numeric,
    error_pct         numeric,                         -- ± % (un error estándar)
    km                numeric     NOT NULL DEFAULT 0,  -- km que respaldan la estimación
    litros            numeric,
    puntos            integer     NOT NULL DEFAULT 0,  -- ventanas usadas
    segmentos         integer     NOT NULL DEFAULT 0,  -- trozos continuos usados
    desde             timestamptz,
    hasta             timestamptz,
    motivo            text,                            -- NULL si hay estimación
    coordenadas_inicio text,                           -- 'lat,lon' al inicio de la ventana (desde)
    coordenadas_fin    text                            -- 'lat,lon' al final de la ventana (hasta)
);

-- Si la tabla ya existía sin coordenadas (y limpia las 4 columnas de la versión anterior):
ALTER TABLE public.fuel_rendimiento_reciente
    ADD COLUMN IF NOT EXISTS coordenadas_inicio text,
    ADD COLUMN IF NOT EXISTS coordenadas_fin    text,
    DROP COLUMN IF EXISTS latitud_inicio,
    DROP COLUMN IF EXISTS longitud_inicio,
    DROP COLUMN IF EXISTS latitud_fin,
    DROP COLUMN IF EXISTS longitud_fin;

COMMENT ON TABLE public.fuel_rendimiento_reciente IS
    'Rendimiento (km/L) de cada autobús en sus últimos km. Lo calcula elena-fuel.';
