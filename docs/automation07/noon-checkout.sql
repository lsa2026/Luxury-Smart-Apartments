CREATE OR REPLACE FUNCTION public.lsa07_ingest(p_snapshot jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;s jsonb:=p_snapshot;k uuid;v bigint;f text;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RAISE EXCEPTION 'Scenario 07 disabled';END IF;
 IF coalesce(jsonb_typeof(s) IS DISTINCT FROM 'object' OR s->>'source' IS DISTINCT FROM 'morning07'
  OR s->'payment_verified' IS DISTINCT FROM 'true'::jsonb
  OR s->>'status' NOT IN ('paid','cancelled')
  OR NOT s ?& ARRAY['morning_stay_id','source_revision','state_fingerprint','phone','starts_at','ends_at',
   'arrival_date','departure_date','check_in_time','check_out_time','timezone','source_event_id','delivery_token','listing_map_id','status']
  OR s->>'phone' !~ '^\+?[1-9][0-9]{7,14}$'
  OR s->>'timezone' NOT IN ('Asia/Riyadh','Africa/Casablanca')
  OR s->>'state_fingerprint' !~ '^[0-9a-f]{64}$'
  OR s->>'arrival_date' IS DISTINCT FROM s->>'departure_date'
  OR NOT ((s->>'starts_at')::timestamptz<(s->>'ends_at')::timestamptz)
  OR (s->>'ends_at')::timestamptz-(s->>'starts_at')::timestamptz>=interval '12 hours'
  OR NOT (time '00:00'<(s->>'check_in_time')::time AND (s->>'check_in_time')::time<(s->>'check_out_time')::time
   AND (s->>'check_out_time')::time<=time '12:00'),true) THEN RAISE EXCEPTION 'Invalid verified Morning07 event';END IF;
 k:=(s->>'morning_stay_id')::uuid;v:=(s->>'source_revision')::bigint;f:=s->>'state_fingerprint';
 IF v<1 OR (s->>'listing_map_id')::bigint<1 OR length(s->>'delivery_token')<40 THEN RAISE EXCEPTION 'Invalid Morning07 identity';END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('lsa07:'||k::text,0));
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=k FOR UPDATE;
 IF FOUND THEN
  IF v<r.revision THEN RETURN jsonb_build_object('accepted',true,'ignored',true,'reason','stale_revision');END IF;
  IF v=r.revision THEN
   IF f<>r.fingerprint THEN RAISE EXCEPTION 'Revision payload conflict';END IF;
   RETURN jsonb_build_object('accepted',true,'duplicate',true);
  END IF;
  UPDATE public.lsa07_jobs SET state='superseded',lease_owner=null,lease_until=null,updated_at=now()
   WHERE stay_id=k AND state NOT IN ('delivered','failed','superseded','expired');
  UPDATE public.lsa07_reservations SET revision=v,fingerprint=f,previous_payload=payload,payload=s,
   prepare_owner=null,prepare_until=null,contact_state=CASE WHEN s->>'status'='cancelled' THEN 'complete' ELSE 'pending' END,
   contact_next_at=now(),contact_owner=null,contact_until=null,contact_attempts=0,updated_at=now() WHERE stay_id=k;
 ELSE
  INSERT INTO public.lsa07_reservations(stay_id,revision,fingerprint,payload,contact_state)
   VALUES(k,v,f,s,CASE WHEN s->>'status'='cancelled' THEN 'complete' ELSE 'pending' END);
 END IF;
 RETURN jsonb_build_object('accepted',true,'revision',v);
END $function$;
