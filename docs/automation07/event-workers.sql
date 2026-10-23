-- Event-driven scenario 07 workers: no global scan or periodic trigger.
CREATE OR REPLACE FUNCTION public.lsa07_event_claim_booking(p_worker text, p_stay uuid, p_revision bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;a jsonb;t jsonb;g text;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_work',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=p_stay AND revision=p_revision AND prepared_revision<revision
  AND (prepare_until IS NULL OR prepare_until<=now()) ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_work',false);END IF;
 SELECT to_jsonb(ai) INTO a FROM public.lsa07_apartments_info ai WHERE listing_map_id=(r.payload->>'listing_map_id')::bigint;
 IF a IS NULL THEN RAISE EXCEPTION 'Scenario 07 apartment mapping missing';END IF;
 SELECT coalesce(jsonb_agg(to_jsonb(mt)),'[]') INTO t FROM public.lsa07_message_templates mt;
 SELECT work_group_id INTO g FROM public.lsa07_settings WHERE id;
 UPDATE public.lsa07_reservations SET prepare_owner=p_worker,prepare_until=now()+interval '90 seconds' WHERE stay_id=r.stay_id;
 RETURN jsonb_build_object('has_work',true,'booking',r.payload,'previous',r.previous_payload,'apartment',a,'templates',t,'work_group_id',g,'now',now());
END $function$;
REVOKE ALL ON FUNCTION public.lsa07_event_claim_booking(text,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.lsa07_event_claim_booking(text,uuid,bigint) TO service_role;

CREATE OR REPLACE FUNCTION public.lsa07_event_contact_claim(p_worker text, p_stay uuid, p_revision bigint)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
DECLARE r public.lsa07_reservations%rowtype;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_contact',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=p_stay AND revision=p_revision AND contact_attempts<3 AND (payload->>'ends_at')::timestamptz>now() AND contact_state<>'complete' AND contact_next_at<=now()
  AND (contact_until IS NULL OR contact_until<=now()) AND payload->>'status'='paid'
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_contact',false);END IF;
 UPDATE public.lsa07_reservations SET contact_owner=p_worker,contact_until=now()+interval '5 minutes',contact_attempts=contact_attempts+1 WHERE stay_id=r.stay_id;
 RETURN r.payload||jsonb_build_object('has_contact',true,'previous_phones',jsonb_build_array(r.previous_payload->>'phone'));
END $function$;
REVOKE ALL ON FUNCTION public.lsa07_event_contact_claim(text,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.lsa07_event_contact_claim(text,uuid,bigint) TO service_role;

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
   AND contact_attempts<3 AND payload->>'status'='paid' AND (payload->>'ends_at')::timestamptz>now();
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
REVOKE ALL ON FUNCTION public.lsa07_event_next(text,uuid,bigint) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.lsa07_event_next(text,uuid,bigint) TO service_role;

