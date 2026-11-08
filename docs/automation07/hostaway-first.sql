-- Morning07 Hostaway-first compatibility. Apply before the website emits contract version 2.
BEGIN;
-- Pure admission predicate shared by scenario 07 RPCs; legacy v1 remains accepted.
CREATE OR REPLACE FUNCTION public.lsa07_booking_admitted(s jsonb)
RETURNS boolean LANGUAGE sql IMMUTABLE SET search_path TO ''
AS $body$
 SELECT coalesce(s->>'source'='morning07' AND
  CASE
   WHEN coalesce(s->'contract_version','1'::jsonb)='1'::jsonb THEN
    s->'payment_verified'='true'::jsonb AND s->>'status' IN ('paid','cancelled')
   WHEN s->'contract_version'='2'::jsonb THEN
    s->'booking_verified'='true'::jsonb
    AND s->>'status' IN ('confirmed','cancelled')
    AND jsonb_typeof(s->'hostaway_reservation_id')='number'
    AND CASE WHEN s->>'hostaway_reservation_id' ~ '^[1-9][0-9]{0,15}$'
      THEN (s->>'hostaway_reservation_id')::numeric<=9007199254740991 ELSE false END
    AND ((s->>'payment_status'='unpaid' AND s->'payment_verified'='false'::jsonb)
      OR (s->>'payment_status'='paid' AND s->'payment_verified'='true'::jsonb))
   ELSE false
  END,false);
$body$;

REVOKE ALL ON FUNCTION public.lsa07_booking_admitted(jsonb) FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.lsa07_booking_admitted(jsonb) TO service_role;


CREATE UNIQUE INDEX IF NOT EXISTS lsa07_v2_hostaway_identity_unique
ON public.lsa07_reservations ((payload->>'hostaway_reservation_id'))
WHERE payload->'contract_version'='2'::jsonb;

