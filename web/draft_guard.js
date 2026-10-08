'use strict';
// Draft contents and baselines live only in memory, never in browser storage.
const DraftGuard = (() => {
  const drafts=new Map();
  function signature(read){return JSON.stringify(read());}
  function snapshot(selector){const root=document.querySelector(selector);return root?[...root.querySelectorAll('input,textarea,select')].filter(item=>!['password','file'].includes(item.type)).map(item=>[item.id,item.type==='checkbox'||item.type==='radio'?item.checked:item.value]):[];}
  function dirty(key){const item=drafts.get(key);return Boolean(item&&signature(item.read)!==item.saved);}
  function changed(keys){return [...drafts.keys()].filter(key=>(!keys||keys.includes(key))&&dirty(key));}
  function render(){const output=document.getElementById('draft-status');if(!output)return;const labels=changed().map(key=>drafts.get(key).label);output.hidden=!labels.length;output.textContent=labels.length?'有未保存修改：'+labels.join('、')+'。切换页面会保留；请在离开或关闭前保存。':'';}
  function register(key,label,read){if(drafts.has(key))return;drafts.set(key,{label,read,saved:signature(read)});render();}
  function saved(key){const item=drafts.get(key);if(item)item.saved=signature(item.read);render();}
  function confirmDiscard(keys,action='继续操作'){const list=changed(keys);return !list.length||window.confirm('以下资料有未保存修改：'+list.map(key=>drafts.get(key).label).join('、')+'。\n'+action+'会丢弃这些修改，是否继续？');}
  function forget(){drafts.clear();render();}
  function lock(selector){const controls=[...document.querySelectorAll(selector)].flatMap(root=>[...root.querySelectorAll('input,textarea,select,button')]);const prior=controls.map(item=>item.disabled);controls.forEach(item=>item.disabled=true);return ()=>{controls.forEach((item,index)=>item.disabled=prior[index]);if(typeof Security!=='undefined')Security.apply();};}
  document.addEventListener('input',render);document.addEventListener('change',render);
  window.addEventListener('beforeunload',event=>{if(changed().length){event.preventDefault();event.returnValue='';}});
  return {register,saved,dirty,render,confirmDiscard,forget,snapshot,lock};
})();
