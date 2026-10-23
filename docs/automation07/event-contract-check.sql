DO $contract$
DECLARE initial_enabled boolean;k uuid:='07000000-0000-4000-8000-000000000001';k2 uuid:='07000000-0000-4000-8000-000000000003';s jsonb;a jsonb;b jsonb;listing bigint;
BEGIN
 SELECT enabled INTO initial_enabled FROM public.lsa07_settings WHERE id;
 BEGIN
  UPDATE public.lsa07_settings SET enabled=true WHERE id;
  SELECT min(listing_map_id) INTO listing FROM public.lsa07_apartments_info;
  s:=jsonb_build_object('source','morning07','payment_verified',true,'status','paid',
    'morning_stay_id',k,'source_revision',1,'state_fingerprint',repeat('a',64),
    'phone','+16502530000','starts_at','2032-01-01T01:00:00+03:00','ends_at','2032-01-01T11:00:00+03:00',
    'arrival_date','2032-01-01','departure_date','2032-01-01','check_in_time','01:00','check_out_time','11:00',
    'timezone','Asia/Riyadh','source_event_id','07000000-0000-4000-8000-000000000002',
    'delivery_token',repeat('synthetic-capability-',4),'listing_map_id',listing,
    'management_url','https://example.invalid/synthetic-morning07');
  PERFORM public.lsa07_ingest(s);
  a:=public.lsa07_ingest(s);
  IF a->'duplicate'<>'true'::jsonb THEN RAISE EXCEPTION 'Duplicate was not durable';END IF;
  PERFORM public.lsa07_ingest(s||jsonb_build_object('morning_stay_id',k2));
  a:=public.lsa07_event_claim_booking('k2-worker',k2,1);
  IF (a->'booking'->>'morning_stay_id')::uuid<>k2 THEN RAISE EXCEPTION 'Claim crossed stay boundary';END IF;
  a:=public.lsa07_event_claim_booking('worker',k,1);
  IF a->'has_work'<>'true'::jsonb THEN RAISE EXCEPTION 'Targeted claim failed';END IF;
  PERFORM public.lsa07_prepare(k,1,'worker',jsonb_build_array(
   jsonb_build_object('job_key','lsa07:'||k||':r1:status','kind','status_new','recipient_kind','guest','body','synthetic status','sequence',1,'due_at',now()),
   jsonb_build_object('job_key','lsa07:'||k||':checkout','kind','reminder_checkout','recipient_kind','guest','body','synthetic checkout','sequence',4,'due_at','2032-01-01T07:00:00Z','eligible_until','2032-01-01T08:00:00Z')
  ));
  a:=public.lsa07_event_next('worker',k,1);
  IF a->>'action'<>'send' THEN RAISE EXCEPTION 'Initial targeted send missing';END IF;
  b:=public.lsa07_event_next('another',k,1);
  IF b->>'action'='send' THEN RAISE EXCEPTION 'Duplicate provider POST claimed';END IF;
  PERFORM public.lsa07_result(a->>'job_key','worker','{"delivered":true}');
  a:=public.lsa07_event_contact_claim('contact',k,1);
  IF (a->>'morning_stay_id')::uuid<>k THEN RAISE EXCEPTION 'Contact crossed stay boundary';END IF;
  PERFORM public.lsa07_contact_result(k,1,'contact',true);
  a:=public.lsa07_event_next('worker',k,1);
  IF a->>'action'<>'wait' OR (a->>'wake_at')::timestamptz<>'2032-01-01T07:00:00Z'::timestamptz THEN RAISE EXCEPTION 'Checkout must sleep until exact booking deadline: %',a;END IF;
  a:=public.lsa07_event_next('worker',k,99);
  IF a->>'action'<>'done' THEN RAISE EXCEPTION 'Unknown revision did not finish';END IF;
  PERFORM public.lsa07_ingest(s||jsonb_build_object('source_revision',2,'state_fingerprint',repeat('b',64),'status','cancelled'));
  a:=public.lsa07_event_next('old-waiting-worker',k,1);
  IF a->>'action'<>'done' THEN RAISE EXCEPTION 'Old waiting revision was not retired';END IF;
  IF EXISTS(SELECT 1 FROM public.lsa07_jobs WHERE stay_id=k AND kind='reminder_checkout' AND state IN ('pending','posting','check')) THEN RAISE EXCEPTION 'Cancellation left a live reminder';END IF;
  RAISE EXCEPTION USING ERRCODE='ZX007',MESSAGE='Event-only contract passed; rollback fixtures';
 EXCEPTION WHEN SQLSTATE 'ZX007' THEN NULL;
 END;
 IF EXISTS(SELECT 1 FROM public.lsa07_reservations WHERE stay_id IN(k,k2)) OR (SELECT enabled FROM public.lsa07_settings WHERE id) IS DISTINCT FROM initial_enabled THEN RAISE EXCEPTION 'Test data leaked';END IF;
END $contract$;
SELECT 'event-only, exact checkout wait, concurrent duplicate, isolated contact and cancelled revision: passed' AS result;
