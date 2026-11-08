// Pure functions. No network, filesystem, credentials, or production clients.
function admitted(d) {
  if (!d || d.source !== 'morning07') return false;
  const v = d.contract_version === undefined ? 1 : d.contract_version;
  if (v === 1) return d.payment_verified === true && ['paid','cancelled'].includes(d.status);
  if (v !== 2 || d.booking_verified !== true ||
      !Number.isSafeInteger(d.hostaway_reservation_id) || d.hostaway_reservation_id <= 0 ||
      !['confirmed','cancelled'].includes(d.status)) return false;
  return (d.payment_status === 'unpaid' && d.payment_verified === false) ||
         (d.payment_status === 'paid' && d.payment_verified === true);
}

function paidEvidence(d) {
  return admitted(d) && d.status !== 'cancelled' && d.payment_verified === true &&
    ((d.contract_version === undefined || d.contract_version === 1) || d.payment_status === 'paid');
}

function validPayload(d) {
  const uuid = /^[0-9a-f]{8}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{4}-[0-9a-f]{12}$/;
  const date = /^\d{4}-\d{2}-\d{2}$/;
  const clock = /^(?:[01]\d|2[0-3]):[0-5]\d$/;
  const instant = /^\d{4}-\d{2}-\d{2}T\d{2}:\d{2}:\d{2}(?:\.\d+)?(?:Z|[+-]\d{2}:\d{2})$/;
  if (!admitted(d) || !uuid.test(d.morning_stay_id || '') || !uuid.test(d.source_event_id || '') ||
      !Number.isSafeInteger(d.source_revision) || d.source_revision < 1 ||
      !Number.isSafeInteger(d.listing_map_id) || d.listing_map_id < 1 ||
      !/^\+?[1-9]\d{7,14}$/.test(d.phone || '') ||
      !/^[0-9a-f]{64}$/.test(d.state_fingerprint || '') ||
      typeof d.delivery_token !== 'string' || d.delivery_token.length < 40 || d.delivery_token.length > 300 ||
      !date.test(d.arrival_date || '') || d.arrival_date !== d.departure_date ||
      !['Asia/Riyadh','Africa/Casablanca'].includes(d.timezone) ||
      !clock.test(d.check_in_time || '') || !clock.test(d.check_out_time || '') ||
      !(d.check_in_time > '00:00' && d.check_in_time < d.check_out_time && d.check_out_time <= '12:00') ||
      !instant.test(d.starts_at || '') || !instant.test(d.ends_at || '')) return false;
  const start = Date.parse(d.starts_at), end = Date.parse(d.ends_at);
  if (!Number.isFinite(start) || !Number.isFinite(end) || !(start < end) || end-start >= 43200000) return false;
  try {
    const at = value => {
      const parts = new Intl.DateTimeFormat('en-GB', {timeZone:d.timezone,year:'numeric',month:'2-digit',
        day:'2-digit',hour:'2-digit',minute:'2-digit',hourCycle:'h23'}).formatToParts(new Date(value));
      const p = Object.fromEntries(parts.map(x=>[x.type,x.value]));
      return {date:p.year+'-'+p.month+'-'+p.day,time:p.hour+':'+p.minute};
    };
    const a=at(start), b=at(end);
    return a.date===d.arrival_date && b.date===d.departure_date &&
      a.time===d.check_in_time && b.time===d.check_out_time;
  } catch { return false; }
}

function operationalState(d) {
  // Financial fields, tokens, fingerprints, and source revision are deliberately excluded.
  // A defensive payment-only revision must not resend booking confirmations.
  return JSON.stringify({
    status:d.status==='paid'?'confirmed':d.status,
    listing_map_id:d.listing_map_id, listing_name:d.listing_name,
    starts_at:d.starts_at, ends_at:d.ends_at, timezone:d.timezone,
    arrival_date:d.arrival_date, departure_date:d.departure_date,
    check_in_time:d.check_in_time, check_out_time:d.check_out_time,
    phone:String(d.phone||'').replace(/\D/g,''), guest_name:d.guest_name,
    guest_email:d.guest_email, guest_language:d.guest_language,
    guests:d.guests, total_price:d.total_price, currency:d.currency
  });
}


