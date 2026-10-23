-- Scenario 07 owns every mutable object below. No legacy delivery engine is called.
CREATE TABLE public.lsa07_settings (
 id boolean PRIMARY KEY DEFAULT true CHECK(id), enabled boolean NOT NULL DEFAULT false,
 work_group_id text NOT NULL DEFAULT '120363408128237629@g.us'
);
INSERT INTO public.lsa07_settings(id) VALUES(true);
CREATE TABLE public.lsa07_apartments_info (LIKE public.apartments_info INCLUDING CONSTRAINTS INCLUDING INDEXES);
INSERT INTO public.lsa07_apartments_info SELECT * FROM public.apartments_info;
CREATE TABLE public.lsa07_message_templates (
 template_name text PRIMARY KEY,content_ar text NOT NULL,content_en text NOT NULL,content_fr text
);
INSERT INTO public.lsa07_message_templates SELECT template_name,content_ar,content_en,content_fr
 FROM public.message_templates WHERE template_name IN ('house_rules','checkin_guide');
CREATE TABLE public.lsa07_reservations (
 stay_id uuid PRIMARY KEY,revision bigint NOT NULL CHECK(revision>0),fingerprint text NOT NULL,
 payload jsonb NOT NULL,previous_payload jsonb NOT NULL DEFAULT '{}',prepared_revision bigint NOT NULL DEFAULT 0,
 prepare_owner text,prepare_until timestamptz,
 contact_state text NOT NULL DEFAULT 'pending',contact_attempts integer NOT NULL DEFAULT 0,
 contact_next_at timestamptz NOT NULL DEFAULT now(),contact_owner text,contact_until timestamptz,
 created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE TABLE public.lsa07_jobs (
 job_key text PRIMARY KEY,stay_id uuid NOT NULL REFERENCES public.lsa07_reservations(stay_id),
 revision bigint NOT NULL,kind text NOT NULL CHECK(kind IN
 ('status_new','status_modified','status_cancelled','house_rules','checkin_guide','reminder_checkout','group_notice','attention')),
 recipient_kind text NOT NULL CHECK(recipient_kind IN ('guest','group')),recipient text NOT NULL,
 body text NOT NULL CHECK(length(body)>0),sequence integer NOT NULL,
 due_at timestamptz NOT NULL,eligible_until timestamptz,
 state text NOT NULL DEFAULT 'pending' CHECK(state IN ('pending','posting','check','delivered','failed','superseded','expired')),
 reference_id text NOT NULL UNIQUE,provider_id text,attempts integer NOT NULL DEFAULT 0,
 deadline timestamptz,next_at timestamptz NOT NULL DEFAULT now(),lease_owner text,lease_until timestamptz,
 last_error text,created_at timestamptz NOT NULL DEFAULT now(),updated_at timestamptz NOT NULL DEFAULT now()
);
CREATE INDEX lsa07_jobs_due ON public.lsa07_jobs(state,next_at,due_at);
CREATE INDEX lsa07_jobs_stay ON public.lsa07_jobs(stay_id,revision,sequence);

CREATE FUNCTION public.lsa07_ingest(p_snapshot jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
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
   AND (s->>'check_out_time')::time<time '12:00'),true) THEN RAISE EXCEPTION 'Invalid verified Morning07 event';END IF;
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
END $$;

CREATE FUNCTION public.lsa07_claim_booking(p_worker text) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.lsa07_reservations%rowtype;a jsonb;t jsonb;g text;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_work',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE prepared_revision<revision
  AND (prepare_until IS NULL OR prepare_until<=now()) ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_work',false);END IF;
 SELECT to_jsonb(ai) INTO a FROM public.lsa07_apartments_info ai WHERE listing_map_id=(r.payload->>'listing_map_id')::bigint;
 IF a IS NULL THEN RAISE EXCEPTION 'Scenario 07 apartment mapping missing';END IF;
 SELECT coalesce(jsonb_agg(to_jsonb(mt)),'[]') INTO t FROM public.lsa07_message_templates mt;
 SELECT work_group_id INTO g FROM public.lsa07_settings WHERE id;
 UPDATE public.lsa07_reservations SET prepare_owner=p_worker,prepare_until=now()+interval '90 seconds' WHERE stay_id=r.stay_id;
 RETURN jsonb_build_object('has_work',true,'booking',r.payload,'previous',r.previous_payload,'apartment',a,'templates',t,'work_group_id',g,'now',now());
