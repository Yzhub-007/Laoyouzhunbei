'use strict';
// UI permissions mirror server enforcement. Only the origin-bound CSRF token is
// kept in sessionStorage; passwords/session IDs never enter browser storage.
const Security = (() => {
  const tokenKey='laoyou.csrf';
  let authenticated=false, user=null, csrf='', permissions={}, leaving=false, observer=null, setup=false;
  const el=id=>document.getElementById(id);
  const roles={admin:'管理员',operator:'操作员',viewer:'只读用户'};
  const rules=[
    ['can_operate','#response-nav,#response-panel',true],
    ['can_configure','#response-review-details',true],
    ['can_configure','#monitor-config-details,#monitor-open-config,#save-pilot',true],
    ['can_configure','#pilot-form input,#pilot-form select,#pilot-form textarea',false],
    ['can_operate','#monitor-refresh,#monitor-correction-details,#monitor-points .button,#use-live,#reset-demo,#apply-json,label[for="import-file"],#generate-rules,#generate-ai,#trial-start,#trial-finish,#stop-trial,#trial-records button,#cards .review-details,#cards .card-body>form,#notice-list .review-details',true],
    ['can_operate','#input-panel input,#input-panel textarea,#input-panel select,#site-panel input,#site-panel textarea,#site-panel select',false],
    ['can_manage_users','#security-users',true]
  ];
  function can(permission){return authenticated && permissions[permission]===true;}
  function notice(text,error=false,target='security-feedback'){
    const output=el(target);output.textContent=text;output.className=error?'message error':'message';output.hidden=!text;
  }
  function denied(){if(typeof message==='function')message('当前账户没有此项操作权限，请联系管理员。',true);}
  function guard(permission){if(can(permission))return true;denied();return false;}
  function apply(){
    if(!authenticated)return;
    for(const [permission,selector,hide] of rules){
      for(const item of document.querySelectorAll(selector)){
        if(item.dataset.requires!==permission)item.dataset.requires=permission;
        if(!can(permission)){
          if(hide && !item.classList.contains('access-hidden'))item.classList.add('access-hidden');
          if('disabled' in item && !item.disabled)item.disabled=true;
          for(const control of item.querySelectorAll('button,input,textarea,select'))if(!control.disabled)control.disabled=true;
        }
      }
    }
  }
  function observe(){
    if(observer)return;
    observer=new MutationObserver(apply);
    observer.observe(document.querySelector('.workspace'),{childList:true,subtree:true,attributes:true,attributeFilter:['disabled','hidden']});
    document.addEventListener('submit',event=>{
      const form=event.target;
      const permission=form.id==='pilot-form'?'can_configure':form.closest('[data-requires]')?.dataset.requires;
      if(permission&&!can(permission)){event.preventDefault();event.stopImmediatePropagation();denied();}
    },true);
    document.addEventListener('click',event=>{
      const target=event.target.closest('[data-requires]');
      if(target&&!can(target.dataset.requires)){event.preventDefault();event.stopImmediatePropagation();denied();}
    },true);
  }
  function expire(){
    if(leaving)return;
    DraftGuard.forget();
    leaving=true;authenticated=false;csrf='';permissions={};user=null;
    sessionStorage.removeItem(tokenKey);
    document.querySelector('.workspace').hidden=true;
    el('account-controls').hidden=true;
    el('auth-gate').hidden=false;
    el('auth-title').textContent='会话已结束';
    el('auth-description').textContent='正在返回登录页面…';
    el('auth-form').hidden=true;
    // Reload also destroys current observations, locally edited drafts and JS state.
    window.location.replace(window.location.pathname);
  }
  async function authFetch(path,options={}){
    if(leaving)throw new Error('会话已结束，请重新登录。');
    const headers=new Headers(options.headers||{});if(csrf)headers.set('X-App-Token',csrf);
    const response=await fetch(path,{credentials:'same-origin',cache:'no-store',...options,headers});
    if(response.status===401){expire();throw new Error('会话已失效，请重新登录。');}
    if(response.status===403)denied();
    return response;
  }
  async function request(path,payload){
    const response=await authFetch(path,{method:'POST',headers:{'Content-Type':'application/json','X-App-Token':csrf},body:JSON.stringify(payload),signal:AbortSignal.timeout(15000)});
    const result=await response.json();
    if(!response.ok)throw new Error(result.error||'操作未完成。');
    return result;
  }
  function showGate(status){
    setup=Boolean(status.setup_required);
    el('auth-gate').hidden=false;
    document.querySelector('.workspace').hidden=true;
    el('auth-title').textContent=setup?'首次创建管理员':'登录防汛工作台';
    el('auth-description').textContent=setup?'使用本机的一次性设置码创建管理员账户，再开始监测。':'登录后按账户权限查看监测与处理任务。';
    el('auth-setup-fields').hidden=!setup;
    el('auth-setup-code').required=setup;
    el('auth-password').minLength=setup?15:1;
    el('auth-password').autocomplete=setup?'new-password':'current-password';
    el('auth-password-help').textContent=setup?'至少15字符，可用长句；最多128字符。':'请输入当前账户密码。';
    el('auth-submit').textContent=setup?'创建管理员并登录':'登录';
    el('auth-form').hidden=false;
  }
  async function signIn(event){
    event.preventDefault();const button=el('auth-submit');button.disabled=true;notice('',false,'auth-feedback');
    try{
      const payload={username:el('auth-username').value.trim(),password:el('auth-password').value};
      if(setup)payload.setup_code=el('auth-setup-code').value.trim();
      const response=await fetch(setup?'/api/auth/setup':'/api/auth/login',{method:'POST',credentials:'same-origin',cache:'no-store',headers:{'Content-Type':'application/json'},body:JSON.stringify(payload),signal:AbortSignal.timeout(15000)});
      const result=await response.json();
      el('auth-password').value='';
      if(!response.ok)throw new Error(result.error||'登录未完成，请检查账户信息。');
      if(typeof result.csrf_token!=='string'||!result.csrf_token)throw new Error('登录响应缺少会话校验，请重新登录。');
      sessionStorage.setItem(tokenKey,result.csrf_token);
      el('auth-setup-code').value='';window.location.reload();
    }catch(error){notice(error.name==='TimeoutError'?'连接超时，请检查本机服务后重试。':error.message,true,'auth-feedback');}
    finally{el('auth-password').value='';button.disabled=false;}
  }
  async function logout(){
    if(!DraftGuard.confirmDiscard(null,'退出账户'))return;
    el('account-logout').disabled=true;
    try{await request('/api/auth/logout',{});expire();}
    catch(error){notice(error.message,true);el('account-logout').disabled=false;}
  }
  async function changePassword(event){
    event.preventDefault();if(!DraftGuard.confirmDiscard(null,'修改密码后重新登录'))return;const button=el('security-password-save');button.disabled=true;
    try{
      if(el('security-password-new').value!==el('security-password-confirm').value)throw new Error('两次输入的新密码不一致。');
      await request('/api/auth/password',{current_password:el('security-password-current').value,new_password:el('security-password-new').value});
      expire();
    }catch(error){notice(error.message,true);}
    finally{event.target.reset();button.disabled=false;}
  }
  async function loadUsers(){
    if(!can('can_manage_users'))return;
    el('security-users-refresh').disabled=true;
    try{
      const response=await authFetch('/api/auth/users',{signal:AbortSignal.timeout(15000)});
      const result=await response.json();if(!response.ok)throw new Error(result.error||'账户列表暂不可用。');
      const list=el('security-users-list');list.replaceChildren();
      for(const account of result.users||[]){
        const row=document.createElement('div');row.className='security-user-row';
        const name=document.createElement('span');name.textContent=`${account.username} · ${roles[account.role]||account.role} · ${account.disabled?'已停用':'启用中'}`;
        const edit=document.createElement('button');edit.type='button';edit.className='button secondary';edit.textContent='编辑';
        edit.addEventListener('click',()=>{el('security-user-name').value=account.username;el('security-user-role').value=account.role;el('security-user-disabled').checked=Boolean(account.disabled);el('security-user-password').value='';el('security-user-name').focus();});
        row.append(name,edit);list.append(row);
      }
    }catch(error){notice(error.message,true);}
    finally{el('security-users-refresh').disabled=false;apply();}
  }
  async function saveUser(event){
    event.preventDefault();if(!guard('can_manage_users'))return;
    const button=el('security-user-save');button.disabled=true;
    try{
      const payload={username:el('security-user-name').value.trim(),role:el('security-user-role').value,disabled:el('security-user-disabled').checked};
      if(el('security-user-password').value)payload.password=el('security-user-password').value;
      await request('/api/auth/users',payload);event.target.reset();
      notice('账户设置已保存。账户停用或权限变化后的会话由服务端撤销。');await loadUsers();
    }catch(error){notice(error.message,true);}
    finally{el('security-user-password').value='';button.disabled=false;apply();}
  }
  function configure(data){
    user=data.user;csrf=data.token||csrf;permissions=data.permissions||{};
    if(csrf)sessionStorage.setItem(tokenKey,csrf);
    if(!user||!roles[user.role]){expire();return;}
    document.body.dataset.role=user.role;
    el('account-name').textContent=`${user.username} · ${roles[user.role]}`;
    el('account-controls').hidden=false;
    el('security-role-note').textContent=user.role==='admin'?'可管理账户、配置来源并处理任务。':user.role==='operator'?'可处理任务与现场校正；来源、建档和账户设置由管理员维护。':'当前账户为只读：可查看监测、来源状态和历史记录，不能修改数据或触发采集。';
    el('security-users').hidden=!can('can_manage_users');
    el('auth-gate').hidden=true;
    document.querySelector('.workspace').hidden=false;
    observe();apply();
  }
  function initializationFailed(text){
    document.querySelector('.workspace').hidden=true;el('auth-gate').hidden=false;el('auth-form').hidden=true;
    el('auth-title').textContent='工作台暂未载入';el('auth-description').textContent=text;el('auth-retry').hidden=false;
  }
  async function prepare(){
    el('auth-form').addEventListener('submit',signIn);
    el('account-logout').addEventListener('click',logout);
    el('account-security').addEventListener('click',()=>{el('security-panel').open=true;el('security-panel').scrollIntoView({block:'start'});el('security-password-current').focus({preventScroll:true});if(can('can_manage_users'))loadUsers();});
    el('security-password-form').addEventListener('submit',changePassword);
    el('security-user-form').addEventListener('submit',saveUser);
    el('security-users-refresh').addEventListener('click',loadUsers);
    el('auth-retry').addEventListener('click',()=>window.location.reload());
    try{
      csrf=sessionStorage.getItem(tokenKey)||'';
      const response=await fetch('/api/auth/status',{credentials:'same-origin',cache:'no-store',headers:csrf?{'X-App-Token':csrf}:{},signal:AbortSignal.timeout(10000)});
      const status=await response.json();if(!response.ok)throw new Error(status.error||'身份验证服务不可用。');
      authenticated=status.authenticated===true;user=status.user;csrf=status.csrf_token||'';
      if(!authenticated){sessionStorage.removeItem(tokenKey);showGate(status);return false;}
      return true;
    }catch(error){showGate({});el('auth-form').hidden=true;el('auth-retry').hidden=false;notice('无法连接身份验证服务，请检查本机服务后重试。',true,'auth-feedback');return false;}
  }
  return {prepare,configure,apply,can,guard,fetch:authFetch,expire,initializationFailed,authenticated:()=>authenticated&&!leaving,csrf:()=>csrf};
})();
