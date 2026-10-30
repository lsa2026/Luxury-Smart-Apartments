'use strict';
const test=require('node:test');const assert=require('node:assert/strict');const vm=require('node:vm');const fs=require('node:fs');const path=require('node:path');
const code=fs.readFileSync(path.join(__dirname,'../../static/js/loyalty-invitation-credential-copy.js'),'utf8');
function rig(clipboard){
const handlers={};let selected=0;
const input={value:'Bearer synthetic-test-only',type:'password',focus(){},select(){selected++;}};
const copy={addEventListener:(name,fn)=>{handlers.copy=fn;}};
const attrs={};const show={addEventListener:(name,fn)=>{handlers.show=fn;},setAttribute:(k,v)=>{attrs[k]=v;},textContent:''};
const status={textContent:''};const elements={'#loyalty-credential-value':input,'[data-copy-credential]':copy,'[data-show-credential]':show,'[data-copy-status]':status};
vm.runInNewContext(code,{document:{querySelector:()=>({querySelector:key=>elements[key]})},navigator:{clipboard}});
return {handlers,input,status,attrs,selected:()=>selected};}
test('copies only on explicit click and copies the complete authorization value',async()=>{const values=[];const r=rig({writeText:async value=>{values.push(value);}});assert.deepEqual(values,[]);await r.handlers.copy();assert.deepEqual(values,['Bearer synthetic-test-only']);assert.equal(r.input.type,'password');assert.match(r.status.textContent,/تم النسخ/);});
test('clipboard denial selects a manual copy fallback without another request',async()=>{const r=rig({writeText:async()=>{throw Error('denied');}});await r.handlers.copy();assert.equal(r.selected(),1);assert.equal(r.input.type,'text');assert.equal(r.attrs['aria-pressed'],'true');assert.match(r.status.textContent,/يدوي/);});
test('show and hide require a user action and do not copy',()=>{const r=rig();assert.equal(r.input.type,'password');r.handlers.show();assert.equal(r.input.type,'text');r.handlers.show();assert.equal(r.input.type,'password');});