CREATE OR REPLACE FUNCTION public.lsa07_contact_claim(p_worker text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_contact',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE contact_state<>'complete' AND contact_next_at<=now()
  AND (contact_until IS NULL OR contact_until<=now()) AND payload->>'status' IN ('paid','confirmed') AND public.lsa07_booking_admitted(payload)
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_contact',false);END IF;
 UPDATE public.lsa07_reservations SET contact_owner=p_worker,contact_until=now()+interval '5 minutes',contact_attempts=contact_attempts+1 WHERE stay_id=r.stay_id;
 RETURN r.payload||jsonb_build_object('has_contact',true,'previous_phones',jsonb_build_array(r.previous_payload->>'phone'));
END $function$;



CREATE OR REPLACE FUNCTION public.lsa07_event_contact_claim(p_worker text, p_stay uuid, p_revision bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_contact',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=p_stay AND revision=p_revision AND contact_attempts<3 AND (payload->>'ends_at')::timestamptz>now() AND contact_state<>'complete' AND contact_next_at<=now()
  AND (contact_until IS NULL OR contact_until<=now()) AND payload->>'status' IN ('paid','confirmed') AND public.lsa07_booking_admitted(payload)
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_contact',false);END IF;
 UPDATE public.lsa07_reservations SET contact_owner=p_worker,contact_until=now()+interval '5 minutes',contact_attempts=contact_attempts+1 WHERE stay_id=r.stay_id;
 RETURN r.payload||jsonb_build_object('has_contact',true,'previous_phones',jsonb_build_array(r.previous_payload->>'phone'));
END $function$;



CREATE OR REPLACE FUNCTION public.lsa07_event_next(p_worker text, p_stay uuid, p_revision bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE j public.lsa07_jobs%rowtype;s jsonb;target text;soon timestamptz;contact_soon timestamptz;prepare_soon timestamptz;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('action','done');END IF;
 UPDATE public.lsa07_jobs SET state='expired',updated_at=now() WHERE stay_id=p_stay AND revision=p_revision AND eligible_until<=now() AND state IN ('pending','posting','check');
 UPDATE public.lsa07_jobs SET state='failed',last_error='receipt_deadline_exceeded',lease_owner=null,lease_until=null,updated_at=now()
  WHERE stay_id=p_stay AND revision=p_revision AND state IN ('posting','check') AND deadline<=now() AND (lease_until IS NULL OR lease_until<=now());
 -- One durable attention notice per failed guest revision, not one per failed message.
 INSERT INTO public.lsa07_jobs(job_key,stay_id,revision,kind,recipient_kind,recipient,body,sequence,due_at,reference_id)
 SELECT DISTINCT 'lsa07:'||j2.stay_id||':'||j2.revision||':attention',j2.stay_id,j2.revision,'attention','group',st.work_group_id,
  'تعذر تأكيد وصول رسائل الحجز الصباحي. المرجع AM-'||left(j2.stay_id::text,8)||E'\nمتابعة الإدارة: '||(r.payload->>'management_url'),
  0,now(),'lsa07-'||md5('lsa07:'||j2.stay_id||':'||j2.revision||':attention')
 FROM public.lsa07_jobs j2 JOIN public.lsa07_reservations r ON r.stay_id=j2.stay_id AND r.revision=j2.revision
 CROSS JOIN public.lsa07_settings st WHERE j2.stay_id=p_stay AND j2.revision=p_revision AND j2.state='failed' AND j2.recipient_kind='guest' AND st.id
 ON CONFLICT(job_key) DO NOTHING;
 SELECT x.* INTO j FROM public.lsa07_jobs x JOIN public.lsa07_reservations r ON r.stay_id=x.stay_id AND r.revision=x.revision
  WHERE x.stay_id=p_stay AND x.revision=p_revision AND x.state IN ('pending','posting','check') AND x.due_at<=now() AND x.next_at<=now()
   AND (x.lease_until IS NULL OR x.lease_until<=now())
   AND NOT EXISTS(SELECT 1 FROM public.lsa07_jobs earlier WHERE earlier.stay_id=x.stay_id AND earlier.revision=x.revision
    AND earlier.recipient_kind=x.recipient_kind AND earlier.sequence<x.sequence AND earlier.state IN ('pending','posting','check'))
  ORDER BY x.due_at,x.sequence,x.created_at FOR UPDATE OF x SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN
  SELECT min(greatest(x.next_at,x.due_at,coalesce(x.lease_until,x.next_at))) INTO soon
   FROM public.lsa07_jobs x JOIN public.lsa07_reservations r ON r.stay_id=x.stay_id AND r.revision=x.revision
   WHERE x.stay_id=p_stay AND x.revision=p_revision AND x.state IN ('pending','posting','check');
  SELECT greatest(contact_next_at,coalesce(contact_until,contact_next_at)) INTO contact_soon
   FROM public.lsa07_reservations WHERE stay_id=p_stay AND revision=p_revision AND contact_state<>'complete'
   AND contact_attempts<3 AND payload->>'status' IN ('paid','confirmed') AND public.lsa07_booking_admitted(payload) AND (payload->>'ends_at')::timestamptz>now();
  SELECT greatest(now(),coalesce(prepare_until,now())) INTO prepare_soon
   FROM public.lsa07_reservations WHERE stay_id=p_stay AND revision=p_revision AND prepared_revision<revision;
  soon:=least(soon,contact_soon,prepare_soon);
  IF soon IS NOT NULL THEN RETURN jsonb_build_object('action','wait','wake_at',greatest(soon,now()+interval '15 seconds'));END IF;
  RETURN jsonb_build_object('action','done');END IF;
 SELECT payload INTO s FROM public.lsa07_reservations WHERE stay_id=j.stay_id;
 UPDATE public.lsa07_jobs SET state=CASE WHEN state='pending' THEN 'posting' ELSE 'check' END,
  attempts=attempts+CASE WHEN state='pending' THEN 1 ELSE 0 END,
  deadline=coalesce(deadline,now()+interval '5 minutes'),lease_owner=p_worker,lease_until=now()+interval '90 seconds',updated_at=now()
  WHERE job_key=j.job_key;
 RETURN s||jsonb_build_object('action',CASE WHEN j.state='pending' THEN 'send' ELSE 'check' END,
  'job_key',j.job_key,'message_type',j.kind,'recipient_kind',j.recipient_kind,'recipient_phone',j.recipient,
  'message_body',j.body,'reference_id',j.reference_id,'provider_message_id',j.provider_id);
END $function$;



CREATE OR REPLACE FUNCTION public.lsa07_ingest(p_snapshot jsonb)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;s jsonb:=p_snapshot;k uuid;v bigint;f text;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RAISE EXCEPTION 'Scenario 07 disabled';END IF;
 IF coalesce(jsonb_typeof(s) IS DISTINCT FROM 'object' OR s->>'source' IS DISTINCT FROM 'morning07'
  OR NOT public.lsa07_booking_admitted(s)
  OR NOT s ?& ARRAY['morning_stay_id','source_revision','state_fingerprint','phone','starts_at','ends_at',
   'arrival_date','departure_date','check_in_time','check_out_time','timezone','source_event_id','delivery_token','listing_map_id','status']
  OR s->>'phone' !~ '^\+?[1-9][0-9]{7,14}$'
  OR s->>'timezone' NOT IN ('Asia/Riyadh','Africa/Casablanca')
  OR s->>'state_fingerprint' !~ '^[0-9a-f]{64}$'
  OR s->>'arrival_date' IS DISTINCT FROM s->>'departure_date'
  OR NOT ((s->>'starts_at')::timestamptz<(s->>'ends_at')::timestamptz)
  OR (s->>'ends_at')::timestamptz-(s->>'starts_at')::timestamptz>=interval '12 hours'
  OR to_char((s->>'starts_at')::timestamptz AT TIME ZONE (s->>'timezone'),'YYYY-MM-DD') IS DISTINCT FROM s->>'arrival_date'
  OR to_char((s->>'ends_at')::timestamptz AT TIME ZONE (s->>'timezone'),'YYYY-MM-DD') IS DISTINCT FROM s->>'departure_date'
  OR to_char((s->>'starts_at')::timestamptz AT TIME ZONE (s->>'timezone'),'HH24:MI') IS DISTINCT FROM s->>'check_in_time'
  OR to_char((s->>'ends_at')::timestamptz AT TIME ZONE (s->>'timezone'),'HH24:MI') IS DISTINCT FROM s->>'check_out_time'
  OR NOT (time '00:00'<(s->>'check_in_time')::time AND (s->>'check_in_time')::time<(s->>'check_out_time')::time
   AND (s->>'check_out_time')::time<=time '12:00'),true) THEN RAISE EXCEPTION 'Invalid verified Morning07 event';END IF;
 k:=(s->>'morning_stay_id')::uuid;v:=(s->>'source_revision')::bigint;f:=s->>'state_fingerprint';
 IF v<1 OR (s->>'listing_map_id')::bigint<1 OR length(s->>'delivery_token')<40 THEN RAISE EXCEPTION 'Invalid Morning07 identity';END IF;
 PERFORM pg_advisory_xact_lock(hashtextextended('lsa07:'||k::text,0));
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=k FOR UPDATE;
 IF FOUND THEN
  IF v<r.revision THEN RETURN jsonb_build_object('accepted',true,'ignored',true,'reason','stale_revision');END IF;
  IF r.payload->'contract_version'='2'::jsonb AND (s->'contract_version' IS DISTINCT FROM '2'::jsonb OR s->'hostaway_reservation_id' IS DISTINCT FROM r.payload->'hostaway_reservation_id') THEN RAISE EXCEPTION 'Morning07 linked Hostaway identity changed';END IF;
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


COMMIT;
