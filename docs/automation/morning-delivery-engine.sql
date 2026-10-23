do $$ begin if md5(pg_get_functiondef('public.lsa_v32_enqueue(jsonb,text)'::regprocedure)) <> '415e441ed77f49b384923cc14239de3b' then raise exception 'Concurrent function change: lsa_v32_enqueue'; end if; end $$;

CREATE OR REPLACE FUNCTION public.lsa_v32_enqueue(p_messages jsonb, p_lane text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
declare m jsonb; k text; r public.lsa_v3_reservations%rowtype; j public.lsa_v32_delivery_jobs%rowtype;
 legacy_id text; legacy_group_id text; ev jsonb; accepted_count integer:=0; skipped jsonb:='[]';
 validation_error text; initial_state text; group_available boolean; legacy_accepted boolean;
 existing_purpose text; incoming_purpose text; existing_target text; incoming_target text;
 refreshed jsonb; attempted boolean; original_body text; incoming_body text; group_field text; group_text text;
 revived_state text; can_handoff boolean;
begin
 if p_lane not in ('booking','reminders','reviews') or jsonb_typeof(p_messages)<>'array' then raise exception 'Invalid delivery batch'; end if;
 -- Reviews are deliberately paused in this release, including manual callers.
 if p_lane='reviews' then return jsonb_build_object('enqueued',0,'reason','reviews_paused'); end if;
 for m in select value from jsonb_array_elements(p_messages) loop
  -- Daily check-in/out summaries are operational; other lanes retain failures-only routing.
  if not ((p_lane='reminders' and coalesce(m->>'message_type','') in ('reminder_checkin','reminder_checkout')) or (p_lane='booking' and m->>'source'='morningstay' and m->>'message_type' in ('status_new','status_modified','status_cancelled'))) then
   m:=m||jsonb_build_object('should_notify_work_group',false,'send_group_notification',false,'group_success_body',null);
  end if;
  k:=m->>'message_key';
  if nullif(k,'') is null or nullif(m->>'reservation_key','') is null then raise exception 'Missing message identity'; end if;
  select * into r from public.lsa_v3_reservations where reservation_key=m->>'reservation_key';
  if found and r.snapshot->>'source'='morningstay' and m->>'source_revision' is distinct from r.snapshot->>'source_revision' then
   skipped:=skipped||jsonb_build_array(jsonb_build_object('key',k,'reason','newer_morning_revision'));continue;
  end if;
  if found and ((r.snapshot->>'event_type'='cancelled' and m->>'message_type'<>'status_cancelled')
    or (m->>'message_type'<>'status_cancelled' and ((r.snapshot->>'arrival_date') is distinct from (m->>'arrival_date')
      or (r.snapshot->>'departure_date') is distinct from (m->>'departure_date')))) then
   skipped:=skipped||jsonb_build_array(jsonb_build_object('key',k,'reason','reservation_dates_changed_or_cancelled'));continue;
  end if;
  if p_lane='reminders' and nullif(m->>'eligible_until','')::timestamptz<=now() then
   skipped:=skipped||jsonb_build_array(jsonb_build_object('key',k,'reason','reminder_expired'));continue;
  end if;
  ev:=case when jsonb_typeof(m->'calendar_event_ids')='array' then m->'calendar_event_ids' else '[]'::jsonb end;
  if nullif(m->>'calendar_event_id','') is not null then ev:=ev||jsonb_build_array(m->>'calendar_event_id');end if;
  ev:=(select coalesce(jsonb_agg(distinct value),'[]') from jsonb_array_elements(ev)
   where jsonb_typeof(value)='string' and value#>>'{}'<>'');
  perform pg_advisory_xact_lock(hashtextextended('lsa-v32-message:'||k,0));
  select * into j from public.lsa_v32_delivery_jobs where message_key=k for update;
  if found then
   -- Arrival guides and the matching calendar reminder share one stable key.
   -- A departure extension must refresh this same arrival message's stay data,
   -- while a different arrival, recipient or property is a different job.
   existing_purpose:=case when j.payload->>'message_type' in('checkin_guide','reminder_checkin') then 'checkin'
     when j.payload->>'message_type'='reminder_checkout' then 'checkout' else j.payload->>'message_type' end;
   incoming_purpose:=case when m->>'message_type' in('checkin_guide','reminder_checkin') then 'checkin'
     when m->>'message_type'='reminder_checkout' then 'checkout' else m->>'message_type' end;
   existing_target:=case when existing_purpose='checkout' then j.payload->>'departure_date' else j.payload->>'arrival_date' end;
   incoming_target:=case when incoming_purpose='checkout' then m->>'departure_date' else m->>'arrival_date' end;
   if j.reservation_key is distinct from m->>'reservation_key'
    or (j.payload->>'listing_map_id') is distinct from (m->>'listing_map_id')
    or regexp_replace(coalesce(j.payload->>'recipient_phone',''),'[^0-9]','','g') is distinct from
      regexp_replace(coalesce(m->>'recipient_phone',''),'[^0-9]','','g')
    or existing_purpose is distinct from incoming_purpose or existing_target is distinct from incoming_target then
    skipped:=skipped||jsonb_build_array(jsonb_build_object('key',k,'reason','existing_message_identity_mismatch'));continue;
   end if;
   refreshed:=j.payload||m;
   attempted:=j.guest_attempts>0 or j.guest_id is not null or j.guest_reference is not null or j.guest_delivered;
   if attempted then
    original_body:=coalesce(j.payload->>'message_body','');incoming_body:=coalesce(m->>'message_body','');
    refreshed:=refreshed||jsonb_build_object('message_body',original_body,'message_html',j.payload->>'message_html',
      'message_type',j.payload->>'message_type','message_language',j.payload->>'message_language');
    -- Fresh group dates come from the current render. Replace its copied guest
    -- text with the exact text of the already-attempted provider message.
    foreach group_field in array array['group_success_body','group_failure_body'] loop
     group_text:=coalesce(m->>group_field,j.payload->>group_field,'');
     if incoming_body<>'' and right(group_text,char_length(incoming_body))=incoming_body then
      group_text:=left(group_text,char_length(group_text)-char_length(incoming_body))||original_body;
     elsif incoming_body<>'' then group_text:=replace(group_text,incoming_body,original_body);
     end if;
     refreshed:=jsonb_set(refreshed,array[group_field],to_jsonb(group_text));
    end loop;
   end if;
   can_handoff:=j.lease_until is null or j.lease_until<=now();
   revived_state:=j.state;
   if j.state='cancelled' and can_handoff then
    revived_state:=case when j.guest_delivered and (j.group_sent or not coalesce((refreshed->>'should_notify_work_group')::boolean,false)) then 'complete'
      when j.guest_delivered and (j.group_id is not null or j.group_reference is not null) then 'group_check'
      when j.guest_delivered then 'group_send'
      when j.guest_id is not null or j.guest_reference is not null then 'guest_check'
      when j.guest_attempts=0 then 'guest_send' else 'attention' end;
   end if;
   -- Attach a newly encountered calendar ID without ever resending a completed guest message.
   update public.lsa_v32_delivery_jobs set payload=refreshed,state=revived_state,
    lane=case when can_handoff and j.state<>'complete' then p_lane else lane end,
    next_at=case when can_handoff and (j.state='cancelled' or (j.state='guest_send' and not attempted)) then
      greatest(now(),coalesce(nullif(m->>'not_before','')::timestamptz,now())) else next_at end,
    lease_owner=case when can_handoff then null else lease_owner end,
    lease_until=case when can_handoff then null else lease_until end,
    guest_deadline=case when j.state='cancelled' and revived_state='guest_check' then now()+interval '5 minutes' else guest_deadline end,
    group_deadline=case when j.state='cancelled' and revived_state='group_check' then now()+interval '5 minutes' else group_deadline end,
    attention_notified=case when j.state='cancelled' then false else attention_notified end,
    calendar_event_ids=(select coalesce(jsonb_agg(distinct value),'[]') from jsonb_array_elements(calendar_event_ids||ev)),
    cleanup_lane=case when jsonb_array_length(ev)>0 then p_lane else cleanup_lane end,updated_at=now()
    where message_key=k;
   -- A manual retry checks the existing provider ID first. It does not repost an accepted message.
   if (m->>'manual_run')::boolean is true and j.state='attention' then
    update public.lsa_v32_delivery_jobs set state=case when not guest_delivered and (guest_id is not null or guest_reference is not null) then 'guest_check'
      when guest_delivered and p_lane='reminders'
        and coalesce(payload->>'message_type','') in ('reminder_checkin','reminder_checkout')
        and coalesce((payload->>'should_notify_work_group')::boolean,false) and not group_sent then
          case when group_id is not null or group_reference is not null then 'group_check' else 'group_send' end
      when guest_delivered then 'complete'
      else state end,
     guest_deadline=now()+interval '5 minutes',group_deadline=now()+interval '5 minutes',
     payload=case when guest_delivered then jsonb_set(payload,'{group_attempt_limit}',to_jsonb(group_attempts+2)) else payload end,
     next_at=now(),lease_until=null,lease_owner=null,attention_notified=false
    where message_key=k;
   end if;
   accepted_count:=accepted_count+1;continue;
  end if;
  -- Prior acceptance suppresses a new POST, but never proves device delivery.
  select l.provider_message_id into legacy_id from public.automation_message_log l
   where l.delivery_status='accepted' and l.recipient_phone=m->>'recipient_phone'
    and l.event_key in(k,m->>'legacy_event_key',m->>'source_event_key')
    and (l.message_type=m->>'message_type' or
      (l.message_type in('checkin_guide','reminder_checkin') and m->>'message_type' in('checkin_guide','reminder_checkin')))
   order by l.created_at desc limit 1;
  legacy_accepted:=found;
  legacy_group_id:=null;
  if legacy_id is not null then
   select l.provider_message_id into legacy_group_id from public.automation_message_log l
    where l.delivery_status='accepted' and l.recipient_phone=m->>'work_group_id'
     and l.message_type='work_group_'||(m->>'message_type')
     and l.event_key in(k||':work-group:accepted',(m->>'legacy_event_key')||':work-group:accepted',
       (m->>'source_event_key')||':work-group:accepted')
    order by l.created_at desc limit 1;
  end if;
  validation_error:=case when legacy_accepted and legacy_id is null then 'Legacy accepted message has no provider ID; manual verification required'
    when coalesce((m->>'safety_passed')::boolean,false) is not true then 'Guest message safety validation failed'
    when coalesce(m->>'recipient_phone','')!~'^[0-9]{8,15}$' then 'Guest phone missing or invalid'
    when nullif(btrim(m->>'message_body'),'') is null then 'Guest message body is empty' end;
  group_available:=(coalesce((m->>'should_notify_work_group')::boolean,false) or coalesce((m->>'notify_group_on_failure')::boolean,false))
    and coalesce(m->>'work_group_id','')~'^[0-9]+@g[.]us$'
    and nullif(btrim(m->>'group_failure_body'),'') is not null
    and coalesce((m#>>'{safety_checks,production_enabled}')::boolean,true)
    and coalesce((m#>>'{safety_checks,test_mode_disabled}')::boolean,true);
  initial_state:=case when validation_error is not null then case when group_available then 'group_send' else 'attention' end
    when legacy_id is null then 'guest_send' else 'guest_check' end;
  insert into public.lsa_v32_delivery_jobs(message_key,lane,reservation_key,payload,state,guest_id,guest_attempts,guest_deadline,
    group_id,group_attempts,group_deadline,
    next_at,calendar_event_ids,cleanup_lane,last_error)
   values(k,p_lane,m->>'reservation_key',m,initial_state,
    legacy_id,case when legacy_accepted then 1 else 0 end,case when legacy_id is not null then now()+interval '5 minutes' end,
    legacy_group_id,case when legacy_group_id is null then 0 else 1 end,case when legacy_group_id is not null then now()+interval '5 minutes' end,
    greatest(now(),coalesce(nullif(m->>'not_before','')::timestamptz,now())),ev,p_lane,validation_error);
  accepted_count:=accepted_count+1;legacy_id:=null;
 end loop;
 return jsonb_build_object('enqueued',accepted_count,'skipped',skipped,'lane',p_lane);
end $function$
;

do $$ begin if md5(pg_get_functiondef('public.lsa_v32_next(text,text)'::regprocedure)) <> 'bf44329ef45570389c8e97af69df8d23' then raise exception 'Concurrent function change: lsa_v32_next'; end if; end $$;

CREATE OR REPLACE FUNCTION public.lsa_v32_next(p_lane text, p_worker text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
declare j public.lsa_v32_delivery_jobs%rowtype; r public.lsa_v3_reservations%rowtype;
 recipient_value text; clock_at timestamptz; action text; phase text; body text; ref text; n integer;
 next_time timestamptz; detail text;
begin
 if p_lane not in ('booking','reminders','reviews') then raise exception 'Invalid lane'; end if;
 if p_lane='reviews' then return jsonb_build_object('action','done','reason','reviews_paused');end if;
 -- Interrupted POSTs are reconciled by referenceId. They are never blindly posted again.
 update public.lsa_v32_delivery_jobs set state=case when state='guest_posting' then 'guest_check' else 'group_check' end,
  lease_owner=null,lease_until=null,next_at=now(),last_error='Interrupted request; checking provider reference'
  where lane=p_lane and state in('guest_posting','group_posting') and lease_until<=now();
 -- Morning changes include exact times, apartment and phone, even on the same date.
 update public.lsa_v32_delivery_jobs j2 set
  state=case when j2.guest_attempts=0 and j2.group_attempts=0 then 'cancelled' else 'attention' end,
  lease_owner=null,lease_until=null,last_error='MorningStay revision superseded; inspect any attempted delivery',
  attention_notified=false,updated_at=now()
 from public.lsa_v3_reservations r2
 where j2.reservation_key=r2.reservation_key and j2.lane=p_lane
  and r2.snapshot->>'source'='morningstay'
  and j2.payload->>'source_revision' is distinct from r2.snapshot->>'source_revision'
  and j2.state not in('complete','cancelled','attention','guest_posting','group_posting')
  and (j2.lease_until is null or j2.lease_until<=now());
 -- Cancel old work after a genuine date change/cancellation. Metadata-only changes do not invalidate messages.
 update public.lsa_v32_delivery_jobs j2 set state='cancelled',lease_owner=null,lease_until=null,
  last_error='Reservation dates changed or cancelled',updated_at=now()
  from public.lsa_v3_reservations r2
  where j2.reservation_key=r2.reservation_key and j2.lane=p_lane
   and (j2.lease_until is null or j2.lease_until<=now())
   and j2.state not in('complete','cancelled','attention','guest_posting','group_posting')
   and ((r2.snapshot->>'event_type'='cancelled' and j2.payload->>'message_type'<>'status_cancelled')
    or (j2.payload->>'message_type'<>'status_cancelled' and
     ((r2.snapshot->>'arrival_date') is distinct from (j2.payload->>'arrival_date')
      or (r2.snapshot->>'departure_date') is distinct from (j2.payload->>'departure_date'))));
 -- A reminder that became obsolete is not sent. Its event remains for diagnosis.
 update public.lsa_v32_delivery_jobs set state='attention',last_error='Reminder expired before send',next_at=now()
  where lane=p_lane and state='guest_send' and nullif(payload->>'eligible_until','')::timestamptz<=now();
 -- Every outcome stays within its own lane. Failed jobs never block another guest.
 select * into j from public.lsa_v32_delivery_jobs
  where ((lane=p_lane and ((state not in('complete','attention','cancelled') and next_at<=now())
       or(state='attention' and not attention_notified)))
    or(cleanup_lane=p_lane and state='attention' and cleanup_attempts>=2 and not attention_notified)
    or(cleanup_lane=p_lane and state='complete' and jsonb_array_length(calendar_event_ids)>0 and cleanup_attempts<2 and next_at<=now()))
   and (lease_until is null or lease_until<=now())
  order by case when state='attention' then 0 when state='complete' then 1
    when state='guest_check' and guest_deadline<=now() then 2
    when state='group_check' and group_deadline<=now() then 2
    when state in('guest_send','group_send') then 3 else 4 end,
   created_at,coalesce(nullif(payload->>'message_sequence','')::integer,1)
  for update skip locked limit 1;
 if not found then
  select min(greatest(next_at,coalesce(lease_until,next_at))) into next_time from public.lsa_v32_delivery_jobs
   where lane=p_lane and state not in('complete','attention','cancelled');
  if next_time is not null and next_time<now()+interval '6 minutes' then
   return jsonb_build_object('action','wait','wait_seconds',greatest(1,least(15,ceil(extract(epoch from(next_time-now()))))));end if;
  return jsonb_build_object('action','done','lane',p_lane);
 end if;
 if j.lease_until>now() then return jsonb_build_object('action','wait','wait_seconds',5); end if;
 if j.state='attention' then
  update public.lsa_v32_delivery_jobs set attention_notified=true where message_key=j.message_key;
  return j.payload||jsonb_build_object('action','notify','message_key',j.message_key,'error',j.last_error,
   'guest_delivered',j.guest_delivered,'group_sent',j.group_sent,'execution_id',p_worker);
 end if;
 if j.state='complete' then
  update public.lsa_v32_delivery_jobs set lease_owner=p_worker,lease_until=now()+interval '90 seconds',cleanup_attempts=cleanup_attempts+1
   where message_key=j.message_key;
  return j.payload||jsonb_build_object('action','delete','message_key',j.message_key,'calendar_event_id',j.calendar_event_ids->>0);
 end if;
 phase:=case when j.state like 'group_%' then 'group' else 'guest' end;
 action:=case when j.state in('guest_send','group_send') then 'send' else 'check' end;
 recipient_value:=case when phase='group' then j.payload->>'work_group_id' else j.payload->>'recipient_phone' end;
 if phase='group' then
  -- Freeze the complete group copy once. A later failure/retry must not change
  -- character offsets or remove any of the exact guest-message copy.
  body:=coalesce(j.group_body,case when j.guest_delivered then j.payload->>'group_success_body' else
   replace(coalesce(j.payload->>'group_failure_body',''),'__SEND_ERROR__',coalesce(j.last_error,'لم يتأكد التسليم خلال خمس دقائق')) end);
  if j.group_body is null then
   j.group_body:=body;
   update public.lsa_v32_delivery_jobs set group_body=body where message_key=j.message_key;
  end if;
  body:=substring(body from j.group_part_index*4096+1 for 4096);
 else body:=j.payload->>'message_body';end if;
 if action='send' then
  if nullif(btrim(body),'') is null or
    (phase='guest' and (coalesce(recipient_value,'')!~'^[0-9]{8,15}$' or coalesce((j.payload->>'safety_passed')::boolean,false) is not true)) or
    (phase='group' and coalesce(recipient_value,'')!~'^[0-9]+@g[.]us$') then
   update public.lsa_v32_delivery_jobs set state='attention',last_error='Outbound recipient, body or safety validation failed',
    attention_notified=false,lease_owner=null,lease_until=null where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',1,'reason','outbound_validation_failed');
  end if;
  insert into public.lsa_v32_recipient_clock(recipient)values(recipient_value)on conflict do nothing;
  select c.next_at into clock_at from public.lsa_v32_recipient_clock c where c.recipient=recipient_value for update;
  if clock_at>now() then
   update public.lsa_v32_delivery_jobs set next_at=clock_at where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',greatest(1,least(5,ceil(extract(epoch from(clock_at-now()))))));
  end if;
  if phase='guest' and exists(select 1 from public.lsa_v32_delivery_jobs prev
    where prev.reservation_key=j.reservation_key and prev.lane=p_lane and prev.state in('guest_send','guest_posting')
     and coalesce(nullif(prev.payload->>'message_sequence','')::integer,1)<coalesce(nullif(j.payload->>'message_sequence','')::integer,1)) then
   update public.lsa_v32_delivery_jobs set next_at=now()+interval '5 seconds' where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',5);
  end if;
  n:=case when phase='guest' then j.guest_attempts+1 else j.group_attempts+1 end;
  ref:='lsa32-'||md5(j.message_key)||'-'||phase||'-'||n;
  update public.lsa_v32_recipient_clock c set next_at=now()+interval '5 seconds' where c.recipient=recipient_value;
  update public.lsa_v32_delivery_jobs set state=case when phase='guest' then 'guest_posting' else 'group_posting' end,
   guest_attempts=case when phase='guest' then n else guest_attempts end,
   group_attempts=case when phase='group' then n else group_attempts end,
   guest_reference=case when phase='guest' then ref else guest_reference end,
   group_reference=case when phase='group' then ref else group_reference end,
   guest_id=case when phase='guest' then null else guest_id end,
   group_id=case when phase='group' then null else group_id end,
   guest_deadline=case when phase='guest' then now()+interval '5 minutes' else guest_deadline end,
   group_deadline=case when phase='group' then now()+interval '5 minutes' else group_deadline end
   where message_key=j.message_key;
 else ref:=case when phase='guest' then j.guest_reference else j.group_reference end;
 end if;
 update public.lsa_v32_delivery_jobs set lease_owner=p_worker,lease_until=now()+interval '90 seconds',updated_at=now()
  where message_key=j.message_key;
 return j.payload||jsonb_build_object('action',action,'phase',phase,'message_key',j.message_key,'recipient_phone',recipient_value,'message_body',body,
  'provider_message_id',case when action='send' then null when phase='guest' then j.guest_id else j.group_id end,'reference_id',ref,
  'attempt',case when action='send' then n when phase='guest' then j.guest_attempts else j.group_attempts end,
  'group_part',case when phase='group' then j.group_part_index+1 else null end,
  'group_parts',case when phase='group' then (char_length(j.group_body)+4095)/4096 else null end,
  'deadline',case when action='send' then now()+interval '5 minutes' when phase='guest' then j.guest_deadline else j.group_deadline end,'guest_delivered',j.guest_delivered);
end $function$
;

CREATE OR REPLACE FUNCTION public.lsa_morning_next(p_lane text, p_worker text)
 RETURNS jsonb
 LANGUAGE plpgsql
 SET search_path TO ''
AS $function$
declare j public.lsa_v32_delivery_jobs%rowtype; r public.lsa_v3_reservations%rowtype;
 recipient_value text; clock_at timestamptz; action text; phase text; body text; ref text; n integer;
 next_time timestamptz; detail text;
begin
 if p_lane not in ('booking','reminders','reviews') then raise exception 'Invalid lane'; end if;
 if p_lane='reviews' then return jsonb_build_object('action','done','reason','reviews_paused');end if;
 -- Interrupted POSTs are reconciled by referenceId. They are never blindly posted again.
 update public.lsa_v32_delivery_jobs set state=case when state='guest_posting' then 'guest_check' else 'group_check' end,
  lease_owner=null,lease_until=null,next_at=now(),last_error='Interrupted request; checking provider reference'
  where payload->>'source'='morningstay' and lane=p_lane and state in('guest_posting','group_posting') and lease_until<=now();
 -- Morning changes include exact times, apartment and phone, even on the same date.
 update public.lsa_v32_delivery_jobs j2 set
  state=case when j2.guest_attempts=0 and j2.group_attempts=0 then 'cancelled' else 'attention' end,
  lease_owner=null,lease_until=null,last_error='MorningStay revision superseded; inspect any attempted delivery',
  attention_notified=false,updated_at=now()
 from public.lsa_v3_reservations r2
 where j2.reservation_key=r2.reservation_key and j2.lane=p_lane and j2.payload->>'source'='morningstay'
  and r2.snapshot->>'source'='morningstay'
  and j2.payload->>'source_revision' is distinct from r2.snapshot->>'source_revision'
  and j2.state not in('complete','cancelled','attention','guest_posting','group_posting')
  and (j2.lease_until is null or j2.lease_until<=now());
 -- Cancel old work after a genuine date change/cancellation. Metadata-only changes do not invalidate messages.
 update public.lsa_v32_delivery_jobs j2 set state='cancelled',lease_owner=null,lease_until=null,
  last_error='Reservation dates changed or cancelled',updated_at=now()
  from public.lsa_v3_reservations r2
  where j2.reservation_key=r2.reservation_key and j2.lane=p_lane and j2.payload->>'source'='morningstay'
   and (j2.lease_until is null or j2.lease_until<=now())
   and j2.state not in('complete','cancelled','attention','guest_posting','group_posting')
   and ((r2.snapshot->>'event_type'='cancelled' and j2.payload->>'message_type'<>'status_cancelled')
    or (j2.payload->>'message_type'<>'status_cancelled' and
     ((r2.snapshot->>'arrival_date') is distinct from (j2.payload->>'arrival_date')
      or (r2.snapshot->>'departure_date') is distinct from (j2.payload->>'departure_date'))));
 -- A reminder that became obsolete is not sent. Its event remains for diagnosis.
 update public.lsa_v32_delivery_jobs set state='attention',last_error='Reminder expired before send',next_at=now()
  where payload->>'source'='morningstay' and lane=p_lane and state='guest_send' and nullif(payload->>'eligible_until','')::timestamptz<=now();
 -- Every outcome stays within its own lane. Failed jobs never block another guest.
 select * into j from public.lsa_v32_delivery_jobs
  where payload->>'source'='morningstay' and ((lane=p_lane and ((state not in('complete','attention','cancelled') and next_at<=now())
       or(state='attention' and not attention_notified)))
    or(cleanup_lane=p_lane and state='attention' and cleanup_attempts>=2 and not attention_notified)
    or(cleanup_lane=p_lane and state='complete' and jsonb_array_length(calendar_event_ids)>0 and cleanup_attempts<2 and next_at<=now()))
   and (lease_until is null or lease_until<=now())
  order by case when state='attention' then 0 when state='complete' then 1
    when state='guest_check' and guest_deadline<=now() then 2
    when state='group_check' and group_deadline<=now() then 2
    when state in('guest_send','group_send') then 3 else 4 end,
   created_at,coalesce(nullif(payload->>'message_sequence','')::integer,1)
  for update skip locked limit 1;
 if not found then
  select min(greatest(next_at,coalesce(lease_until,next_at))) into next_time from public.lsa_v32_delivery_jobs
   where payload->>'source'='morningstay' and lane=p_lane and state not in('complete','attention','cancelled');
  if next_time is not null and next_time<now()+interval '6 minutes' then
   return jsonb_build_object('action','wait','wait_seconds',greatest(1,least(15,ceil(extract(epoch from(next_time-now()))))));end if;
  return jsonb_build_object('action','done','lane',p_lane);
 end if;
 if j.lease_until>now() then return jsonb_build_object('action','wait','wait_seconds',5); end if;
 if j.state='attention' then
  update public.lsa_v32_delivery_jobs set attention_notified=true where message_key=j.message_key;
  return j.payload||jsonb_build_object('action','notify','message_key',j.message_key,'error',j.last_error,
   'guest_delivered',j.guest_delivered,'group_sent',j.group_sent,'execution_id',p_worker);
 end if;
 if j.state='complete' then
  update public.lsa_v32_delivery_jobs set lease_owner=p_worker,lease_until=now()+interval '90 seconds',cleanup_attempts=cleanup_attempts+1
   where message_key=j.message_key;
  return j.payload||jsonb_build_object('action','delete','message_key',j.message_key,'calendar_event_id',j.calendar_event_ids->>0);
 end if;
 phase:=case when j.state like 'group_%' then 'group' else 'guest' end;
 action:=case when j.state in('guest_send','group_send') then 'send' else 'check' end;
 recipient_value:=case when phase='group' then j.payload->>'work_group_id' else j.payload->>'recipient_phone' end;
 if phase='group' then
  -- Freeze the complete group copy once. A later failure/retry must not change
  -- character offsets or remove any of the exact guest-message copy.
  body:=coalesce(j.group_body,case when j.guest_delivered then j.payload->>'group_success_body' else
   replace(coalesce(j.payload->>'group_failure_body',''),'__SEND_ERROR__',coalesce(j.last_error,'لم يتأكد التسليم خلال خمس دقائق')) end);
  if j.group_body is null then
   j.group_body:=body;
   update public.lsa_v32_delivery_jobs set group_body=body where message_key=j.message_key;
  end if;
  body:=substring(body from j.group_part_index*4096+1 for 4096);
 else body:=j.payload->>'message_body';end if;
 if action='send' then
  if nullif(btrim(body),'') is null or
    (phase='guest' and (coalesce(recipient_value,'')!~'^[0-9]{8,15}$' or coalesce((j.payload->>'safety_passed')::boolean,false) is not true)) or
    (phase='group' and coalesce(recipient_value,'')!~'^[0-9]+@g[.]us$') then
   update public.lsa_v32_delivery_jobs set state='attention',last_error='Outbound recipient, body or safety validation failed',
    attention_notified=false,lease_owner=null,lease_until=null where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',1,'reason','outbound_validation_failed');
  end if;
  insert into public.lsa_v32_recipient_clock(recipient)values(recipient_value)on conflict do nothing;
  select c.next_at into clock_at from public.lsa_v32_recipient_clock c where c.recipient=recipient_value for update;
  if clock_at>now() then
   update public.lsa_v32_delivery_jobs set next_at=clock_at where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',greatest(1,least(5,ceil(extract(epoch from(clock_at-now()))))));
  end if;
  if phase='guest' and exists(select 1 from public.lsa_v32_delivery_jobs prev
    where prev.reservation_key=j.reservation_key and prev.lane=p_lane and prev.state in('guest_send','guest_posting')
     and coalesce(nullif(prev.payload->>'message_sequence','')::integer,1)<coalesce(nullif(j.payload->>'message_sequence','')::integer,1)) then
   update public.lsa_v32_delivery_jobs set next_at=now()+interval '5 seconds' where message_key=j.message_key;
   return jsonb_build_object('action','wait','wait_seconds',5);
  end if;
  n:=case when phase='guest' then j.guest_attempts+1 else j.group_attempts+1 end;
  ref:='lsa32-'||md5(j.message_key)||'-'||phase||'-'||n;
  update public.lsa_v32_recipient_clock c set next_at=now()+interval '5 seconds' where c.recipient=recipient_value;
  update public.lsa_v32_delivery_jobs set state=case when phase='guest' then 'guest_posting' else 'group_posting' end,
   guest_attempts=case when phase='guest' then n else guest_attempts end,
   group_attempts=case when phase='group' then n else group_attempts end,
   guest_reference=case when phase='guest' then ref else guest_reference end,
   group_reference=case when phase='group' then ref else group_reference end,
   guest_id=case when phase='guest' then null else guest_id end,
   group_id=case when phase='group' then null else group_id end,
   guest_deadline=case when phase='guest' then now()+interval '5 minutes' else guest_deadline end,
   group_deadline=case when phase='group' then now()+interval '5 minutes' else group_deadline end
   where message_key=j.message_key;
 else ref:=case when phase='guest' then j.guest_reference else j.group_reference end;
 end if;
 update public.lsa_v32_delivery_jobs set lease_owner=p_worker,lease_until=now()+interval '90 seconds',updated_at=now()
  where message_key=j.message_key;
 return j.payload||jsonb_build_object('action',action,'phase',phase,'message_key',j.message_key,'recipient_phone',recipient_value,'message_body',body,
  'provider_message_id',case when action='send' then null when phase='guest' then j.guest_id else j.group_id end,'reference_id',ref,
  'attempt',case when action='send' then n when phase='guest' then j.guest_attempts else j.group_attempts end,
  'group_part',case when phase='group' then j.group_part_index+1 else null end,
  'group_parts',case when phase='group' then (char_length(j.group_body)+4095)/4096 else null end,
  'deadline',case when action='send' then now()+interval '5 minutes' when phase='guest' then j.guest_deadline else j.group_deadline end,'guest_delivered',j.guest_delivered);
end $function$
;
revoke all on function public.lsa_morning_next(text,text) from public,anon,authenticated;
grant execute on function public.lsa_morning_next(text,text) to service_role;
