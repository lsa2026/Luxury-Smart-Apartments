-- Existing 01/02 engines; restricted server-role RPCs, no new guest-facing table.
create or replace function public.lsa_morning_ingest(p_snapshot jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 r public.lsa_v3_reservations%rowtype;
 s jsonb:=p_snapshot; result jsonb;
 k text:=s->>'reservation_key'; old_source bigint;
 phone_changed boolean; access_changed boolean;
begin
 if s is null or not s ?& array['source','reservation_key','payment_verified','status','timezone',
  'source_revision','state_fingerprint','arrival_date','departure_date','starts_at','ends_at',
  'check_in_time','check_out_time','listing_map_id'] then
  raise exception 'Missing MorningStay contract fields';
 end if;
 if coalesce((s->>'source'<>'morningstay' or k !~ '^morningstay:[0-9a-f-]{36}$'
  or coalesce((s->>'payment_verified')::boolean,false) is not true
  or s->>'status' not in ('paid','cancelled')
  or s->>'timezone' not in ('Asia/Riyadh','Africa/Casablanca')
  or coalesce((s->>'source_revision')::bigint,0)<1
  or s->>'state_fingerprint' !~ '^[0-9a-f]{64}$'
  or s->>'arrival_date' is distinct from s->>'departure_date'
  or not ((s->>'starts_at')::timestamptz < (s->>'ends_at')::timestamptz)
  or not ((s->>'check_in_time')::time > time '00:00'
          and (s->>'check_in_time')::time < (s->>'check_out_time')::time
          and (s->>'check_out_time')::time < time '12:00')
  or coalesce((s->>'listing_map_id')::bigint,0)<1),true) then
   raise exception 'Invalid verified MorningStay event';
 end if;
 perform pg_advisory_xact_lock(hashtextextended('lsa-v3-booking:'||k,0));
 select * into r from public.lsa_v3_reservations where reservation_key=k for update;
 if found then
  old_source:=(r.snapshot->>'source_revision')::bigint;
  if (s->>'source_revision')::bigint<old_source then
   return jsonb_build_object('reservation_key',k,'ignored',true,'reason','stale_morning_revision');
  end if;
  if (s->>'source_revision')::bigint=old_source and s->>'state_fingerprint' is distinct from r.fingerprint then
   raise exception 'MorningStay revision payload conflict';
  end if;
 end if;
 phone_changed:=r.reservation_key is not null and s->>'phone' is distinct from r.snapshot->>'phone';
 access_changed:=phone_changed or r.reservation_key is null
   or s->>'listing_map_id' is distinct from r.snapshot->>'listing_map_id'
   or s->>'starts_at' is distinct from r.snapshot->>'starts_at';
 s:=s||jsonb_build_object('dates_changed',true,'notify_date_change',true,
  'calendar_rebuild_required',true,'phone_changed',phone_changed,'access_changed',access_changed,
  'booking_update_policy','morning_exact_window_property_contact',
  'previous_phones',case when phone_changed then jsonb_build_array(r.snapshot->>'phone') else '[]'::jsonb end);
 result:=public.lsa_v3_ingest(s);
 if not coalesce((result->>'ignored')::boolean,false) then
  update public.lsa_guest_reminders set state='superseded',last_error='Morning stay revised',updated_at=now()
   where reservation_key=k and source='morningstay' and state not in ('complete','cancelled','superseded')
    and (guest_data->>'ends_at' is distinct from s->>'ends_at'
     or guest_data->>'listing_map_id' is distinct from s->>'listing_map_id'
     or guest_data->>'phone' is distinct from s->>'phone' or s->>'status'='cancelled');
 end if;
 return result;
end $$;

create or replace function public.lsa_morning_reminders_sync(p_guest jsonb,p_plan jsonb)
returns jsonb language plpgsql security invoker set search_path='' as $$
declare
 r public.lsa_v3_reservations%rowtype;
 k text:=p_guest->>'reservation_key'; co timestamptz:=(p_guest->>'ends_at')::timestamptz;
 due timestamptz; rk text; dk text; plan jsonb:=p_plan; g jsonb;
begin
 perform pg_advisory_xact_lock(hashtextextended('lsa-reminders:'||k,0));
 select * into r from public.lsa_v3_reservations where reservation_key=k for update;
 if not found or r.snapshot->>'source'<>'morningstay' or r.revision<>(p_guest->>'event_revision')::bigint then
  return p_guest||jsonb_build_object('calendar_plan',plan,'reminder_sync',jsonb_build_object('skipped','newer_revision'));
 end if;
 if p_guest->>'event_type'='cancelled' then
  update public.lsa_guest_reminders set state='cancelled',last_error='Morning stay cancelled',updated_at=now()
   where reservation_key=k and state not in ('complete','cancelled');
  return p_guest||jsonb_build_object('calendar_plan',plan,'reminder_sync',jsonb_build_object('cancelled',true));
 end if;
 due:=co-interval '60 minutes';
 if due<=now() then
  return p_guest||jsonb_build_object('calendar_plan',plan,'reminder_sync',jsonb_build_object('checkout_in_confirmation',true));
 end if;
 dk:='morning-checkout:v1:'||md5(k||':'||(p_guest->>'listing_map_id')||':'||extract(epoch from co)::text||':'||(p_guest->>'phone'));
 rk:='morning-reminder:v1:'||k||':'||(p_guest->>'departure_date');
 g:=p_guest||jsonb_build_object('timezone',p_guest->>'timezone','message_key',dk);
 insert into public.lsa_guest_reminders(reminder_key,reservation_key,reminder_kind,target_date,
  due_at,eligible_until,property_timezone,reservation_revision,listing_map_id,phone,guest_data,source,delivery_key)
 values(rk,k,'checkout',(p_guest->>'departure_date')::date,due,co,p_guest->>'timezone',r.revision,
  (p_guest->>'listing_map_id')::bigint,p_guest->>'phone',g,'morningstay',dk)
 on conflict(reminder_key) do update set guest_data=excluded.guest_data,reservation_revision=excluded.reservation_revision,
  due_at=excluded.due_at,eligible_until=excluded.eligible_until,property_timezone=excluded.property_timezone,
  listing_map_id=excluded.listing_map_id,phone=excluded.phone,delivery_key=excluded.delivery_key,
  state=case when lsa_guest_reminders.delivery_key is distinct from excluded.delivery_key
     or lsa_guest_reminders.state in('cancelled','superseded') then 'waiting' else lsa_guest_reminders.state end,
  completed_at=case when lsa_guest_reminders.delivery_key is not distinct from excluded.delivery_key then lsa_guest_reminders.completed_at end,
  updated_at=now();
 return p_guest||jsonb_build_object('calendar_plan',plan,'reminder_sync',jsonb_build_object('scheduled',1,'due_at',due));
end $$;

create or replace function public.lsa_morning_reminders_due()
returns jsonb language sql security invoker set search_path='' as $$
 select jsonb_build_object('reminders',items,'has_due_reminders',jsonb_array_length(items)>0,
  'reminder_diagnostics','[]'::jsonb,'selection_at',now())
 from (select coalesce(jsonb_agg(x),'[]'::jsonb) items
  from jsonb_array_elements(public.lsa_reminders_due('["Asia/Riyadh","Africa/Casablanca"]'::jsonb,false)->'reminders') x
  where x->>'source'='morningstay') selected;
$$;

create or replace function public.lsa_morning_pending()
returns jsonb language sql security invoker set search_path='' as $$
 select coalesce((select jsonb_build_object('has_pending',true,'reservation_key',r.reservation_key)
  from public.lsa_v3_reservations r where r.snapshot->>'source'='morningstay'
   and r.calendar_status<>'completed' and (r.lease_until is null or r.lease_until<=now())
  order by r.updated_at limit 1),jsonb_build_object('has_pending',false));
$$;

revoke all on function public.lsa_morning_ingest(jsonb) from public,anon,authenticated;
revoke all on function public.lsa_morning_reminders_sync(jsonb,jsonb) from public,anon,authenticated;
revoke all on function public.lsa_morning_reminders_due() from public,anon,authenticated;
revoke all on function public.lsa_morning_pending() from public,anon,authenticated;
grant execute on function public.lsa_morning_ingest(jsonb),public.lsa_morning_reminders_sync(jsonb,jsonb),
 public.lsa_morning_reminders_due(),public.lsa_morning_pending() to service_role;
