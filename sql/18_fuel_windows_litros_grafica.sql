-- 18_fuel_windows_litros_grafica.sql
-- Litros para la gráfica. Igual a `litros`, salvo en las ventanas con el ángulo fuera de rango
-- (|mediana(inclinación) - FCC| > angulo_max): ahí el sensor no es confiable y se estima con
-- la última ventana válida menos los km recorridos desde ella / rendimiento del autobús
-- (fuel_rendimiento_reciente, si no hay fuel_config.rendimiento_default).
-- También agrega ignicion_dominante (informativo para la gráfica).
-- Se puede correr más de una vez. Correr ANTES de actualizar el código en el servidor.
-- Las ventanas existentes quedan en NULL hasta re-consolidarlas (elena-fuel consolidate --desde ...).

ALTER TABLE public.fuel_windows
    ADD COLUMN IF NOT EXISTS ignicion_dominante boolean,
    ADD COLUMN IF NOT EXISTS litros_grafica     numeric(8, 2);

COMMENT ON COLUMN public.fuel_windows.ignicion_dominante IS
    'Encendida si al menos la mitad de las muestras con dato de ignición lo están (informativo).';
COMMENT ON COLUMN public.fuel_windows.litros_grafica IS
    'litros, o estimado por odómetro y rendimiento si el ángulo está fuera de rango. NULL = sin ancla/rendimiento/odómetro.';
