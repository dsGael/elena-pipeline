-- 20_fuel_events_trigger.sql
-- Maniobras (aceleración, frenado, giro brusco) en fuel_events al instante: en el mismo INSERT de
-- gps_records. El ralentí y las cargas siguen saliendo del ciclo (elena-fuel run-cycle), que
-- además recoge las maniobras que este trigger no haya podido guardar.
-- Se puede correr más de una vez. Requiere sql/19_fuel_eventos.sql.

CREATE OR REPLACE FUNCTION public.fuel_events_maniobra() RETURNS trigger
LANGUAGE plpgsql AS $$
BEGIN
    INSERT INTO public.fuel_events (idautobus, fecha, evento, ubicacion)
    SELECT d.idautobus,
           NEW.recorded_at,
           (ARRAY['aceleracion', 'frenado', 'giro_brusco'])[NEW.maniobra],
           CASE WHEN NEW.latitude IS NOT NULL AND NEW.longitude IS NOT NULL
                THEN concat(round(NEW.latitude::numeric, 5), ',', round(NEW.longitude::numeric, 5))
           END
    FROM public.dispositivos d
    JOIN public.fuel_config c ON c.idautobus = d.idautobus AND c.activo
    WHERE d.imei = NEW.imei
    ON CONFLICT (idautobus, fecha, evento) DO NOTHING;
    RETURN NULL;
EXCEPTION WHEN OTHERS THEN
    -- Nunca tumbar la ingesta: la maniobra la guarda el siguiente ciclo.
    RAISE WARNING 'fuel_events_maniobra (imei %, %): %', NEW.imei, NEW.recorded_at, SQLERRM;
    RETURN NULL;
END
$$;

DROP TRIGGER IF EXISTS fuel_events_maniobra ON public.gps_records;
CREATE TRIGGER fuel_events_maniobra
    AFTER INSERT ON public.gps_records
    FOR EACH ROW
    WHEN (NEW.maniobra IN (1, 2, 3))
    EXECUTE FUNCTION public.fuel_events_maniobra();

COMMENT ON FUNCTION public.fuel_events_maniobra() IS
    'Guarda en fuel_events la maniobra (AVL 253) de cada registro nuevo de gps_records.';
