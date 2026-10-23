-- Run in the same migration transaction, before publishing either minute trigger.
-- The inner subtransaction always rolls back every synthetic booking/job/reminder.
do $test$
declare
 k text:='morningstay:00000000-0000-4000-8000-000000009991';
 day date:=(now() at time zone 'Asia/Riyadh')::date+2;
 s jsonb; r jsonb; g jsonb; job jsonb; before_count bigint;
begin
 if exists(select 1 from public.lsa_v3_reservations where reservation_key=k) then
  raise exception 'Synthetic test key already exists; refusing to overwrite';
 end if;
 select count(*) into before_count from public.lsa_v32_delivery_jobs;
 begin
  s:=jsonb_build_object('source','morningstay','reservation_key',k,'payment_verified',true,
   'status','paid','timezone','Asia/Riyadh','source_revision',1,'state_fingerprint',repeat('a',64),
   'arrival_date',day,'departure_date',day,'check_in_time','01:00','check_out_time','11:00',
   'starts_at',(day+time '01:00') at time zone 'Asia/Riyadh',
   'ends_at',(day+time '11:00') at time zone 'Asia/Riyadh',
   'listing_map_id',97001,'phone','16502530000','guest_name','Synthetic Guest','guest_language','ar',
   'event_type','new','event_key','synthetic-morning-v1','source_updated_at',now(),'authoritative_at',now());
  begin
   perform public.lsa_morning_ingest(s||'{"payment_verified":false}'::jsonb);
   raise exception using errcode='ZX002',message='Invalid payment was accepted';
  exception when sqlstate 'P0001' then
   if sqlerrm <> 'Invalid verified MorningStay event' then raise; end if;
  end;
  r:=public.lsa_morning_ingest(s);
  if r->>'event_revision'<>'1' or (r->>'ignored')::boolean then raise exception 'Initial ingest failed'; end if;
  r:=public.lsa_morning_ingest(s);
  if (select revision from public.lsa_v3_reservations where reservation_key=k)<>1 then raise exception 'Duplicate increased revision'; end if;
  select snapshot into g from public.lsa_v3_reservations where reservation_key=k;
  perform public.lsa_morning_reminders_sync(g,'{}'::jsonb);
  if (select count(*) from public.lsa_guest_reminders where reservation_key=k and source='morningstay')<>1 then raise exception 'Missing morning checkout reminder'; end if;
  if exists(select 1 from public.lsa_guest_reminders where reservation_key=k and due_at<>eligible_until-interval '60 minutes') then raise exception 'Wrong checkout due time'; end if;
  job:=g||jsonb_build_object('message_key','synthetic-morning-job-v1','message_type','status_new',
   'recipient_phone','16502530000','message_body','Synthetic only','safety_passed',true,
   'should_notify_work_group',true,'notify_group_on_failure',true,
   'work_group_id','120363408128237629@g.us','group_success_body','Synthetic team notice',
   'group_failure_body','Synthetic failure','not_before',now());
  perform public.lsa_v32_enqueue(jsonb_build_array(job),'booking');
  perform public.lsa_v32_enqueue(jsonb_build_array(job),'booking');
  if (select count(*) from public.lsa_v32_delivery_jobs where reservation_key=k)<>1 then raise exception 'Duplicate delivery job'; end if;
  if not (select (payload->>'should_notify_work_group')::boolean from public.lsa_v32_delivery_jobs where reservation_key=k) then raise exception 'Morning team notice lost'; end if;
  s:=s||jsonb_build_object('source_revision',2,'state_fingerprint',repeat('b',64),
   'phone','16502530001','source_updated_at',now()+interval '1 second','event_type','modified');
  perform public.lsa_morning_ingest(s);
  select snapshot into g from public.lsa_v3_reservations where reservation_key=k;
  if g->>'phone_changed'<>'true' or g->>'effective_event_type'<>'modified' then raise exception 'Same-day phone change ignored'; end if;
  -- Obsolete jobs are cancelled before any provider action. No HTTP occurs in SQL.
  r:=public.lsa_morning_next('booking','synthetic-worker');
  if exists(select 1 from public.lsa_v32_delivery_jobs where reservation_key=k and state<>'cancelled') then raise exception 'Obsolete booking delivery survived'; end if;
  perform public.lsa_morning_reminders_sync(g,'{}'::jsonb);
  if (select count(*) from public.lsa_guest_reminders where reservation_key=k)<>1
   or (select phone from public.lsa_guest_reminders where reservation_key=k)<>'16502530001' then raise exception 'Same-day reminder phone update failed'; end if;
  r:=public.lsa_v32_enqueue(jsonb_build_array(job),'booking');
  if r->>'enqueued'<>'0' then raise exception 'Old revision enqueued'; end if;
  s:=s||jsonb_build_object('source_revision',3,'state_fingerprint',repeat('c',64),
   'status','cancelled','event_type','cancelled','source_updated_at',now()+interval '2 seconds');
  perform public.lsa_morning_ingest(s);
  select snapshot into g from public.lsa_v3_reservations where reservation_key=k;
  perform public.lsa_morning_reminders_sync(g,'{}'::jsonb);
  if exists(select 1 from public.lsa_guest_reminders where reservation_key=k and state<>'cancelled') then raise exception 'Cancellation left an active reminder'; end if;
  raise exception using errcode='ZX001',message='Rollback successful synthetic contract checks';
 exception when sqlstate 'ZX001' then null;
 end;
 if exists(select 1 from public.lsa_v3_reservations where reservation_key=k)
  or exists(select 1 from public.lsa_guest_reminders where reservation_key=k)
  or (select count(*) from public.lsa_v32_delivery_jobs)<>before_count then
  raise exception 'Synthetic test data was not fully rolled back';
 end if;
end $test$;
