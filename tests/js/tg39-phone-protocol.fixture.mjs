function sha256(text){
 const data=Buffer.from(text,'utf8'),n=data.length,b=new Uint8Array(Math.ceil((n+9)/64)*64);b.set(data);b[n]=128;const bits=n*8;for(let i=0;i<8;i++)b[b.length-1-i]=Math.floor(bits/2**(8*i))&255;
 const H=[0x6a09e667,0xbb67ae85,0x3c6ef372,0xa54ff53a,0x510e527f,0x9b05688c,0x1f83d9ab,0x5be0cd19],K=[0x428a2f98,0x71374491,0xb5c0fbcf,0xe9b5dba5,0x3956c25b,0x59f111f1,0x923f82a4,0xab1c5ed5,0xd807aa98,0x12835b01,0x243185be,0x550c7dc3,0x72be5d74,0x80deb1fe,0x9bdc06a7,0xc19bf174,0xe49b69c1,0xefbe4786,0x0fc19dc6,0x240ca1cc,0x2de92c6f,0x4a7484aa,0x5cb0a9dc,0x76f988da,0x983e5152,0xa831c66d,0xb00327c8,0xbf597fc7,0xc6e00bf3,0xd5a79147,0x06ca6351,0x14292967,0x27b70a85,0x2e1b2138,0x4d2c6dfc,0x53380d13,0x650a7354,0x766a0abb,0x81c2c92e,0x92722c85,0xa2bfe8a1,0xa81a664b,0xc24b8b70,0xc76c51a3,0xd192e819,0xd6990624,0xf40e3585,0x106aa070,0x19a4c116,0x1e376c08,0x2748774c,0x34b0bcb5,0x391c0cb3,0x4ed8aa4a,0x5b9cca4f,0x682e6ff3,0x748f82ee,0x78a5636f,0x84c87814,0x8cc70208,0x90befffa,0xa4506ceb,0xbef9a3f7,0xc67178f2],R=(x,n)=>(x>>>n)|(x<<(32-n));
 for(let i=0;i<b.length;i+=64){const w=new Uint32Array(64);for(let j=0;j<16;j++)w[j]=(b[i+j*4]<<24)|(b[i+j*4+1]<<16)|(b[i+j*4+2]<<8)|b[i+j*4+3];for(let j=16;j<64;j++)w[j]=(w[j-16]+(R(w[j-15],7)^R(w[j-15],18)^(w[j-15]>>>3))+w[j-7]+(R(w[j-2],17)^R(w[j-2],19)^(w[j-2]>>>10)))>>>0;let[a,c,d,e,f,g,h,z]=H;for(let j=0;j<64;j++){const t1=(z+(R(f,6)^R(f,11)^R(f,25))+((f&g)^(~f&h))+K[j]+w[j])>>>0,t2=((R(a,2)^R(a,13)^R(a,22))+((a&c)^(a&d)^(c&d)))>>>0;z=h;h=g;g=f;f=(e+t1)>>>0;e=d;d=c;c=a;a=(t1+t2)>>>0;}[a,c,d,e,f,g,h,z].forEach((v,j)=>H[j]=(H[j]+v)>>>0);}
 return H.map(v=>v.toString(16).padStart(8,'0')).join('');
}
// Included inside the Code nodes. Requires the existing pure sha256 function; no network or durable writes.
const PC_GROUP='-5525037012',PC_LOGO='eb4d7a6eb5d8bff20c8a187798f6c6e3354db34e1e6cc5869a47a4134d66c7e7';
const PC_META=['job_id','source_kind','source_id','source_revision','job_key','reference_id','phone_hash','content_sha256'];
function pcEscape(v){return String(v).replace(/&/g,'&amp;').replace(/</g,'&lt;').replace(/>/g,'&gt;').replace(/"/g,'&quot;');}
function pcPlain(v){
 if(/<(?!\/?b>|br\s*\/?>)[^>]*>/i.test(v))throw Error('PHONE_TEMPLATE_TAG_UNSUPPORTED');
 return v.replace(/<br\s*\/?>/gi,'\n').replace(/<\/?b>/gi,'*').replace(/&#(?:x([a-f0-9]+)|([0-9]+));|&(amp|lt|gt|quot|apos|#39);/gi,(s,h,n,e)=>{if(h||n){const c=parseInt(h||n,h?16:10);if(c<1||c>0x10ffff||(c>=0xd800&&c<=0xdfff))throw Error('PHONE_TEMPLATE_ENTITY_INVALID');return String.fromCodePoint(c);}return {amp:'&',lt:'<',gt:'>',quot:'"',apos:"'",'#39':"'"}[e.toLowerCase()];});
}
function pcScope(p,full=false){const s={contract:'tg39_phone_code_scope_v1'};for(const k of PC_META)s[k]=p[k];if(full)for(const k of ['language','purpose','asset_sha256','not_before','expires_at'])s[k]=p[k];return s;}
function pcValidatePacket(p,dispatch,now=Date.now()){
 const uuid=/^[a-f0-9]{8}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{4}-[a-f0-9]{12}$/;
 if(!p||p.contract!=='tg39_phone_code_packet_v1'||!uuid.test(p.job_id??'')||p.source_kind!=='phone_code'||p.source_id!==p.job_id||p.job_key!=='site01otp:'+p.job_id||p.reference_id!=='site01otp-'+p.job_id||['source_revision','phone_hash','content_sha256'].some(k=>!/^[a-f0-9]{64}$/.test(p[k]??'')))throw Error('PHONE_PACKET_SCOPE_INVALID');
 const keys=['contract',...PC_META,...(dispatch?['language','purpose','method','caption_html','parse_mode','asset_sha256','asset_mime','asset_base64','guest_phone_e164','not_before','expires_at','content_nonce']:[])];
 if(Object.keys(p).length!==keys.length||Object.keys(p).some(k=>!keys.includes(k)))throw Error('PHONE_PACKET_FIELDS_INVALID');
 if(!dispatch)return pcScope(p);
 const start=Date.parse(p.not_before),end=Date.parse(p.expires_at),phone=String(p.guest_phone_e164??'').replace(/^\+/,'');
 if(!['ar','en','fr'].includes(p.language)||!['phone_bind','phone_login'].includes(p.purpose)||p.method!=='photo'||p.parse_mode!=='HTML'||p.asset_mime!=='image/png'||p.asset_sha256!==PC_LOGO||typeof p.caption_html!=='string'||!p.caption_html||p.caption_html.length>2400||!/^[a-f0-9]{64}$/.test(p.content_nonce??'')||!Number.isFinite(start)||start>now+5000||!Number.isFinite(end)||end<=now||end>start+300000||!/^[1-9][0-9]{6,14}$/.test(phone)||sha256(phone)!==p.phone_hash)throw Error('PHONE_PACKET_PAYLOAD_INVALID');
 if(typeof p.asset_base64!=='string'||p.asset_base64.length!==43464||!/^[A-Za-z0-9+/]+={0,2}$/.test(p.asset_base64))throw Error('PHONE_APPROVED_LOGO_INVALID');
 const b=Buffer.from(p.asset_base64,'base64');if(b.length!==32598||b.toString('base64')!==p.asset_base64||b.subarray(0,8).toString('hex')!=='89504e470d0a1a0a'||sha256(b)!==PC_LOGO)throw Error('PHONE_APPROVED_LOGO_INVALID');
 if(sha256(JSON.stringify([p.language,'HTML','photo',p.caption_html,p.asset_sha256,p.content_nonce]))!==p.content_sha256)throw Error('PHONE_CONTENT_HASH_INVALID');
 pcPlain(p.caption_html);return pcScope(p,true);
}
function pcPlan(p,now=Date.now()){
 const scope=pcValidatePacket(p,true,now),phone=p.guest_phone_e164.replace(/^\+/,''),label=p.purpose==='phone_bind'?'توثيق رقم الجوال':'رمز تسجيل الدخول';
 const photo='<b>Luxury Living 🌿</b>\n'+label+'\nمرجع الإشعار: '+pcEscape(p.reference_id)+'\nالنص الخاص بالضيف يتبع هذه الصورة.';
 const expiry=new Date(p.expires_at).toLocaleTimeString('en-GB',{timeZone:'Asia/Riyadh',hour:'2-digit',minute:'2-digit',second:'2-digit'});
 const text='<b>🔐 '+label+' — إرسال يدوي عبر واتساب</b>\n📌 مرجع الإشعار: '+pcEscape(p.reference_id)+'\n📞 الجوال: '+phone+'\n<a href="https://wa.me/'+phone+'">💬 فتح واتساب مع صاحب الرقم</a>\n⏰ تنتهي صلاحية الرمز: '+expiry+' بتوقيت الرياض\n<b>أرسل النص إلى صاحب الرقم فقط. وصوله للقروب لا يوثق الهاتف ولا يسجل الدخول:</b>\n<pre>'+pcEscape(pcPlain(p.caption_html))+'</pre>';
 if(text.length>4000||photo.length>1024)throw Error('PHONE_COPY_OVERSIZE');
 const parts=[{part_index:1,method:'photo',html:photo},{part_index:2,method:'text',html:text}];
 for(const part of parts)part.content_sha256=sha256(JSON.stringify([PC_GROUP,'HTML',part.method,part.html,part.method==='photo'?PC_LOGO:'',p.content_nonce]));
 return {scope,parts};
}
function pcAuthority(response,request,now=Date.now()){
 const a=response?.body??response,end=Date.parse(a?.expires_at??'');
 if(response?.error||Number(response?.statusCode??200)!==200||a?.contract!=='tg39_activation_authority_v1'||a.authorized!==true||a.request_id!==request.request_id||a.action!==request.action||!Number.isFinite(end)||end<=now||end>now+125000||a.source_scope!=='phone_code_handoff'||a.dispatch_mode!=='workgroup_manual'||a.workgroup_id!==PC_GROUP)return {authorized:false,http:403,error:'phone_authority_denied'};
 try{const dispatch=request.action==='phone_code_handoff';pcValidatePacket(a.packet,dispatch,now);return {authorized:true,authority:{contract:a.contract,authorized:true,request_id:a.request_id,action:a.action,expires_at:a.expires_at,source_scope:a.source_scope,dispatch_mode:a.dispatch_mode,workgroup_id:PC_GROUP,packet:a.packet}};}catch{return {authorized:false,http:403,error:'phone_packet_denied'};}
}

export {sha256,pcScope,pcPlan,pcValidatePacket,pcAuthority};
