-- Preserve failures-only booking policy when a legacy payload has no source field.
do $$
declare d text;
begin
 select pg_get_functiondef('public.lsa_v32_enqueue(jsonb,text)'::regprocedure) into d;
 if strpos(d, 'p_lane=''booking'' and m->>''source''=''morningstay''')=0 then
  raise exception 'Expected MorningStay booking policy was changed concurrently';
 end if;
 execute replace(d,'p_lane=''booking'' and m->>''source''=''morningstay''',
  'p_lane=''booking'' and coalesce(m->>''source'','''')=''morningstay''');
end $$;

do $test$
declare k text:='synthetic-morning-null-source-policy-20261023';
begin
 if exists(select 1 from public.lsa_v32_delivery_jobs where message_key=k) then
  raise exception 'Synthetic source-policy key already exists';
 end if;
 begin
  perform public.lsa_v32_enqueue(jsonb_build_array(jsonb_build_object(
   'message_key',k,'reservation_key',k,'message_type','status_new',
   'recipient_phone','16502530000','message_body','Synthetic only','safety_passed',true,
   'should_notify_work_group',true,'notify_group_on_failure',true,
   'work_group_id','120363408128237629@g.us','group_success_body','Synthetic only',
   'group_failure_body','Synthetic only')),'booking');
  if exists(select 1 from public.lsa_v32_delivery_jobs where message_key=k
   and (coalesce((payload->>'should_notify_work_group')::boolean,false)
     or payload->>'group_success_body' is not null)) then
   raise exception 'Legacy booking success group policy changed';
  end if;
  raise exception using errcode='ZX001',message='Rollback synthetic policy assertion';
 exception when sqlstate 'ZX001' then null;
 end;
 if exists(select 1 from public.lsa_v32_delivery_jobs where message_key=k) then
  raise exception 'Synthetic policy record survived rollback';
 end if;
end $test$;
