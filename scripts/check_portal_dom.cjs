/* DOM-only regression: no browser, network, credentials or business writes. Optional jsdom/postcss dev dependencies. */
const fs=require('fs');
const assert=require('assert/strict');
const path=require('path');
const {JSDOM,VirtualConsole}=require(process.env.JSDOM_MODULE || 'jsdom');
const postcss=require(process.env.POSTCSS_MODULE || 'postcss');
const source=path.resolve(__dirname,'..')+path.sep;
const html=fs.readFileSync(source+'app/static/index.html','utf8');
const script=fs.readFileSync(source+'app/static/app.js','utf8');
postcss.parse(fs.readFileSync(source+'app/static/app.css','utf8'));
const errors=[];
const base={id:'T-01234567',reference:'01234567',title:'ERP 一直打不开 <img src=x onerror=alert(1)>',description:'ERP 卡在登录页',requester:'bob',department:'IT',created_at:'2026-10-22T03:30:00Z',updated_at:'2026-10-22T03:31:00Z',severity:'较高',category:'故障报修',status:{code:'pending_review',label:'待支持核查',tone:'amber',group:'processing',title:'需要支持人员进一步核查',message:'自动排查暂未形成可用结论。目前尚未确认问题已解决。',next_action:'请保留相关报错和发生时间。',stage:2},clarification:null,confirmation:null,request_details:[],conversation:[],activity:[{label:'工单已提交',at:'2026-10-22T03:30:00Z'}],can_cancel:false,needs_polling:false};
const flush=()=>new Promise(resolve=>setImmediate(resolve));
async function settle(){for(let i=0;i<8;i++)await flush();}
function boot({role='employee',url='http://127.0.0.1:8080/',ticket=structuredClone(base),handler,restored=false}={}){
 const vc=new VirtualConsole();vc.on('jsdomError',e=>errors.push(e.message));
 const dom=new JSDOM(html,{url,runScripts:'outside-only',virtualConsole:vc,pretendToBeVisual:true});
 const w=dom.window,calls=[];
 w.HTMLDialogElement.prototype.showModal=function(){this.open=true;};w.HTMLDialogElement.prototype.close=function(){this.open=false;};
 let current=structuredClone(ticket),signedIn=restored;const session={user:{user_id:role==='employee'?'bob':'manager',role,department:'IT'},csrf_token:'a'.repeat(64)};
 w.fetch=async(path,options={})=>{
  calls.push({path,options});let status=200,data;
  if(handler){const custom=await handler(path,options,current);if(custom){status=custom.status||200;data=custom.data;if(custom.current)current=custom.current;return {ok:status>=200&&status<300,status,json:async()=>data};}}
  if(path==='/auth/session'){status=signedIn?200:401;data=signedIn?session:{error:'unauthenticated'};}
  else if(path==='/auth/login'){signedIn=true;data=session;}
  else if(path==='/auth/logout'){signedIn=false;status=204;data=null;}
  else if(path.startsWith('/portal/tickets?'))data={items:[current],total:1,counts:{total:1,action_required:current.status.group==='action_required'?1:0,processing:current.status.group==='processing'?1:0,completed:current.status.group==='completed'?1:0}};
  else if(path.startsWith('/portal/tickets/'))data=current;
  else throw new Error('Unexpected endpoint '+path);
  return {ok:status>=200&&status<300,status,json:async()=>structuredClone(data)};
 };
 w.eval(script);
 const id=name=>w.document.getElementById(name);
 const submit=form=>id(form).dispatchEvent(new w.Event('submit',{bubbles:true,cancelable:true}));
 const login=async()=>{await settle();id('username').value=role==='employee'?'bob':'manager';id('password').value='Test-Password-For-UI!';submit('connect');await settle();};
 return {dom,w,id,calls,login,submit};
}
(async()=>{
 // Requester sees human status, escaped user input, no support controls or internals.
 const a=boot();await a.login();
 assert.equal(a.id('login').hidden,true);assert.equal(a.id('nav-support').hidden,true);
 assert.equal(a.id('ticket-list').querySelector('.badge').textContent,'待支持核查');
 assert.match(a.id('detail').textContent,/尚未确认问题已解决/);
 assert.equal(a.id('detail').querySelector('img'),null);assert.match(a.id('detail').textContent,/<img/);
 assert.doesNotMatch(a.id('detail').textContent,/repeated_tool|token|JSON|工具选择|已交付|审批与处理/);
 assert.equal(a.calls.some(x=>x.path.includes('/state')||x.path==='/runtime'),false);assert.equal(a.id('password').value,'');assert.equal(a.calls.some(x=>x.options.headers.Authorization),false);assert(a.calls.every(x=>x.options.credentials==='same-origin'));
 const loginCall=a.calls.find(x=>x.path==='/auth/login');assert.deepEqual(JSON.parse(loginCall.options.body),{username:'bob',password:'Test-Password-For-UI!'});
 a.id('logout').click();await settle();assert.equal(a.id('login').hidden,false);assert.equal(a.id('password').value,'');assert.equal(a.id('ticket-list').textContent,'');a.dom.window.close();
 // Pending clarification is visible and submits the current round with the user's answer.
 const need=structuredClone(base);need.status={...need.status,code:'needs_input',label:'待你补充',group:'action_required',stage:1};need.clarification={question:'请补充具体报错和发生时间。',round_number:2};need.can_cancel=true;
 const b=boot({ticket:need});await b.login();const answer=b.id('detail').querySelector('textarea');assert(answer);answer.value='今天 10 点开始，ERP 提示 503';b.id('detail').querySelector('form').dispatchEvent(new b.w.Event('submit',{bubbles:true,cancelable:true}));await settle();const clarify=b.calls.find(x=>x.path.endsWith('/clarify'));assert.deepEqual(JSON.parse(clarify.options.body),{answer:'今天 10 点开始，ERP 提示 503',round_number:2});b.dom.window.close();
 // Confirmation shows labeled values, never a raw execution plan.
 const confirm=structuredClone(need);confirm.status.code='confirmation';confirm.confirmation=[{label:'申请事项',value:'访问权限申请'},{label:'目标系统',value:'ERP'},{label:'权限范围',value:'普通使用权限'}];
 const c=boot({ticket:confirm});await c.login();assert.match(c.id('detail').textContent,/权限范围/);[...c.id('detail').querySelectorAll('button')].find(x=>x.textContent==='确认申请信息').click();await settle();assert.equal(JSON.parse(c.calls.find(x=>x.path.endsWith('/clarify')).options.body).answer,'确认执行');c.dom.window.close();
 // A role alone does not mix support controls into the personal view; /support is distinct.
 const support=structuredClone(base);support.support={approval_id:'A-1',can_approve:true,can_investigate:false,can_feedback:false,can_resolve:false,can_recover:false,summary:'需要核查客户端日志',next_check:'核查登录失败时间',observations:[{source:'客户端记录',records:[]}],feedback:[]};
 const d=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:support});await d.login();assert.equal(d.id('nav-support').hidden,false);assert.equal(d.id('page-title').textContent,'审批与处理');assert.match(d.id('detail').textContent,/同意申请/);d.id('nav-mine').click();await settle();assert.equal(d.id('page-title').textContent,'我的工单');assert.doesNotMatch(d.id('detail').textContent,/同意申请|支持人员处理区/);d.dom.window.close();
 // Failed submit keeps body + idempotency key for a retry and unlocks only after success.
 let attempt=0;
 const e=boot({handler:(path,opts)=>{if(path==='/portal/tickets'&&opts.method==='POST'){attempt++;if(attempt===1)return {status:503,data:{detail:'INTERNAL SHOULD NOT BE DISPLAYED'}};return {status:201,data:base};}}});await e.login();e.id('new-ticket').click();e.id('title').value='ERP 打不开';e.id('description').value='今天 10 点开始卡住';e.submit('create-form');await settle();assert.equal(e.id('title').disabled,true);assert.equal(e.id('create-dialog').open,true);assert.doesNotMatch(e.id('create-error').textContent,/INTERNAL/);e.submit('create-form');await settle();const creates=e.calls.filter(x=>x.path==='/portal/tickets');assert.equal(creates.length,2);assert.equal(creates[0].options.headers['Idempotency-Key'],creates[1].options.headers['Idempotency-Key']);assert.equal(creates[0].options.body,creates[1].options.body);assert.equal(e.id('create-dialog').open,false);assert.equal(e.id('title').disabled,false);e.dom.window.close();
 // Stale detail responses cannot overwrite a new login.
 let release;let delay=false;
 const f=boot({handler:async(path)=>{if(delay&&path.startsWith('/portal/tickets/T-')){await new Promise(resolve=>release=resolve);return {data:base};}}});await f.login();delay=true;f.id('ticket-list').querySelector('button').click();await settle();f.id('logout').click();release();await settle();assert.equal(f.id('workspace').hidden,true);assert.doesNotMatch(f.id('detail').textContent,/ERP 一直打不开/);f.dom.window.close();
 // Cookie-backed refresh restores the session without asking for a password again.
 const g=boot({restored:true});await settle();assert.equal(g.id('workspace').hidden,false);assert.equal(g.calls.some(x=>x.path==='/auth/login'),false);g.dom.window.close();
 // Login failures show a friendly error and discard the typed password.
 const h=boot({handler:(path)=>path==='/auth/login'?{status:401,data:{detail:'private-auth-error'}}:null});await h.login();assert.equal(h.id('login').hidden,false);assert.match(h.id('login-error').textContent,/账号或密码不正确/);assert.equal(h.id('password').value,'');h.dom.window.close();
 // Expired sessions return to the login view and discard the previous user's data.
 let expire=false;const j=boot({handler:(path)=>expire&&path.startsWith('/portal/tickets/')?{status:401,data:{error:'expired'}}:null});await j.login();expire=true;j.id('refresh').click();await settle();assert.equal(j.id('workspace').hidden,true);assert.match(j.id('login-error').textContent,/登录已过期/);j.dom.window.close();
 // A support agent claims a real actionable case before submitting a result.
 let claimed=false;
 const caseTicket=structuredClone(base);caseTicket.handling={team:'应用支持',assignee:null,due_at:'2026-10-23T10:00:00Z',overdue:false};caseTicket.can_follow_up=true;caseTicket.case_revision=4;
 caseTicket.support={can_claim:true,assignment_revision:0,is_assignee:false,can_approve:false,can_investigate:false,can_feedback:false,can_resolve:false,can_recover:false,can_transfer:false,can_request_info:false,observations:[],feedback:[],assignees:[{user_id:'manager',role:'manager'},{user_id:'admin',role:'admin'}]};
 const k=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:caseTicket,handler:(path,opts,current)=>{if(path.endsWith('/claim')){claimed=true;const next=structuredClone(current);next.handling.assignee='manager';next.support={...next.support,can_claim:false,is_assignee:true,assignment_revision:1,can_resolve:true,can_request_info:true,can_transfer:true};next.status={...next.status,code:'in_progress',label:'支持处理中',tone:'blue'};return {data:next,current:next};}if(path.endsWith('/request-info')){const next=structuredClone(current);next.clarification={question:'问题是什么时候开始的？',stage:'investigation'};next.status={...next.status,code:'needs_input',label:'待你补充',group:'action_required'};next.support={...next.support,can_request_info:false,can_resolve:false,assignment_revision:2};return {data:next,current:next};}}});await k.login();const claimButton=[...k.id('detail').querySelectorAll('button')].find(x=>x.textContent==='认领并开始处理');assert(claimButton);assert.doesNotMatch(k.id('detail').textContent,/确认人工结案/);claimButton.click();await settle();assert(claimed);assert.match(k.id('detail').textContent,/确认人工结案/);assert.deepEqual(JSON.parse(k.calls.find(x=>x.path.endsWith('/claim')).options.body),{revision:0});
 const infoButton=[...k.id('detail').querySelectorAll('button')].find(x=>x.textContent==='请求用户补充');infoButton.closest('form').dispatchEvent(new k.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.deepEqual(JSON.parse(k.calls.find(x=>x.path.endsWith('/request-info')).options.body),{field:'occurred_at',revision:1});k.dom.window.close();
 // Investigation answers use a case revision and never resubmit the original action.
 const userCase=structuredClone(caseTicket);delete userCase.support;userCase.clarification={question:'请说明故障开始时间',stage:'investigation'};userCase.status={...userCase.status,code:'needs_input',label:'待你补充',group:'action_required'};
 const m=boot({ticket:userCase});await m.login();const follow=m.id('detail').querySelector('textarea');follow.value='今天上午10点开始';follow.closest('form').dispatchEvent(new m.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.deepEqual(JSON.parse(m.calls.find(x=>x.path.endsWith('/follow-up')).options.body),{answer:'今天上午10点开始',revision:4});assert.equal(m.calls.some(x=>x.path.endsWith('/clarify')),false);m.dom.window.close();
 // Staff queue selection is sent to the server and reset in the requester view.
 const n=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:caseTicket});await n.login();n.id('support-queue').value='unassigned';n.id('support-queue').dispatchEvent(new n.w.Event('change',{bubbles:true}));await settle();assert(n.calls.some(x=>x.path.includes('queue=unassigned')));n.id('nav-mine').click();await settle();assert.equal(n.id('support-queue-row').hidden,true);assert.equal(n.calls.filter(x=>x.path.startsWith('/portal/tickets?')).at(-1).path.includes('queue='),false);n.dom.window.close();
 // Assigned staff queue a bounded retry with the current case revision.
 const failedCase=structuredClone(caseTicket);failedCase.support={...failedCase.support,can_claim:false,is_assignee:true,can_retry_investigation:true,worker_issue:{message:'自动排查超过处理时限，已交由支持人员跟进。',remaining_runs:2,escalated:false}};
 const p=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:failedCase});await p.login();const retryButton=[...p.id('detail').querySelectorAll('button')].find(x=>x.textContent==='重新排查');assert(retryButton);assert.match(p.id('detail').textContent,/剩余排查次数：2/);retryButton.click();await settle();assert.deepEqual(JSON.parse(p.calls.find(x=>x.path.endsWith('/retry-investigation')).options.body),{revision:4});assert.equal(p.calls.some(x=>x.path.endsWith('/investigate')),false);p.dom.window.close();
 // Exhaustion stays actionable for human handling and offers no further retry.
 const exhausted=structuredClone(failedCase);exhausted.support.can_retry_investigation=false;exhausted.support.worker_issue={...exhausted.support.worker_issue,remaining_runs:0,escalated:true};const q=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:exhausted});await q.login();assert.match(q.id('detail').textContent,/已达到重新排查上限/);assert.equal([...q.id('detail').querySelectorAll('button')].some(x=>x.textContent==='重新排查'),false);q.dom.window.close();
 // Applicant confirmation and recurrence use the same case with fresh versions.
 const pendingVerification=structuredClone(base);pendingVerification.case_revision=8;pendingVerification.verification={can_confirm:true,can_reopen:false};pendingVerification.waiting={kind:'verification',expires_at:'2030-01-01T00:00:00Z',reminded_at:null};pendingVerification.status={...base.status,code:'awaiting_verification',label:'待你验证',group:'action_required'};
 const r=boot({ticket:pendingVerification,handler:(path,options,current)=>{if(path.endsWith('/verify')){const body=JSON.parse(options.body),next=structuredClone(current);next.case_revision++;next.waiting=null;next.verification={can_confirm:false,can_reopen:body.verdict==='confirmed',reopen_until:'2030-01-01T00:00:00Z'};next.status={...next.status,code:body.verdict==='confirmed'?'resolved':'in_progress',label:body.verdict==='confirmed'?'已解决':'支持处理中',group:body.verdict==='confirmed'?'completed':'processing'};return {data:next,current:next};}}});await r.login();let submitVerification=[...r.id('detail').querySelectorAll('button')].find(x=>x.textContent==='提交验证结果');submitVerification.closest('form').dispatchEvent(new r.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.deepEqual(JSON.parse(r.calls.find(x=>x.path.endsWith('/verify')).options.body),{revision:8,verdict:'confirmed',note:''});const reopen=[...r.id('detail').querySelectorAll('button')].find(x=>x.textContent==='重新打开工单');assert(reopen);reopen.closest('form').querySelector('textarea').value='今天再次报错503';reopen.closest('form').dispatchEvent(new r.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.deepEqual(JSON.parse(r.calls.filter(x=>x.path.endsWith('/verify')).at(-1).options.body),{revision:9,verdict:'recurred',note:'今天再次报错503'});assert.equal(r.calls.some(x=>x.path==='/portal/tickets'&&x.options.method==='POST'),false);r.dom.window.close();
 // Failure feedback needs a note and keeps its observed case revision.
 const s=boot({ticket:pendingVerification});await s.login();const vf=[...s.id('detail').querySelectorAll('button')].find(x=>x.textContent==='提交验证结果').closest('form');vf.querySelector('select').value='still_failing';vf.querySelector('select').dispatchEvent(new s.w.Event('change'));vf.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.equal(s.calls.some(x=>x.path.endsWith('/verify')),false);vf.querySelector('textarea').value='实际操作仍然失败';vf.dispatchEvent(new s.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.equal(JSON.parse(s.calls.find(x=>x.path.endsWith('/verify')).options.body).verdict,'still_failing');s.dom.window.close();
 // Waiting applicants can withdraw using the version captured when the dialog opened.
 const waitingCase=structuredClone(userCase);waitingCase.case_revision=9;waitingCase.can_cancel=true;waitingCase.waiting={kind:'user_info',expires_at:'2030-01-01T00:00:00Z',reminded_at:'2026-10-27T00:00:00Z'};const t=boot({ticket:waitingCase});await t.login();assert.match(t.id('detail').textContent,/补充信息期限/);[...t.id('detail').querySelectorAll('button')].find(x=>x.textContent==='撤销工单').click();t.id('cancel-reason').value='不再需要';t.submit('cancel-form');await settle();assert.deepEqual(JSON.parse(t.calls.find(x=>x.path.endsWith('/cancel')).options.body),{reason:'不再需要',case_revision:9});t.dom.window.close();
 // Staff feedback and archive commands include the version shown on the page.
 const staffCase=structuredClone(caseTicket);staffCase.case_revision=12;staffCase.support={...staffCase.support,can_claim:false,can_feedback:true,can_resolve:true,feedback_revision:1};const u=boot({role:'manager',url:'http://127.0.0.1:8080/support',ticket:staffCase,handler:(path,options,current)=>path.startsWith('/tickets/')?{data:current}:null});await u.login();const af=[...u.id('detail').querySelectorAll('button')].find(x=>x.textContent==='确认人工结案').closest('form');af.querySelector('select').value='archived';af.querySelector('textarea').value='联系不上用户，未确认解决';af.dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await settle();const archiveBody=JSON.parse(u.calls.find(x=>x.path.endsWith('/resolve')).options.body);assert.equal(archiveBody.case_revision,12);assert.equal(archiveBody.outcome,'archived');const ff=[...u.id('detail').querySelectorAll('button')].find(x=>x.textContent==='提交核查反馈').closest('form');ff.querySelector('textarea').value='已核查本版报告';ff.querySelector('input').value='record-1';ff.dispatchEvent(new u.w.Event('submit',{bubbles:true,cancelable:true}));await settle();assert.equal(JSON.parse(u.calls.find(x=>x.path.endsWith('/feedback')).options.body).case_revision,12);u.dom.window.close();
 // Dedicated support-role accounts can enter the handler workspace and claim a case.
 const v=boot({role:'support',url:'http://127.0.0.1:8080/support',ticket:caseTicket});await v.login();assert.equal(v.id('nav-support').hidden,false);assert(v.calls.some(x=>x.path.includes('scope=support')));assert([...v.id('detail').querySelectorAll('button')].some(x=>x.textContent==='认领并开始处理'));v.dom.window.close();
 assert.deepEqual(errors,[]);
 console.log('PASS: CSS parse + 19 DOM flows, including dedicated support accounts, verification, recurrence and versioned staff results.');
})().catch(error=>{console.error(error);process.exitCode=1;});
