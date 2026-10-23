function render(input) {
  const g=input.booking,p=input.previous||{},a=input.apartment||{},now=new Date(input.now),end=new Date(g.ends_at);
  if(g.source!=='morning07'||g.payment_verified!==true||!['paid','cancelled'].includes(g.status)
    ||!/^\+?[1-9]\d{7,14}$/.test(g.phone||'')||!Number.isFinite(end.getTime())||!Number.isFinite(now.getTime()))throw Error('Invalid verified Morning07 booking');
  const lang=['ar','en','fr'].includes(g.guest_language)?g.guest_language:'ar';
  const phone=String(g.phone).replace(/\D/g,'');
  const kind=g.status==='cancelled'?'cancelled':Object.keys(p).length?'modified':'new';
  const labels={ar:{new:'تأكيد الحجز الصباحي',modified:'تعديل الحجز الصباحي',cancelled:'إلغاء الحجز الصباحي',checkout:'تذكير بموعد الخروج',ref:'المرجع',end:'موعد الخروج'},en:{new:'Morning stay confirmed',modified:'Morning stay updated',cancelled:'Morning stay cancelled',checkout:'Morning stay check-out reminder',ref:'Reference',end:'Check-out'},fr:{new:'Séjour matinal confirmé',modified:'Séjour matinal modifié',cancelled:'Séjour matinal annulé',checkout:'Rappel du départ',ref:'Référence',end:'Départ'}}[lang];
  const header=[g.guest_name,g.listing_name,g.arrival_date,g.check_in_time+' → '+g.check_out_time,g.timezone].join('\n');
  const short='AM-'+g.morning_stay_id.slice(0,8);
  const status=[labels[kind],header,labels.ref+': '+short,labels.end+': '+g.check_out_time].join('\n\n');
  const jobs=[];
  if(g.status==='paid'&&end<=now)return {stay_id:g.morning_stay_id,revision:g.source_revision,jobs};
  const key=(suffix)=>'lsa07:'+g.morning_stay_id+':'+suffix;
  function add(type,body,suffix,sequence,recipient='guest',due=now.toISOString(),until=null){
    if(!body||body.length>4096)throw Error('Morning07 message exceeds the provider text limit');
    jobs.push({kind:type,body,job_key:key(suffix),sequence,recipient_kind:recipient,due_at:due,eligible_until:until});
  }
  add('status_'+kind,status,'r'+g.source_revision+':status',1,'guest',now.toISOString(),kind==='cancelled'?null:g.ends_at);
  const operational=['☀️ '+labels[kind],g.listing_name,'الضيف: '+g.guest_name,'الجوال: +'+phone,
    'الفترة: '+g.arrival_date+' | '+g.check_in_time+' → '+g.check_out_time+' | '+g.timezone,
    'المرجع: '+short,'إدارة الحجز: '+g.management_url].join('\n');
  add('group_notice',operational,'r'+g.source_revision+':group',0,'group',now.toISOString(),kind==='cancelled'?null:g.ends_at);
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
if(typeof module!=='undefined')module.exports={render};
