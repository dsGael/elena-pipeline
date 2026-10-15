-- 12b_revertir_001_opcional.sql
-- SOLO si alcanzaste a correr 001_fuel_rendimiento.sql (la primera propuesta). El diseño
-- final ya no usa esas columnas, tabla ni vista. Todo es IF EXISTS: no falla si no existen.
-- Si no corriste el 001, ignora este archivo.

DROP VIEW  IF EXISTS public.fuel_rendimiento_actual;
DROP TABLE IF EXISTS public.fuel_rendimiento_tramos;

ALTER TABLE public.fuel_config
    DROP CONSTRAINT IF EXISTS fuel_config_rend_metodo_chk,
    DROP COLUMN IF EXISTS rend_activo,
    DROP COLUMN IF EXISTS rend_km_min_tramo,
    DROP COLUMN IF EXISTS rend_ventana_suavizado_s,
    DROP COLUMN IF EXISTS rend_umbral_recarga_l,
    DROP COLUMN IF EXISTS rend_recarga_paso_min_l,
    DROP COLUMN IF EXISTS rend_hueco_max_s,
    DROP COLUMN IF EXISTS rend_min_kml,
    DROP COLUMN IF EXISTS rend_max_kml,
    DROP COLUMN IF EXISTS rend_metodo,
    DROP COLUMN IF EXISTS rend_n_tramos_actual;
