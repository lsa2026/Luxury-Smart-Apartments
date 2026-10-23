function evidence(job,response) {
  const data=response.body??response;
  let providerId='',delivered=false;
  if(job.action==='send'){
    if(data&&['true','1','yes'].includes(String(data.sent).toLowerCase()))providerId=String(data.id||'');
  }else{
    const rows=Array.isArray(data.messages)?data.messages:[];
    for(const row of rows){
      if((job.provider_message_id&&String(row.id)===String(job.provider_message_id))||row.referenceId===job.reference_id){
        providerId=String(row.id||'');
        const ack=String(row.ack??'').toLowerCase(),status=String(row.status??'').toLowerCase();
        delivered=(/^\d+$/.test(ack)&&Number(ack)>=2)||['device','delivered','read','played'].includes(ack)||['delivered','read','played'].includes(status);
        if(delivered)break;
      }
    }
  }
  return {provider_id:providerId,delivered,error:response.error?'provider_result_uncertain':data?.error||null};
}
if(typeof module!=='undefined')module.exports={evidence};
