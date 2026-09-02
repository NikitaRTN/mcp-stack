import { el, clear, setText, fmtTime, fmtDateTime, fmtMs, debounce, pretty, download } from './util.js'
import { rpc } from './api.js'
import { card, empty, statusBadge, openDrawer, reportError, confirmDialog, toast, segmented, pill } from './ui.js'

const rows = new Map()
let root, body, stream, newButton, countText, follow = true, buffered = 0
let filters = { status: 'all', tool: '', search: '' }
let disposed = false
// ID выбранного сервиса (null — общий журнал). Доступен всем функциям модуля.
let activeService = null

function cell(cls, text) { return el('td' + (cls ? '.' + cls : ''), { text }) }
function statusKind(s) { return ['error','timeout'].includes(s) ? 'is-failed' : s === 'running' ? 'is-running' : '' }

function patchRow(row, call, fresh=false) {
  row.className = 'clickable ' + statusKind(call.status) + (fresh ? ' is-new' : '')
  row.dataset.id = call.id
  const values = [fmtTime(call.started), call.service || '—', call.tool || call.method || '—', call.status || '—', fmtMs(call.duration_ms ?? call.durationMs)]
  values.forEach((v,i)=>setText(row.children[i],v))
  row.onclick = () => showDetail(call.id)
}

function makeRow(call, fresh=false) {
  const row = el('tr', null, [cell('num',''),cell('',''),cell('nowrap',''),cell('',''),cell('num','')])
  patchRow(row,call,fresh); rows.set(String(call.id),row); return row
}

function replaceCalls(calls) {
  // Дополнительная защита UI: сервисная вкладка никогда не показывает чужие строки,
  // даже если старый backend или кэш вернул общий список.
  const scoped = activeService ? calls.filter(call => call.service === activeService) : calls
  const top = stream.scrollTop
  const wanted = new Set(scoped.map(c=>String(c.id)))
  for (const [id,row] of rows) if (!wanted.has(id)) { row.remove(); rows.delete(id) }
  const frag = document.createDocumentFragment()
  for (const call of scoped) {
    const id=String(call.id); let row=rows.get(id)
    if (!row) row=makeRow(call)
    else patchRow(row,call)
    frag.append(row)
  }
  body.append(frag)
  stream.scrollTop = top
  setText(countText, scoped.length + ' записей')
}

async function load() {
  try {
    const result=await rpc('calls.list',{limit:300,service:activeService||null,status:filters.status==='all'?null:filters.status,tool:filters.tool||null,search:filters.search||null},{quiet:true})
    if (!disposed) replaceCalls(result.calls||[])
  } catch(e) { reportError(e,'Журнал вызовов') }
}

async function showDetail(id) {
  try {
    const result=await rpc('calls.detail',{id})
    const c=result.call||{}
    openDrawer({title:(c.tool||c.method||'Вызов')+' · #'+id,subtitle:fmtDateTime(c.started),json:pretty(c),body:el('div',null,[
      el('div.row-actions',null,[statusBadge(c.status)]),
      el('pre.log',{text:pretty(c)})
    ])})
  } catch(e) { reportError(e,'Детали вызова') }
}

export function mountCalls(host, service=null) {
  disposed=false; rows.clear(); filters={status:'all',tool:'',search:''}; activeService=service||null; buffered=0
  const seg=segmented([{id:'all',label:'Все'},{id:'ok',label:'Успешные'},{id:'failed',label:'Ошибки'},{id:'running',label:'Активные'}],'all',v=>{filters.status=v;load()})
  const search=el('input',{type:'search',placeholder:'Метод, инструмент, ошибка…','aria-label':'Поиск'})
  search.oninput=debounce(()=>{filters.search=search.value.trim();load()},300)
  const tool=el('input',{type:'text',placeholder:'Инструмент','aria-label':'Фильтр инструмента'})
  tool.oninput=debounce(()=>{filters.tool=tool.value.trim();load()},300)
  const followPill=pill('Следить',true,v=>{follow=v})
  const exportBtn=el('button.btn.ghost.small',{type:'button',onclick:async()=>{const r=await rpc('calls.list',{limit:1000,service});download('mcp-calls.ndjson',(r.calls||[]).map(JSON.stringify).join('\n'),'application/x-ndjson')}},'Скачать NDJSON')
  const purge=el('button.btn.danger.small',{type:'button',onclick:async()=>{if(await confirmDialog('Удалить записи журнала вызовов?',{okLabel:'Удалить'})){await rpc('calls.purge',{service});toast('Журнал очищен','ok');load()}}},'Очистить')
  stream=el('div.stream.tall.calls-stream')
  newButton=el('button.new-pill.is-hidden',{type:'button',onclick:()=>{buffered=0;newButton.classList.add('is-hidden');stream.scrollTop=0;load()}},'Новые записи')
  body=el('tbody')
  const table=el('table.calls-table',null,[el('colgroup',null,[el('col',{style:'width:14%'}),el('col',{style:'width:18%'}),el('col'),el('col',{style:'width:16%'}),el('col',{style:'width:14%'})]),el('thead',null,el('tr',null,['Время','Сервис','Метод / инструмент','Статус','Длительность'].map(x=>el('th',{text:x})))),body])
  stream.append(newButton,table)
  countText=el('span',{text:'Загрузка…'})
  root=card(service?'Логи выбранного MCP':'Журнал MCP',service?'Показываются только вызовы выбранного сервиса.':'Показываются вызовы всех MCP-сервисов.',[exportBtn,purge],[el('div.filters',null,[seg,tool,search,followPill]),stream,el('div.stream-foot',null,[countText,el('span',{text:'До 300 строк в DOM'})])])
  clear(host).append(root); load()
  return {destroy(){disposed=true},onStarted(call){if(service&&call.service!==service)return;if(follow&&stream.scrollTop<12&&!filters.search&&!filters.tool&&filters.status==='all'){body.prepend(makeRow({...call,status:'running',started:Date.now()/1000},true));while(body.children.length>300)body.lastChild.remove()}else{buffered++;setText(newButton,buffered+' новых');newButton.classList.remove('is-hidden')}},onFinished(call){if(service&&call.service!==service)return;const row=rows.get(String(call.id));if(row)patchRow(row,{...call,duration_ms:call.durationMs});else if(follow&&stream.scrollTop<12)load()}}
}
