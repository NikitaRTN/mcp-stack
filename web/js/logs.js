import { el, clear, setText, fmtClock, debounce, pretty, download } from './util.js'
import { rpc } from './api.js'
import { card, pill, reportError, confirmDialog, toast, openDrawer, segmented } from './ui.js'
import { diag } from './diag.js'

let stream,list,meta,newPill,follow=true,paused=false,buffer=[],seen=new Set(),disposed=false,lastSeq=0
let filter={level:'debug',source:'',search:''}

function matches(e){const rank={debug:0,info:1,warn:2,error:3};return (rank[e.level]??1)>=(rank[filter.level]??0)&&(!filter.source||e.source===filter.source)&&(!filter.search||(e.message+' '+e.event+' '+JSON.stringify(e.fields||{})).toLowerCase().includes(filter.search.toLowerCase()))}
function line(e){const node=el('div.logline.'+(e.level||'info')+'.clickable',null,[el('span.t',{text:fmtClock(e.ts)}),el('span.lvl',{text:e.level}),el('span.src',{text:e.source||'hub',title:e.source||''}),el('span.msg',null,[e.message||'',e.event?el('span.meta',{text:' · '+e.event}):null,e.errorId?el('span.eid',{text:' · '+e.errorId}):null])]);node.onclick=()=>openDrawer({title:e.message||'Запись журнала',subtitle:(e.source||'hub')+' · '+(e.event||e.level),json:pretty(e),body:el('pre.log',{text:pretty(e)})});return node}
function appendEntry(e){if(!matches(e)||seen.has(String(e.seq)))return;seen.add(String(e.seq));const atBottom=stream.scrollHeight-stream.scrollTop-stream.clientHeight<40;if(paused||(!follow&&!atBottom)){buffer.push(e);setText(newPill,buffer.length+' новых');newPill.classList.remove('is-hidden');return}list.append(line(e));while(list.children.length>600)list.firstChild.remove();if(follow||atBottom)stream.scrollTop=stream.scrollHeight}
function flush(){const data=buffer.splice(0);newPill.classList.add('is-hidden');data.forEach(appendEntry);stream.scrollTop=stream.scrollHeight}
async function load(){try{const r=await rpc('logs.tail',{limit:600,level:filter.level,source:filter.source||null,search:filter.search||null},{quiet:true});clear(list);seen.clear();(r.entries||[]).forEach(appendEntry);lastSeq=r.lastSeq||lastSeq;setText(meta,(r.buffered||0)+' в памяти · '+(r.path||''));stream.scrollTop=stream.scrollHeight}catch(e){reportError(e,'Логи')}}
export function mountLogs(host){disposed=false;seen.clear();buffer=[]
 const level=segmented([{id:'debug',label:'Все'},{id:'info',label:'Info+'},{id:'warn',label:'Warn+'},{id:'error',label:'Ошибки'}],'debug',v=>{filter.level=v;load()})
 const source=el('input',{type:'text',placeholder:'Источник','aria-label':'Источник'});source.oninput=debounce(()=>{filter.source=source.value.trim();load()},300)
 const search=el('input',{type:'search',placeholder:'Поиск в сообщениях…'});search.oninput=debounce(()=>{filter.search=search.value.trim();load()},300)
 const followBtn=pill('Следить',true,v=>{follow=v;if(v)flush()});const pauseBtn=pill('Пауза',false,v=>{paused=v;if(!v)flush()})
 const save=el('button.btn.ghost.small',{type:'button',onclick:async()=>{const r=await rpc('logs.file',{lines:2000});download('hub-log.jsonl',r.text||'','application/x-ndjson')}},'Скачать JSONL')
 const browser=el('button.btn.ghost.small',{type:'button',onclick:()=>download('browser-diagnostics.json',diag.dump(),'application/json')},'Диагностика браузера')
 const clearBtn=el('button.btn.danger.small',{type:'button',onclick:async()=>{if(await confirmDialog('Очистить структурный журнал?',{okLabel:'Очистить'})){await rpc('logs.clear');toast('Логи очищены','ok');load()}}},'Очистить')
 stream=el('div.stream.tall');newPill=el('button.new-pill.is-hidden',{type:'button',onclick:flush});list=el('div');stream.append(newPill,list);meta=el('span',{text:'Загрузка…'})
 clear(host).append(card('Системный журнал','Сервер, RPC, процессы и ошибки браузера в одном потоке.',[save,browser,clearBtn],[el('div.filters',null,[level,source,search,followBtn,pauseBtn]),stream,el('div.stream-foot',null,[meta,el('span',{text:'Секреты маскируются перед записью'})])]))
 load();return{destroy(){disposed=true},onEntry(e){if(!disposed){lastSeq=Math.max(lastSeq,e.seq||0);appendEntry(e)}},onClear(){if(!disposed){clear(list);seen.clear()}},onOverflow(e){if(!disposed)setText(meta,'Поток перегружен · пропущено '+(e.dropped||'?'))}}
}
