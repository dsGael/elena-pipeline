-- 15_litros_estimados.sql
-- Nivel estimado "en vivo": último valor confiable menos lo que se debió consumir desde entonces
-- (km / rendimiento). Lo escribe el procesador en cada ventana; no interviene en decisiones.
-- Se puede correr más de una vez. Correr ANTES de actualizar el código en el servidor: el
-- pipeline nuevo inserta esta columna. Las filas existentes quedan en NULL hasta reprocesar.

ALTER TABLE public.fuel_processed_result
    ADD COLUMN IF NOT EXISTS litros_estimados numeric(8, 2);

COMMENT ON COLUMN public.fuel_processed_result.litros_estimados IS
    'Nivel estimado tras la ventana: litros_finales - km desde el último confiable / rendimiento. Solo informativo.';
