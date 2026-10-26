"use strict";
(() => {
  const $ = id => document.getElementById(id);
  const paths = {
    tickets:'M8 3h8l4 4v14H4V3h4M8 3v5h8V3M8 12h8M8 16h5',
    plus:'M12 5v14M5 12h14', shield:'M12 3l8 3v6c0 5-8 9-8 9s-8-4-8-9V6l8-3M8 12l3 3 5-6',
    help:'M9 9a3 3 0 1 1 5 2c-2 1-2 2-2 3M12 17h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    clock:'M12 8v5l3 2M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0',
    check:'M5 12l4 4L19 6', message:'M4 4h16v13H9l-5 4V4M8 8h8M8 12h5',
    lock:'M6 10h12v11H6V10M8 10V7a4 4 0 0 1 8 0v3M12 14v3',
    search:'M21 21l-5-5M18 10a8 8 0 1 1-16 0 8 8 0 0 1 16 0',
    refresh:'M20 7v5h-5M4 17v-5h5M5 8a8 8 0 0 1 13-3l2 3M19 16A8 8 0 0 1 6 19l-2-3',
    arrow:'M5 12h14M14 7l5 5-5 5', user:'M20 21v-2a6 6 0 0 0-6-6h-4a6 6 0 0 0-6 6v2M16 6a4 4 0 1 1-8 0 4 4 0 0 1 8 0',
    info:'M12 11v6M12 7h.01M22 12a10 10 0 1 1-20 0 10 10 0 0 1 20 0'
  };
  function node(tag, text, className) {
    const item = document.createElement(tag);
    if (text !== undefined && text !== null) item.textContent = String(text);
    if (className) item.className = className;
    return item;
  }
  function icon(name) {
    const svg = document.createElementNS('http://www.w3.org/2000/svg', 'svg');
    for (const [key, value] of Object.entries({viewBox:'0 0 24 24',fill:'none',stroke:'currentColor','stroke-width':'1.6','stroke-linecap':'round','stroke-linejoin':'round','aria-hidden':'true'})) svg.setAttribute(key, value);
    const path = document.createElementNS(svg.namespaceURI, 'path');
    path.setAttribute('d', paths[name] || paths.info); svg.append(path); return svg;
  }
  function button(text, action, className='secondary') {
    const item = node('button', text, className); item.type = 'button';
    if (action) item.addEventListener('click', action); return item;
  }
  function card(title, symbol, className='') {
    const item = node('article', null, 'card '+className);
    if (title) { const heading=node('div',null,'card-heading'),h=node('h2',title); if(symbol)h.prepend(icon(symbol)); heading.append(h);item.append(heading); }
    return item;
  }
  function rows(values) {
    const dl=node('dl',null,'info-grid');
    for (const {label,value} of values) { const row=node('div');row.append(node('dt',label),node('dd',value));dl.append(row); } return dl;
  }
  function badge(status) { return node('span', status.label, 'badge '+status.tone); }
  function formatTime(value, compact=false) {
    if (!value) return '';
    const date=new Date(value); if(Number.isNaN(date.getTime()))return '';
    return new Intl.DateTimeFormat('zh-CN', compact?{month:'2-digit',day:'2-digit'}:{year:'numeric',month:'2-digit',day:'2-digit',hour:'2-digit',minute:'2-digit',hour12:false}).format(date);
  }
  function field(form, label, {type='textarea',required=true,choices,maxLength=2000}={}) {
    const id='field-'+crypto.randomUUID(),caption=node('label',label);caption.htmlFor=id;
    const input=node(choices?'select':type==='textarea'?'textarea':'input');input.id=id;input.required=required;
    if(choices)for(const [value,text] of choices){const option=node('option',text);option.value=value;input.append(option);}
    else {input.maxLength=maxLength;if(type==='textarea')input.rows=3;else input.type=type;}
    form.append(caption,input);return input;
  }
  class PortalApp {
    constructor() {
      this.csrf='';this.me=null;this.epoch=0;this.scope='mine';this.queue='';this.selected=null;this.active=null;this.pending=null;
      this.busy=false;this.requests=new Set();this.polling=null;this.searching=null;this.listSequence=0;this.detailSequence=0;
      this.items=[];this.offset=0;this.total=0;this.cancelTarget=null;
      document.querySelectorAll('[data-icon]').forEach(item=>item.append(icon(item.dataset.icon)));
      this.bind(); this.navigation(); this.restore();
    }
    async api(path,{method='GET',body,headers={},timeoutMs=120000}={}) {
      const session=this.epoch,controller=new AbortController();this.requests.add(controller);
      const timeout=setTimeout(()=>controller.abort(),timeoutMs);
      try {
        const response=await fetch(path,{method,headers:{'X-Portal-Request':'1',...(this.csrf?{'X-CSRF-Token':this.csrf}:{}),...(body?{'Content-Type':'application/json'}:{}),...headers},body:body?JSON.stringify(body):undefined,signal:controller.signal,cache:'no-store',credentials:'same-origin'});
        const data=response.status===204?null:await response.json();
        if(session!==this.epoch)throw Object.assign(new Error('登录状态已变化，请重新操作。'),{stale:true});
        if(!response.ok)throw Object.assign(new Error(({401:path==='/auth/login'?'账号或密码不正确。':'登录已过期，请重新登录。',429:'登录尝试过于频繁，请稍后再试。',403:'当前账号没有此操作的权限。',404:'未找到可访问的工单，请刷新列表。',409:'工单状态已变化，或上次操作结果尚未确认。请查看最新进度后再操作。',422:'填写的信息不符合要求，请检查后重新提交。'})[response.status]||'暂时无法完成操作，请稍后重试。'),{status:response.status});
        return data;
      } catch(error) {
        if(session!==this.epoch)error.stale=true;
        if(!error.stale&&!error.status)error.message='连接暂时中断，操作结果尚未确认。请查看工单进度或重试。';
        throw error;
      } finally {clearTimeout(timeout);this.requests.delete(controller);}
    }
    notice(text,error=false) {
      const target=$('notice');target.className='notice'+(error?' error':'');
      const close=button('×',()=>target.hidden=true,'icon-button');close.setAttribute('aria-label','关闭提示');
      target.replaceChildren(node('span',text),close);target.hidden=false;
    }
    handle(error) {if(!error.stale){if(error.status===401&&this.me){this.resetSession();$('login-error').textContent='登录已过期，请重新登录。';}else if($('cancel-dialog').open)$('cancel-error').textContent=error.message;else this.notice(error.message,true);}}
    stopPolling(){clearTimeout(this.polling);this.polling=null;}
    navigation() {
      const support=this.scope==='support';
      $('nav-mine').classList.toggle('active',!support);$('nav-support').classList.toggle('active',support);
      $('nav-mine').setAttribute('aria-current',support?'false':'page');$('nav-support').setAttribute('aria-current',support?'page':'false');
      $('page-title').textContent=support?'审批与处理':'我的工单';
      $('breadcrumb-current').textContent=support?'审批与处理':'我的工单';
      $('page-eyebrow').textContent=support?'SUPPORT WORKSPACE':'SERVICE REQUESTS';
      $('page-description').textContent=support?'查看权限范围内的工单，完成审批、核查和处理反馈。':'在这里查看申请进度，及时补充需要的信息。';
      $('list-title').textContent=support?'可处理工单':'工单列表';$('new-ticket').hidden=support;
      $('support-queue-row').hidden=!support;
      const captions=support?['全部工单','待认领','我负责的','已超时']:['全部工单','待补充 / 确认','处理中','已结束'];
      document.querySelectorAll('.stat small').forEach((item,index)=>item.textContent=captions[index]);
      document.querySelectorAll('.stat-trailing').forEach((item,index)=>item.textContent=(support?['全部记录','接手处理','持续跟进','需要升级']:['全部记录','需要关注','跟进进展','查看结果'])[index]);
      document.title='IT 服务中心 · '+(support?'审批与处理':'我的工单');
    }
    bind() {
      $('connect').addEventListener('submit',event=>{event.preventDefault();this.login();});
      $('logout').onclick=()=>this.logout();
      $('nav-mine').onclick=()=>this.switchScope('mine').catch(error=>this.handle(error));
      $('nav-support').onclick=()=>this.switchScope('support').catch(error=>this.handle(error));
      for(const id of ['nav-create','new-ticket'])$(id).onclick=()=>this.openCreate();
      $('refresh').onclick=async()=>{if(this.busy)return;try{await this.list({keep:true});if(this.selected)await this.load(this.selected);}catch(error){this.handle(error);}};
      $('filter').onchange=()=>this.list().catch(error=>this.handle(error));
      $('support-queue').onchange=()=>{this.queue=$('support-queue').value;this.list().catch(error=>this.handle(error));};
      document.querySelectorAll('.stat').forEach(item=>item.onclick=()=>{if(this.scope==='support'){this.queue=({'':'',action_required:'unassigned',processing:'mine',completed:'overdue'})[item.dataset.filter];$('support-queue').value=this.queue;$('filter').value='';}else $('filter').value=item.dataset.filter;this.list().catch(error=>this.handle(error));});
      $('ticket-search').oninput=()=>{clearTimeout(this.searching);this.searching=setTimeout(()=>this.list().catch(error=>this.handle(error)),300);};
      $('more').onclick=()=>this.list({append:true,keep:true}).catch(error=>this.handle(error));
      for(const id of ['close-create','cancel-create'])$(id).onclick=()=>{if(!this.busy)$('create-dialog').close();};
      $('create-dialog').addEventListener('cancel',event=>{if(this.busy)event.preventDefault();});
      $('create-form').onsubmit=event=>{event.preventDefault();this.create();};
      for(const id of ['close-cancel','keep-ticket'])$(id).onclick=()=>{if(!this.busy)$('cancel-dialog').close();};
      $('cancel-dialog').addEventListener('cancel',event=>{if(this.busy)event.preventDefault();});
      $('cancel-form').onsubmit=event=>{event.preventDefault();if(!this.cancelTarget)return;this.mutate('/portal/tickets/'+encodeURIComponent(this.cancelTarget.id)+'/cancel',{reason:$('cancel-reason').value,case_revision:this.cancelTarget.revision},'工单已撤销。',()=>{$('cancel-dialog').close();});};
    }
    async restore() {
      const session=this.epoch,submit=$('connect').querySelector('button');this.busy=true;submit.disabled=true;
      try {const data=await this.api('/auth/session',{timeoutMs:10000});if(session===this.epoch){this.activateSession(data);await this.list();}}
      catch(error){if(session!==this.epoch)return;if(error.status!==401){if(this.me)this.handle(error);else $('login-error').textContent='暂时无法连接服务，请稍后再试。';}}
      finally{if(session===this.epoch){this.busy=false;submit.disabled=false;}}
    }
    activateSession(data) {
      this.me=data.user;this.csrf=data.csrf_token;
      const me=this.me;$('password').value='';
      $('identity').replaceChildren(node('span',me.user_id.slice(0,1).toUpperCase(),'avatar'),node('span',me.user_id));
      $('identity').hidden=false;$('logout').hidden=false;$('login').hidden=true;$('workspace').hidden=false;
      const authorized=['manager','admin'].includes(me.role);$('nav-support').hidden=!authorized;
      this.scope=location.pathname==='/support'&&authorized?'support':'mine';this.navigation();history.replaceState(null,'',this.scope==='support'?'/support':'/');
    }
    async login() {
      if(this.busy||!$('connect').reportValidity())return;
      const submit=$('connect').querySelector('button'),session=this.epoch;submit.disabled=true;this.busy=true;$('login-error').textContent='';
      try {
        const data=await this.api('/auth/login',{method:'POST',body:{username:$('username').value.trim(),password:$('password').value},timeoutMs:15000});
        if(session!==this.epoch)return;this.activateSession(data);await this.list();
      } catch(error) {if(session!==this.epoch)return;if(!this.me)$('login-error').textContent=error.message;else this.handle(error);}
      finally {if(session===this.epoch){this.busy=false;submit.disabled=false;$('password').value='';}}
    }
    async logout() {
      if(!this.me||$('logout').disabled)return;
      const session=this.epoch;$('logout').disabled=true;
      try {await this.api('/auth/logout',{method:'POST',timeoutMs:10000});if(session===this.epoch)this.resetSession();}
      catch(error){if(session===this.epoch){if(error.status===401)this.resetSession();else this.notice('未能完成退出，请稍后重试。',true);}}
      finally{if(session===this.epoch)$('logout').disabled=false;}
    }
    resetSession() {
      this.epoch++;for(const request of this.requests)request.abort();this.stopPolling();clearTimeout(this.searching);
      this.csrf='';this.me=null;this.selected=null;this.active=null;this.pending=null;this.items=[];this.busy=false;this.cancelTarget=null;this.scope='mine';this.queue='';$('support-queue').value='';
      $('workspace').hidden=true;$('login').hidden=false;$('logout').hidden=true;$('logout').disabled=false;$('identity').hidden=true;$('identity').replaceChildren();$('nav-support').hidden=true;$('nav-count').hidden=true;
      $('username').value='';$('password').value='';$('login-error').textContent='';$('notice').hidden=true;$('ticket-search').value='';$('filter').value='';
      $('ticket-list').replaceChildren();this.emptyDetail();$('create-dialog').close();$('cancel-dialog').close();this.resetCreate();$('connect').querySelector('button').disabled=false;this.navigation();history.replaceState(null,'','/');
    }
    async switchScope(scope) {
      if(!this.me||this.busy||scope===this.scope)return;
      if(scope==='support'&&!['manager','admin'].includes(this.me.role))return;
      this.stopPolling();this.scope=scope;this.queue='';$('support-queue').value='';this.selected=null;this.active=null;this.items=[];this.detailSequence++;this.listSequence++;
      $('filter').value='';$('ticket-search').value='';$('notice').hidden=true;this.navigation();this.emptyDetail();
      history.replaceState(null,'',scope==='support'?'/support':'/');await this.list();
    }
    async list({append=false,keep=false}={}) {
      if(!this.me)return;
      const sequence=++this.listSequence,session=this.epoch,scope=this.scope,queue=this.queue,group=$('filter').value,query=$('ticket-search').value.trim();
      const offset=append?this.offset:0;
      const params=new URLSearchParams({scope,limit:'30',offset:String(offset)});if(group)params.set('group',group);if(query)params.set('query',query);if(scope==='support'&&queue)params.set('queue',queue);
      const data=await this.api('/portal/tickets?'+params);
      if(sequence!==this.listSequence||session!==this.epoch||scope!==this.scope||queue!==this.queue||group!==$('filter').value||query!==$('ticket-search').value.trim())return;
      this.items=append?[...this.items,...data.items]:data.items;this.offset=offset+data.items.length;this.total=data.total;
      for(const name of ['total','action_required','processing','completed'])$('count-'+name).textContent=data.counts[this.scope==='support'?({total:'total',action_required:'unassigned',processing:'mine',completed:'overdue'})[name]:name]||0;
      const count=data.counts.action_required;$('nav-count').textContent=count;$('nav-count').hidden=!count||scope==='support';
      $('list-total').textContent=data.total+' 张';$('more').hidden=this.offset>=data.total;
      document.querySelectorAll('.stat').forEach(item=>{const chosen=this.scope==='support'?({'':'',unassigned:'action_required',mine:'processing',overdue:'completed'})[queue]:group;item.classList.toggle('active',item.dataset.filter===chosen);item.setAttribute('aria-pressed',String(item.dataset.filter===chosen));});
      this.renderList();
      if(!keep||!this.selected) {
        const chosen=this.items.find(item=>item.id===this.selected)||this.items[0];
        if(chosen)await this.load(chosen.id);else{this.stopPolling();this.selected=null;this.active=null;this.emptyDetail();}
      }
    }
    renderList() {
      const container=$('ticket-list');container.replaceChildren();
      for(const ticket of this.items) {
        const item=button('',async()=>{try{await this.load(ticket.id);if(window.matchMedia?.('(max-width: 900px)').matches)$('detail').scrollIntoView?.({block:'start',behavior:window.matchMedia('(prefers-reduced-motion: reduce)').matches?'instant':'smooth'});}catch(error){this.handle(error);}},'ticket'+(ticket.id===this.selected?' active':''));item.dataset.ticketId=ticket.id;
        item.setAttribute('aria-pressed',String(ticket.id===this.selected));
        const top=node('div',null,'ticket-row');top.append(node('small','#'+ticket.reference),node('small',formatTime(ticket.created_at,true)));
        const bottom=node('div',null,'ticket-row'),arrow=node('span',null,'ticket-arrow');arrow.append(icon('arrow'));bottom.append(badge(ticket.status),arrow);
        item.append(top,node('strong',ticket.title));if(this.scope==='support')item.append(node('span',ticket.requester+' · '+ticket.department,'ticket-owner'));item.append(bottom);if(ticket.handling?.overdue)item.append(node('small','已超时 · 需经理跟进','overdue-text'));else if(this.scope==='support'&&ticket.handling?.assignee)item.append(node('small','处理人：'+ticket.handling.assignee));container.append(item);
      }
      if(!this.items.length)container.append(node('p',this.total?'继续加载以查看工单。':$('ticket-search').value||$('filter').value?'没有找到符合条件的工单。':'暂时没有工单，提交问题后会显示在这里。','empty-list'));
    }
    async load(id,{poll=false}={}) {
      this.stopPolling();const previous=this.selected;this.selected=id;const sequence=++this.detailSequence,session=this.epoch,scope=this.scope;
      this.renderList();
      if(!poll&&previous!==id){this.active=null;this.emptyDetail('正在读取工单…','请稍候。');}
      let ticket;
      try {ticket=await this.api('/portal/tickets/'+encodeURIComponent(id)+'?scope='+scope);}
      catch(error){if(!error.stale&&sequence===this.detailSequence&&session===this.epoch&&id===this.selected&&!poll)this.emptyDetail('暂时无法读取这张工单','请点击工单列表右上角的刷新按钮，重新获取进度。');throw error;}
      if(sequence!==this.detailSequence||session!==this.epoch||scope!==this.scope||id!==this.selected)return;
      this.active=ticket;this.render(ticket);const index=this.items.findIndex(item=>item.id===id);if(index>=0)this.items[index]=ticket;this.renderList();
      const delay=ticket.needs_polling?2500:scope==='mine'&&ticket.status.group==='processing'?15000:0;
      if(delay)this.polling=setTimeout(async()=>{if(this.busy){this.queuePoll(id);return;}try{await this.load(id,{poll:true});await this.list({keep:true});}catch(error){if(!error.stale){this.notice('进度更新暂时中断，可点击工单列表右上角刷新。',true);}}},delay);
    }
    queuePoll(id){this.polling=setTimeout(()=>{if(this.busy)this.queuePoll(id);else if(this.selected===id)this.load(id,{poll:true}).catch(error=>this.handle(error));},2500);}
    emptyDetail(title='选择一张工单，查看处理进展',text='也可以点击“提交工单”，告诉我们你需要什么帮助。') {
      const item=card(null,null,'empty-detail'),symbol=node('span',null,'empty-icon');symbol.append(icon('tickets'));item.append(symbol,node('h2',title),node('p',text));$('detail').replaceChildren(item);
    }
    render(ticket) {
      const root=$('detail');root.replaceChildren();
      const heading=card(null,null,'ticket-heading'),top=node('div',null,'ticket-heading-top');top.append(node('span','工单 #'+ticket.reference,'reference'),badge(ticket.status));
      const meta=node('div',null,'ticket-meta');
      for(const [symbol,text] of [['clock','提交于 '+formatTime(ticket.created_at)],['tickets',ticket.category],['info','工作影响：'+ticket.severity]]){const item=node('span',text);item.prepend(icon(symbol));meta.append(item);}
      heading.append(top,node('h2',ticket.title),meta);root.append(heading);
      const progress=card('处理进度','clock','progress-card'),steps=node('ol',null,'stepper');
      const labels=['已提交','确认与排查','审批或支持跟进','已结束'];
      for(let index=0;index<labels.length;index++){
        const current=index===ticket.status.stage,item=node('li',null,index<ticket.status.stage?'done':current?'current'+(ticket.status.tone==='green'?' terminal':''):'');
        const dot=node('span',index<ticket.status.stage?null:index+1,'step-dot');if(index<ticket.status.stage)dot.append(icon('check'));item.append(dot,node('span',labels[index]));if(current)item.setAttribute('aria-current','step');steps.append(item);
      }
      const status=node('div',null,'status-panel '+ticket.status.tone),symbol=node('span',null,'status-icon'),copy=node('div');symbol.append(icon(ticket.status.tone==='green'?'check':ticket.status.tone==='amber'?'info':'clock'));
      copy.append(node('h3',ticket.status.title),node('p',ticket.status.message));status.append(symbol,copy);
      const next=node('div',null,'next-action');next.append(node('strong','接下来'),node('p',ticket.status.next_action));progress.append(steps,status,next);root.append(progress);
      if(ticket.handling?.team){const handling=card('服务跟进','user');handling.append(rows([{label:'支持组',value:ticket.handling.team},{label:'处理人',value:ticket.handling.assignee||'等待支持人员认领'},{label:'本次跟进期限',value:formatTime(ticket.handling.due_at)}]));if(ticket.handling.overdue)handling.append(node('p','已超过跟进期限，已列入经理跟进队列。','overdue-text'));root.append(handling);}
      if(this.scope==='mine'&&ticket.clarification)this.clarification(root,ticket);
      if(ticket.waiting?.expires_at){const wait=card(ticket.waiting.kind==='verification'?'验证期限':'补充信息期限','clock');wait.append(node('p','请在 '+formatTime(ticket.waiting.expires_at)+' 前回复。'));if(ticket.waiting.reminded_at)wait.append(node('p','提醒：请及时完成本次回复。','field-help'));root.append(wait);}
      if(this.scope==='mine'&&(ticket.verification?.can_confirm||ticket.verification?.can_reopen))this.verificationForm(root,ticket);
      if(this.scope==='mine'&&ticket.verification?.can_report_recovered){const recovered=card('问题已自行恢复？','message');recovered.append(node('p','如果你已实际验证恢复，可以确认结束处理；原有事件和证据会保留。'),button('我已验证，问题已恢复',()=>this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/verify',{revision:ticket.case_revision,verdict:'confirmed',note:'申请人已自行验证恢复。'},'已记录你的验证结果。'),'secondary'));root.append(recovered);}
      if(this.scope==='mine'&&ticket.can_follow_up&&!ticket.clarification)this.followUpForm(root,ticket);
      if(this.scope==='support'&&ticket.clarification){const waiting=card('等待申请人补充','message');waiting.append(node('p','当前需要由申请人补充或确认信息。','muted'));if(ticket.confirmation)waiting.append(rows(ticket.confirmation));root.append(waiting);}
      const description=card('申请内容','tickets');description.append(node('p',ticket.description,'description-text'));
      const details=[...(ticket.request_details||[])];if(this.scope==='support')details.unshift({label:'申请人',value:ticket.requester},{label:'所属部门',value:ticket.department});
      if(details.length)description.append(rows(details));root.append(description);
      if(this.scope==='support'&&ticket.support)this.support(root,ticket);
      const activity=card('服务动态','clock'),timeline=node('ol',null,'timeline');
      for(const event of ticket.activity||[]){const item=node('li',event.label),time=node('time',formatTime(event.at));time.dateTime=event.at;item.append(time);timeline.append(item);}activity.append(timeline);root.append(activity);
      if(ticket.conversation?.length){const history=card(),details=node('details',null,'conversation-details');details.append(node('summary','沟通记录 · '+ticket.conversation.length+' 条'));
        for(const message of ticket.conversation){const item=node('div',null,'message '+message.role),meta=node('div',null,'message-meta');meta.append(node('strong',message.role==='user'?'申请人':'服务助手'),node('time',formatTime(message.at)));item.append(meta,node('div',message.message,'message-text'));details.append(item);}history.append(details);root.append(history);}
      if(this.scope==='mine'&&ticket.can_cancel){const foot=node('div',null,'actions');foot.append(node('span','如果已不再需要本次服务，可以撤销申请。','muted'),button('撤销工单',()=>{this.cancelTarget={id:ticket.id,revision:ticket.case_revision};$('cancel-form').reset();$('cancel-error').textContent='';$('cancel-dialog').showModal();},'text-button'));root.append(foot);}
    }
    followUpForm(root,ticket,question=null) {
      const item=card(question?'需要你补充信息':'补充故障线索','message','action-card'),form=node('form');
      item.append(node('p',question||'有新的报错、发生时间或复现步骤？请补充在原工单中，支持人员会继续跟进。'));
      const answer=field(form,'补充本次故障的信息',{maxLength:10000});answer.placeholder='请补充本次故障的具体情况，其他服务申请请另建工单。';
      const actions=node('div',null,'actions'),submit=node('button','提交补充信息','primary');submit.type='submit';actions.append(submit);form.append(actions);
      form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/follow-up',{answer:answer.value,revision:ticket.case_revision},'故障信息已补充，将继续在原工单跟进。');};
      item.append(form);root.append(item);
    }
    verificationForm(root,ticket) {
      const pending=ticket.verification.can_confirm,item=card(pending?'验证处理结果':'问题仍在或再次出现？','message','action-card'),form=node('form');
      const verdict=field(form,'当前情况',{choices:pending?[['confirmed','我已验证，可以正常使用'],['still_failing','验证后仍然失败']]:[['recurred','问题仍然存在或再次出现']]}),note=field(form,'补充说明',{maxLength:2000});
      note.required=!pending;const update=()=>{note.required=verdict.value!=='confirmed';};verdict.onchange=update;update();
      item.append(node('p',pending?'请实际使用后再确认。仍然失败会在原工单继续处理。':'提交后在原工单继续跟进，原有证据和处理记录会保留。'));
      if(ticket.verification.reopen_until)item.append(node('p','可重新打开至 '+formatTime(ticket.verification.reopen_until),'field-help'));
      const actions=node('div',null,'actions'),submit=node('button',pending?'提交验证结果':'重新打开工单','primary');submit.type='submit';actions.append(submit);form.append(actions);
      form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/verify',{revision:ticket.case_revision,verdict:verdict.value,note:note.value},'你的反馈已记录，请查看最新进度。');};
      item.append(form);root.append(item);
    }
    clarification(root,ticket) {
      if(ticket.clarification.stage==='investigation'){this.followUpForm(root,ticket,ticket.clarification.question);return;}
      const item=card(ticket.confirmation?'请确认本次申请':'需要你补充信息','message','action-card'),form=node('form');
      item.append(node('p',ticket.clarification.question));
      if(ticket.confirmation){item.append(rows(ticket.confirmation));const actions=node('div',null,'actions');actions.append(button('确认申请信息',()=>this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/clarify',{answer:'确认执行',round_number:ticket.clarification.round_number},'已确认，正在继续处理。'),'primary'));item.append(actions);}
      const answer=field(form,ticket.confirmation?'信息有误？说明需要调整的内容':'补充具体情况',{maxLength:10000});answer.placeholder=ticket.confirmation?'请说明正确的系统、账号或申请事项。':'请描述发生时间、具体表现或报错内容。';
      const actions=node('div',null,'actions'),submit=node('button',ticket.confirmation?'提交调整意见':'提交补充信息','primary');submit.type='submit';actions.append(submit,node('span','补充后将继续处理这张工单','action-foot'));form.append(actions);
      form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/clarify',{answer:answer.value,round_number:ticket.clarification.round_number},'补充信息已提交。');};item.append(form);root.append(item);
    }
    support(root,ticket) {
      const support=ticket.support,item=card('支持人员处理区','shield','support-card');item.prepend(node('div','SUPPORT ONLY · 审批与核查','support-label'));
      if(support.can_claim){item.append(node('p','这张工单正在等待支持人员认领。认领后由你负责核查和处理。'),button('认领并开始处理',()=>this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/claim',{revision:support.assignment_revision},'已认领，请继续处理这张工单。'),'primary'));}
      if(support.can_transfer){const form=node('form',null,'subsection');form.append(node('h3','转派处理'));const candidates=(support.assignees||[]).filter(person=>person.user_id!==ticket.handling?.assignee);if(candidates.length){const target=field(form,'转派给',{choices:candidates.map(person=>[person.user_id,person.user_id+(person.role==='admin'?'（管理员）':'（经理）')])}),reason=field(form,'转派原因',{type:'text'});const submit=node('button','确认转派','secondary');submit.type='submit';const actions=node('div',null,'actions');actions.append(submit);form.append(actions);form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/transfer',{assignee:target.value,revision:support.assignment_revision,reason:reason.value},'已转派给新的处理人。');};item.append(form);}}
      if(support.can_request_info){const form=node('form',null,'subsection');form.append(node('h3','向申请人补充确认'));const fact=field(form,'需要补充的信息',{choices:[['occurred_at','故障开始时间'],['symptom','具体表现和报错'],['reproduction','复现步骤'],['previous_access','之前是否正常使用']]}),submit=node('button','请求用户补充','secondary');submit.type='submit';const actions=node('div',null,'actions');actions.append(submit);form.append(actions);form.onsubmit=event=>{event.preventDefault();this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/request-info',{field:fact.value,revision:support.assignment_revision},'已在工单中向申请人发出补充请求。');};item.append(form);}
      if(support.can_approve){item.append(node('p','请核对申请内容及权限范围，再决定是否同意本次申请。'));const actions=node('div',null,'actions');actions.append(button('同意申请',()=>this.mutate('/approvals/'+encodeURIComponent(support.approval_id),{approved:true},'审批已通过。'),'primary'),button('不予通过',()=>this.mutate('/approvals/'+encodeURIComponent(support.approval_id),{approved:false},'审批意见已提交。'),'secondary'));item.append(actions);}
      if(support.summary)item.append(node('h3','排查摘要'),node('p',support.summary));
      if(support.next_check)item.append(node('h3','建议下一步'),node('p',support.next_check));
      if(support.diagnostic_note)item.append(node('p',support.diagnostic_note,'field-help'));
      if(support.observations.length){const evidence=node('details');evidence.append(node('summary','查看排查依据'));
        for(const source of support.observations){evidence.append(node('h3',source.source));if(!source.records.length)evidence.append(node('p','未取得可用记录。','muted'));for(const record of source.records){const block=node('div',null,'evidence-item');block.append(node('strong',record.reference||'记录'),node('span',record.text));evidence.append(block);}}item.append(evidence);}
      if(support.can_investigate)item.append(button('继续排查',()=>this.mutate('/tickets/'+encodeURIComponent(ticket.id)+'/investigate',undefined,'已继续排查。'),'secondary'));
      if(support.can_feedback){const form=node('form',null,'subsection');form.append(node('h3','记录核查结果'));const verdict=field(form,'排查建议的核查结论',{choices:[['inconclusive','仍无法确认'],['accepted','采纳建议'],['rejected','不采纳建议']]}),note=field(form,'核查说明'),evidence=field(form,'核查凭证或记录编号',{type:'text'}),resolved=field(form,'问题是否已经解决',{choices:[['unknown','尚未确认'],['no','尚未解决'],['yes','支持侧已处理，待用户验证']]});const actions=node('div',null,'actions'),submit=node('button','提交核查反馈','primary');submit.type='submit';actions.append(submit);form.append(actions);form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/tickets/'+encodeURIComponent(ticket.id)+'/feedback',{revision:support.feedback_revision,case_revision:ticket.case_revision,verdict:verdict.value,note:note.value,evidence:evidence.value,issue_resolved:resolved.value==='unknown'?null:resolved.value==='yes'},'核查反馈已记录。');};item.append(form);}
      if(support.can_resolve){const form=node('form',null,'subsection');form.append(node('h3','人工处理结果'));const outcome=field(form,'处理结果',{choices:[['resolved','已处理，提交用户验证'],['archived','归档，未确认解决'],['rejected','不再继续办理']]}),note=field(form,'处理说明'),evidence=field(form,'核查凭证',{type:'text',required:false});form.append(node('p','存在未确认的执行结果时，必须填写核查凭证。','field-help'));const actions=node('div',null,'actions'),submit=node('button','确认人工结案','primary');submit.type='submit';actions.append(submit);form.append(actions);form.onsubmit=event=>{event.preventDefault();if(form.reportValidity())this.mutate('/tickets/'+encodeURIComponent(ticket.id)+'/resolve',{outcome:outcome.value,note:note.value,evidence:evidence.value,case_revision:ticket.case_revision},'人工处理结果已记录。');};item.append(form);}
      if(support.can_recover){const section=node('div',null,'subsection');section.append(node('p','已核查异常原因且具备继续处理条件时，可尝试恢复。系统会检查是否允许继续。'),button('检查并恢复处理',()=>this.mutate('/tickets/'+encodeURIComponent(ticket.id)+'/recover',undefined,'恢复请求已处理，请查看最新进度。'),'secondary'));item.append(section);}
      if(support.worker_issue){const section=node('div',null,'subsection');section.append(node('h3','排查异常'),node('p',support.worker_issue.message),node('p',support.worker_issue.escalated?'已达到重新排查上限，请由支持负责人继续人工处理。':'本轮剩余排查次数：'+support.worker_issue.remaining_runs,'field-help'));if(support.can_retry_investigation)section.append(button('重新排查',()=>this.mutate('/portal/tickets/'+encodeURIComponent(ticket.id)+'/retry-investigation',{revision:ticket.case_revision},'已安排重新排查，原工单与登记回执继续保留。'),'secondary'));item.append(section);}
      if(!support.can_claim&&!support.can_approve&&!support.summary&&!support.can_resolve&&!support.can_investigate)item.append(node('p','当前没有分配给你的处理事项。','muted'));
      for(const feedback of support.feedback)item.append(node('p',(feedback.actor||'支持人员')+'：'+feedback.note,'evidence-item'));root.append(item);
    }
    async mutate(path,body,message,onSuccess) {
      if(this.busy||!this.me)return;
      const session=this.epoch,id=this.selected;this.busy=true;this.stopPolling();$('cancel-error').textContent='';
      const controls=[...document.querySelectorAll('#detail button,#cancel-dialog button,#nav-mine,#nav-support,#new-ticket,#nav-create')].map(item=>({item,disabled:item.disabled}));controls.forEach(({item})=>item.disabled=true);
      try {await this.api(path,{method:'POST',body});if(session!==this.epoch)return;if(onSuccess)onSuccess();this.notice(message);await this.list({keep:true});if(this.selected===id)await this.load(id);}
      catch(error){if(session===this.epoch)this.handle(error);}
      finally{if(session===this.epoch){this.busy=false;controls.forEach(({item,disabled})=>item.disabled=disabled);}}
    }
    openCreate(){if(!this.me){$('username').focus();return;}if(!this.busy)$('create-dialog').showModal();}
    resetCreate(){$('create-form').reset();for(const id of ['title','description','severity','submit-ticket'])$(id).disabled=false;$('submit-ticket').textContent='提交工单';$('create-error').textContent='';$('pending-note').textContent='提交后可在“我的工单”查看进度。';}
    async create() {
      if(this.busy||!this.me||!$('create-form').reportValidity())return;
      const session=this.epoch;this.busy=true;$('submit-ticket').disabled=true;$('submit-ticket').textContent='正在提交…';$('create-error').textContent='';
      if(!this.pending)this.pending={key:crypto.randomUUID(),body:{title:$('title').value,description:$('description').value,severity:$('severity').value}};
      for(const id of ['title','description','severity'])$(id).disabled=true;
      let created=null;
      try{created=await this.api('/portal/tickets',{method:'POST',body:this.pending.body,headers:{'Idempotency-Key':this.pending.key}});if(session!==this.epoch)return;this.pending=null;this.resetCreate();$('create-dialog').close();}
      catch(error){if(session!==this.epoch)return;if(error.status===401){this.handle(error);return;}$('create-error').textContent=error.message;
        if([401,403,422].includes(error.status)){this.pending=null;for(const id of ['title','description','severity'])$(id).disabled=false;$('submit-ticket').textContent='提交工单';}
        else{$('submit-ticket').textContent='继续确认本次提交';$('pending-note').textContent='提交结果尚未确认，已保留本次内容。请保留当前页面，重试会继续确认本次提交，避免重复创建。';}}
      finally{if(session===this.epoch){this.busy=false;$('submit-ticket').disabled=false;}}
      if(created&&session===this.epoch){try{if(this.scope!=='mine'){this.scope='mine';this.queue='';$('support-queue').value='';this.navigation();history.replaceState(null,'','/');}this.selected=created.id;$('filter').value='';$('ticket-search').value='';this.notice('工单已提交。后续进展和待补充事项会在这里更新。');await this.list({keep:true});await this.load(created.id);}catch(error){this.handle(error);}}
    }
  }
  new PortalApp();
})();
