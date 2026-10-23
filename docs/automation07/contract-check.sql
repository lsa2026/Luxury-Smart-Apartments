-- Subtransaction tests: every synthetic row and enabled flag is rolled back.
DO $contract$
DECLARE k uuid:='07000000-0000-4000-8000-000000000001';s jsonb;a jsonb;b jsonb;listing bigint;failed boolean;
BEGIN
 BEGIN
  UPDATE public.lsa07_settings SET enabled=true WHERE id;
  SELECT min(listing_map_id) INTO listing FROM public.lsa07_apartments_info;
  IF listing IS NULL THEN RAISE EXCEPTION 'Scenario 07 configuration has no apartments';END IF;
  s:=jsonb_build_object('source','morning07','payment_verified',true,'status','paid',
    'morning_stay_id',k,'source_revision',1,'state_fingerprint',repeat('a',64),
    'phone','+16502530000','starts_at','2032-01-01T01:00:00+03:00','ends_at','2032-01-01T11:00:00+03:00',
    'arrival_date','2032-01-01','departure_date','2032-01-01','check_in_time','01:00','check_out_time','11:00',
    'timezone','Asia/Riyadh','source_event_id','07000000-0000-4000-8000-000000000002',
    'delivery_token',repeat('synthetic-capability-',4),'listing_map_id',listing,
    'management_url','https://example.invalid/synthetic-morning07');
  failed:=false;
  BEGIN PERFORM public.lsa07_ingest(s-'phone');EXCEPTION WHEN OTHERS THEN failed:=true;END;
  IF NOT failed THEN RAISE EXCEPTION 'Missing mandatory phone accepted';END IF;
  failed:=false;
  BEGIN PERFORM public.lsa07_ingest(s||'{"payment_verified":false}');EXCEPTION WHEN OTHERS THEN failed:=true;END;
  IF NOT failed THEN RAISE EXCEPTION 'Unverified payment accepted';END IF;
  failed:=false;
  BEGIN PERFORM public.lsa07_ingest(s-'status');EXCEPTION WHEN OTHERS THEN failed:=true;END;
  IF NOT failed THEN RAISE EXCEPTION 'Missing admission status accepted';END IF;
  a:=public.lsa07_ingest(s);
  IF a->'accepted'<>'true'::jsonb THEN RAISE EXCEPTION 'Valid event rejected';END IF;
  a:=public.lsa07_ingest(s);
  IF a->'duplicate'<>'true'::jsonb OR (SELECT count(*) FROM public.lsa07_reservations WHERE stay_id=k)<>1 THEN RAISE EXCEPTION 'Duplicate event not durable';END IF;
  failed:=false;
  BEGIN PERFORM public.lsa07_ingest(s||jsonb_build_object('state_fingerprint',repeat('b',64)));EXCEPTION WHEN OTHERS THEN failed:=true;END;
  IF NOT failed THEN RAISE EXCEPTION 'Conflicting same revision accepted';END IF;
  a:=public.lsa07_claim_booking('synthetic-worker');
  IF a->'has_work'<>'true'::jsonb THEN RAISE EXCEPTION 'Preparation not resumable';END IF;
  PERFORM public.lsa07_prepare(k,1,'synthetic-worker',jsonb_build_array(
    jsonb_build_object('job_key','lsa07:'||k||':r1:status','kind','status_new','recipient_kind','guest','body','synthetic confirmation','sequence',1,'due_at',now()),
    jsonb_build_object('job_key','lsa07:'||k||':checkout-stable','kind','reminder_checkout','recipient_kind','guest','body','synthetic checkout','sequence',4,'due_at','2032-01-01T07:00:00Z','eligible_until','2032-01-01T08:00:00Z')
  ));
  a:=public.lsa07_next('synthetic-worker');
  IF a->>'action'<>'send' OR a->>'message_type'<>'status_new' THEN RAISE EXCEPTION 'Initial dispatch did not claim own status';END IF;
  b:=public.lsa07_next('another-worker');
  IF b->>'action'='send' THEN RAISE EXCEPTION 'Concurrent worker duplicated POST claim';END IF;
  b:=public.lsa07_result(a->>'job_key','wrong-worker','{"delivered":true}');
  IF b->'recorded'<>'false'::jsonb THEN RAISE EXCEPTION 'Stale worker updated receipt';END IF;
  UPDATE public.lsa07_jobs SET lease_until=now()-interval '1 second',next_at=now() WHERE job_key=a->>'job_key';
  b:=public.lsa07_next('recovery-worker');
  IF b->>'action'<>'check' OR (SELECT attempts FROM public.lsa07_jobs WHERE job_key=a->>'job_key')<>1 THEN RAISE EXCEPTION 'Ambiguous POST was resent';END IF;
  PERFORM public.lsa07_result(b->>'job_key','recovery-worker','{"delivered":true,"provider_id":"synthetic-provider"}');
  s:=s||jsonb_build_object('source_revision',2,'state_fingerprint',repeat('b',64),'guest_name','Synthetic changed name');
  PERFORM public.lsa07_ingest(s);
  a:=public.lsa07_claim_booking('synthetic-worker');
  PERFORM public.lsa07_prepare(k,2,'synthetic-worker',jsonb_build_array(
    jsonb_build_object('job_key','lsa07:'||k||':checkout-stable','kind','reminder_checkout','recipient_kind','guest','body','updated checkout','sequence',4,'due_at','2032-01-01T07:00:00Z','eligible_until','2032-01-01T08:00:00Z')
  ));
  IF NOT EXISTS(SELECT 1 FROM public.lsa07_jobs WHERE job_key='lsa07:'||k||':checkout-stable' AND revision=2 AND state='pending') THEN RAISE EXCEPTION 'Unchanged-window reminder lost on metadata revision';END IF;
  a:=public.lsa07_contact_claim('synthetic-contact-worker');
  IF a->'has_contact'<>'true'::jsonb THEN RAISE EXCEPTION 'Separate contact work missing';END IF;
  PERFORM public.lsa07_contact_result(k,2,'synthetic-contact-worker',true);
  s:=s||jsonb_build_object('source_revision',3,'state_fingerprint',repeat('c',64),'ends_at','2032-01-01T10:00:00+03:00','check_out_time','10:00');
  PERFORM public.lsa07_ingest(s);
  IF EXISTS(SELECT 1 FROM public.lsa07_jobs WHERE stay_id=k AND kind='reminder_checkout' AND state='pending') THEN RAISE EXCEPTION 'Old checkout was not superseded';END IF;
  a:=public.lsa07_ingest(s||'{"source_revision":1}');
  IF a->>'reason'<>'stale_revision' THEN RAISE EXCEPTION 'Stale event reactivated booking';END IF;
  PERFORM public.lsa07_ingest(s||jsonb_build_object('source_revision',4,'state_fingerprint',repeat('d',64),'status','cancelled'));
  IF EXISTS(SELECT 1 FROM public.lsa07_jobs WHERE stay_id=k AND state IN ('pending','posting','check')) THEN RAISE EXCEPTION 'Cancellation retained active jobs';END IF;
  RAISE EXCEPTION USING ERRCODE='ZX007',MESSAGE='Scenario 07 synthetic contract passed; roll back fixtures';
 EXCEPTION WHEN SQLSTATE 'ZX007' THEN NULL;
 END;
 IF EXISTS(SELECT 1 FROM public.lsa07_reservations WHERE stay_id=k) OR (SELECT enabled FROM public.lsa07_settings WHERE id) THEN
  RAISE EXCEPTION 'Synthetic rows or test activation leaked';END IF;
END $contract$;