function render(input) {
  const g=input.booking,p=input.previous||{},a=input.apartment||{},now=new Date(input.now),end=new Date(g.ends_at);
  if(!admitted(g)
    ||!/^\+?[1-9]\d{7,14}$/.test(g.phone||'')||!Number.isFinite(end.getTime())||!Number.isFinite(now.getTime()))throw Error('Invalid verified Morning07 booking');
  const lang=['ar','en','fr'].includes(g.guest_language)?g.guest_language:'ar';
  const phone=String(g.phone).replace(/\D/g,'');
  const kind=g.status==='cancelled'?'cancelled':Object.keys(p).length?'modified':'new';
  const labels={ar:{new:'تأكيد الحجز الصباحي',modified:'تعديل الحجز الصباحي',cancelled:'إلغاء الحجز الصباحي',checkout:'تذكير بموعد الخروج',ref:'المرجع',end:'موعد الخروج'},en:{new:'Morning stay confirmed',modified:'Morning stay updated',cancelled:'Morning stay cancelled',checkout:'Morning stay check-out reminder',ref:'Reference',end:'Check-out'},fr:{new:'Séjour matinal confirmé',modified:'Séjour matinal modifié',cancelled:'Séjour matinal annulé',checkout:'Rappel du départ',ref:'Référence',end:'Départ'}}[lang];
  const header=[g.guest_name,g.listing_name,g.arrival_date,g.check_in_time+' → '+g.check_out_time,g.timezone].join('\n');
  const short='AM-'+g.morning_stay_id.slice(0,8);
  const paymentText={ar:paidEvidence(g)?'حالة السداد: مسدد':'حالة السداد: غير مسدد',en:paidEvidence(g)?'Payment: paid':'Payment: unpaid',fr:paidEvidence(g)?'Paiement : réglé':'Paiement : non réglé'}[lang];
  const status=[labels[kind],header,labels.ref+': '+short,labels.end+': '+g.check_out_time,paymentText].join('\n\n');
  const operationalChange=!Object.keys(p).length||operationalState(g)!==operationalState(p);
  const jobs=[];
  if(g.status!=='cancelled'&&end<=now)return {stay_id:g.morning_stay_id,revision:g.source_revision,jobs};
  const key=(suffix)=>'lsa07:'+g.morning_stay_id+':'+suffix;
  function add(type,body,suffix,sequence,recipient='guest',due=now.toISOString(),until=null){
    if(!body||body.length>4096)throw Error('Morning07 message exceeds the provider text limit');
    jobs.push({kind:type,body,job_key:key(suffix),sequence,recipient_kind:recipient,due_at:due,eligible_until:until});
  }
  if(operationalChange)add('status_'+kind,status,'r'+g.source_revision+':status',1,'guest',now.toISOString(),kind==='cancelled'?null:g.ends_at);
  const operational=['☀️ '+labels[kind],g.listing_name,'الضيف: '+g.guest_name,'الجوال: +'+phone,
    'الفترة: '+g.arrival_date+' | '+g.check_in_time+' → '+g.check_out_time+' | '+g.timezone,
    'المرجع: '+short,paymentText,'إدارة الحجز: '+g.management_url].join('\n');
  if(operationalChange)add('group_notice',operational,'r'+g.source_revision+':group',0,'group',now.toISOString(),kind==='cancelled'?null:g.ends_at);
  const cancelled=g.status==='cancelled',future=end>now;
  const phoneChanged=String(p.phone||'').replace(/\D/g,'')!==phone;
  const newBooking=!Object.keys(p).length;
  function fill(name){
    const t=input.templates.find(x=>x.template_name===name);
    if(!t)throw Error('Morning07 template missing: '+name);
    const replacements={...a,...g,address_fr:a.address_fr||a.address_en};
    let text=String(t['content_'+lang]||t.content_en||'').replace(/\{\{\s*(\w+)\s*\}\}/g,(_m,k)=>{
      if(!(k in replacements))throw Error('Unknown Morning07 template field '+k);
      return String(replacements[k]??'');
    });
    const promises=[
      ['سيتم إرسال كود الشقة الخاص بك مباشرة بعد التحقق من هويتك عبر البوابة.','للحصول على كود الشقة، تواصل مع فريق الدعم بعد التحقق من الهوية: +966501205651.'],
      ['Your unique apartment code will be sent immediately after ID verification via the portal.','Contact support after ID verification for your apartment access code: +966501205651.'],
      ['Votre code personnel vous sera envoyé immédiatement après la vérification de votre pièce d’identité via le portail.','Après vérification de votre identité, contactez notre équipe pour le code : +966501205651.']
    ];
    for(const[from,to]of promises)text=text.split(from).join(to);
    text=text.replace(/<br\s*\/?>/gi,'\n').replace(/<\/(?:p|div)>/gi,'\n').replace(/<b>(.*?)<\/b>/gis,'*$1*').replace(/<[^>]*>/g,'').replace(/&amp;/g,'&').replace(/&lt;/g,'<').replace(/&gt;/g,'>');
    return header+'\n\n'+text;
  }
  if(!cancelled&&future&&(newBooking||phoneChanged))add('house_rules',fill('house_rules'),'rules:'+phone,2,'guest',now.toISOString(),g.ends_at);
  if(!cancelled&&future&&(newBooking||phoneChanged||p.listing_map_id!==g.listing_map_id||p.starts_at!==g.starts_at))
    add('checkin_guide',fill('checkin_guide'),'guide:'+g.listing_map_id+':'+Date.parse(g.starts_at)+':'+phone,3,'guest',now.toISOString(),g.ends_at);
  const due=new Date(end.getTime()-3600000);
  if(!cancelled&&due>now)add('reminder_checkout',labels.checkout+'\n\n'+header+'\n\n'+labels.end+': '+g.check_out_time,
    'checkout:'+g.listing_map_id+':'+end.getTime()+':'+phone,4,'guest',due.toISOString(),g.ends_at);
  return {stay_id:g.morning_stay_id,revision:g.source_revision,jobs};
}

module.exports={render};