END $$;

CREATE FUNCTION public.lsa07_prepare(p_stay uuid,p_revision bigint,p_worker text,p_jobs jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.lsa07_reservations%rowtype;m jsonb;recipient_value text;
BEGIN
 SELECT * INTO r FROM public.lsa07_reservations WHERE stay_id=p_stay FOR UPDATE;
 IF NOT FOUND OR r.revision<>p_revision OR r.prepare_owner IS DISTINCT FROM p_worker OR r.prepare_until<now() THEN
  RETURN jsonb_build_object('prepared',false,'reason','lease_or_revision_changed');END IF;
 IF jsonb_typeof(p_jobs)<>'array' OR jsonb_array_length(p_jobs)>6 THEN RAISE EXCEPTION 'Invalid Morning07 batch';END IF;
 FOR m IN SELECT value FROM jsonb_array_elements(p_jobs) LOOP
  recipient_value:=CASE WHEN m->>'recipient_kind'='group' THEN (SELECT work_group_id FROM public.lsa07_settings WHERE id)
    ELSE regexp_replace(r.payload->>'phone','\D','','g') END;
  IF m->>'job_key' NOT LIKE 'lsa07:'||p_stay::text||':%' OR length(coalesce(m->>'body','')) NOT BETWEEN 1 AND 4096
   OR m->>'recipient_kind' NOT IN ('group','guest') OR length(recipient_value)=0 THEN RAISE EXCEPTION 'Invalid Morning07 job';END IF;
  IF r.payload->>'status'='cancelled' AND m->>'kind' NOT IN ('status_cancelled','group_notice') THEN RAISE EXCEPTION 'Cancelled booking job';END IF;
  IF m->>'kind'='reminder_checkout' AND ((m->>'due_at')::timestamptz<>(r.payload->>'ends_at')::timestamptz-interval '60 minutes'
    OR (m->>'due_at')::timestamptz<=now()) THEN RAISE EXCEPTION 'Invalid checkout reminder time';END IF;
  INSERT INTO public.lsa07_jobs(job_key,stay_id,revision,kind,recipient_kind,recipient,body,sequence,due_at,eligible_until,reference_id)
   VALUES(m->>'job_key',p_stay,p_revision,m->>'kind',m->>'recipient_kind',recipient_value,m->>'body',
   (m->>'sequence')::integer,(m->>'due_at')::timestamptz,nullif(m->>'eligible_until','')::timestamptz,
   'lsa07-'||md5(m->>'job_key')) ON CONFLICT(job_key) DO UPDATE SET
   revision=excluded.revision,body=excluded.body,recipient=excluded.recipient,due_at=excluded.due_at,
   eligible_until=excluded.eligible_until,state=CASE WHEN public.lsa07_jobs.attempts>0 THEN 'check' ELSE 'pending' END,
   lease_owner=null,lease_until=null,next_at=now(),updated_at=now()
   WHERE public.lsa07_jobs.state<>'delivered';
 END LOOP;
 UPDATE public.lsa07_reservations SET prepared_revision=p_revision,prepare_owner=null,prepare_until=null WHERE stay_id=p_stay;
 RETURN jsonb_build_object('prepared',true);
END $$;

CREATE FUNCTION public.lsa07_next(p_worker text) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE j public.lsa07_jobs%rowtype;s jsonb;target text;soon timestamptz;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('action','done');END IF;
 UPDATE public.lsa07_jobs SET state='expired',updated_at=now() WHERE eligible_until<=now() AND state IN ('pending','posting','check');
 UPDATE public.lsa07_jobs SET state='failed',last_error='receipt_deadline_exceeded',lease_owner=null,lease_until=null,updated_at=now()
  WHERE state IN ('posting','check') AND deadline<=now() AND (lease_until IS NULL OR lease_until<=now());
 -- One durable attention notice per failed guest revision, not one per failed message.
 INSERT INTO public.lsa07_jobs(job_key,stay_id,revision,kind,recipient_kind,recipient,body,sequence,due_at,reference_id)
 SELECT DISTINCT 'lsa07:'||j2.stay_id||':'||j2.revision||':attention',j2.stay_id,j2.revision,'attention','group',st.work_group_id,
  'تعذر تأكيد وصول رسائل الحجز الصباحي. المرجع AM-'||left(j2.stay_id::text,8)||E'\nمتابعة الإدارة: '||(r.payload->>'management_url'),
  0,now(),'lsa07-'||md5('lsa07:'||j2.stay_id||':'||j2.revision||':attention')
 FROM public.lsa07_jobs j2 JOIN public.lsa07_reservations r ON r.stay_id=j2.stay_id AND r.revision=j2.revision
 CROSS JOIN public.lsa07_settings st WHERE j2.state='failed' AND j2.recipient_kind='guest' AND st.id
 ON CONFLICT(job_key) DO NOTHING;
 SELECT x.* INTO j FROM public.lsa07_jobs x JOIN public.lsa07_reservations r ON r.stay_id=x.stay_id AND r.revision=x.revision
  WHERE x.state IN ('pending','posting','check') AND x.due_at<=now() AND x.next_at<=now()
   AND (x.lease_until IS NULL OR x.lease_until<=now())
   AND NOT EXISTS(SELECT 1 FROM public.lsa07_jobs earlier WHERE earlier.stay_id=x.stay_id AND earlier.revision=x.revision
    AND earlier.recipient_kind=x.recipient_kind AND earlier.sequence<x.sequence AND earlier.state IN ('pending','posting','check'))
  ORDER BY x.due_at,x.sequence,x.created_at FOR UPDATE OF x SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN
  SELECT min(greatest(x.next_at,x.due_at,coalesce(x.lease_until,x.next_at))) INTO soon
   FROM public.lsa07_jobs x JOIN public.lsa07_reservations r ON r.stay_id=x.stay_id AND r.revision=x.revision
   WHERE x.state IN ('pending','posting','check');
  IF soon<now()+interval '30 seconds' THEN RETURN jsonb_build_object('action','wait','wait_seconds',15);END IF;
  RETURN jsonb_build_object('action','done');END IF;
 SELECT payload INTO s FROM public.lsa07_reservations WHERE stay_id=j.stay_id;
 UPDATE public.lsa07_jobs SET state=CASE WHEN state='pending' THEN 'posting' ELSE 'check' END,
  attempts=attempts+CASE WHEN state='pending' THEN 1 ELSE 0 END,
  deadline=coalesce(deadline,now()+interval '5 minutes'),lease_owner=p_worker,lease_until=now()+interval '90 seconds',updated_at=now()
  WHERE job_key=j.job_key;
 RETURN s||jsonb_build_object('action',CASE WHEN j.state='pending' THEN 'send' ELSE 'check' END,
  'job_key',j.job_key,'message_type',j.kind,'recipient_kind',j.recipient_kind,'recipient_phone',j.recipient,
  'message_body',j.body,'reference_id',j.reference_id,'provider_message_id',j.provider_id);
END $$;

CREATE FUNCTION public.lsa07_result(p_key text,p_worker text,p_result jsonb) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE j public.lsa07_jobs%rowtype;delivered boolean:=coalesce((p_result->>'delivered')::boolean,false);
BEGIN
 SELECT * INTO j FROM public.lsa07_jobs WHERE job_key=p_key FOR UPDATE;
 IF NOT FOUND OR j.lease_owner IS DISTINCT FROM p_worker OR j.state NOT IN ('posting','check') THEN
  RETURN jsonb_build_object('recorded',false,'reason','stale_lease');END IF;
 UPDATE public.lsa07_jobs SET state=CASE WHEN delivered THEN 'delivered' ELSE 'check' END,
  provider_id=coalesce(nullif(p_result->>'provider_id',''),provider_id),next_at=now()+interval '15 seconds',
  last_error=left(p_result->>'error',80),lease_owner=null,lease_until=null,updated_at=now() WHERE job_key=p_key;
 RETURN jsonb_build_object('recorded',true,'delivered',delivered);
END $$;

CREATE FUNCTION public.lsa07_contact_claim(p_worker text) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
DECLARE r public.lsa07_reservations%rowtype;
BEGIN
 IF NOT coalesce((SELECT enabled FROM public.lsa07_settings WHERE id),false) THEN RETURN jsonb_build_object('has_contact',false);END IF;
 SELECT * INTO r FROM public.lsa07_reservations WHERE contact_state<>'complete' AND contact_next_at<=now()
  AND (contact_until IS NULL OR contact_until<=now()) AND payload->>'status'='paid'
  ORDER BY updated_at FOR UPDATE SKIP LOCKED LIMIT 1;
 IF NOT FOUND THEN RETURN jsonb_build_object('has_contact',false);END IF;
 UPDATE public.lsa07_reservations SET contact_owner=p_worker,contact_until=now()+interval '5 minutes',contact_attempts=contact_attempts+1 WHERE stay_id=r.stay_id;
 RETURN r.payload||jsonb_build_object('has_contact',true,'previous_phones',jsonb_build_array(r.previous_payload->>'phone'));
END $$;

CREATE FUNCTION public.lsa07_contact_result(p_stay uuid,p_revision bigint,p_worker text,p_ok boolean) RETURNS jsonb
LANGUAGE plpgsql SECURITY INVOKER SET search_path='' AS $$
BEGIN
 UPDATE public.lsa07_reservations SET contact_state=CASE WHEN p_ok THEN 'complete' ELSE 'pending' END,
  contact_owner=null,contact_until=null,contact_next_at=now()+make_interval(secs=>least(86400,300*greatest(1,contact_attempts)))
  WHERE stay_id=p_stay AND revision=p_revision AND contact_owner=p_worker;
 RETURN jsonb_build_object('recorded',FOUND);
END $$;

ALTER TABLE public.lsa07_settings ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lsa07_apartments_info ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lsa07_message_templates ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lsa07_reservations ENABLE ROW LEVEL SECURITY;
ALTER TABLE public.lsa07_jobs ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON public.lsa07_settings,public.lsa07_apartments_info,public.lsa07_message_templates,public.lsa07_reservations,public.lsa07_jobs FROM PUBLIC,anon,authenticated;
GRANT SELECT,INSERT,UPDATE,DELETE ON public.lsa07_settings,public.lsa07_apartments_info,public.lsa07_message_templates,public.lsa07_reservations,public.lsa07_jobs TO service_role;
REVOKE EXECUTE ON FUNCTION public.lsa07_ingest(jsonb),public.lsa07_claim_booking(text),public.lsa07_prepare(uuid,bigint,text,jsonb),public.lsa07_next(text),public.lsa07_result(text,text,jsonb),public.lsa07_contact_claim(text),public.lsa07_contact_result(uuid,bigint,text,boolean) FROM PUBLIC,anon,authenticated;
GRANT EXECUTE ON FUNCTION public.lsa07_ingest(jsonb),public.lsa07_claim_booking(text),public.lsa07_prepare(uuid,bigint,text,jsonb),public.lsa07_next(text),public.lsa07_result(text,text,jsonb),public.lsa07_contact_claim(text),public.lsa07_contact_result(uuid,bigint,text,boolean) TO service_role;
