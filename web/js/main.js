import { $, el, clear, setText, store, copy, pretty, fmtNum, fmtMs, fmtPct, fmtAgo, fmtBytes } from './util.js'
import { rpc, auth, session, withBusy } from './api.js'
import { diag, installGlobalHandlers } from './diag.js'
import { EventStream, createScheduler } from './stream.js'
import { toast, reportError, banner, dismissBanner, installOverlayHandlers, metric, badge, card, empty, sparkline, confirmDialog, openDrawer, drawerJson } from './ui.js'
import { mountCalls } from './calls.js?v=20260901-0250'
import { mountLogs } from './logs.js'
import { mountStudio, selectStudioService } from './studio.js'
import { mountSettings } from './settings.js'

installGlobalHandlers(); installOverlayHandlers()
diag.setSender(params=>rpc('client.log',params,{retries:0,quiet:true}))

const app={state:null,route:'overview',view:null,stream:new EventStream(),online:true}
const routes=[['overview','Обзор'],['studio','MCP Studio'],['calls','Журнал MCP'],['components','Компоненты'],['routes','Маршруты и токен'],['settings','Настройки'],['logs','Логи']]

function showGate(setup=false){$('#app').classList.add('is-hidden');$('#gate').classList.remove('is-hidden');$('#gate-form').dataset.mode=setup?'setup':'login';setText($('#gate-title'),setup?'Первый запуск':'Вход в панель');setText($('#gate-sub'),setup?'Придумайте пароль администратора — минимум 8 символов.':'Панель доступна локально или через ваш домен.');$('#gate-repeat-wrap').classList.toggle('is-hidden',!setup);setText($('#gate-go'),setup?'Создать и войти':'Войти');$('#gate-error').classList.add('is-hidden');setTimeout(()=>$('#gate-pass').focus(),0)}
function showApp(){ $('#gate').classList.add('is-hidden');$('#app').classList.remove('is-hidden') }

$('#gate-form').onsubmit=async e=>{e.preventDefault();const setup=e.currentTarget.dataset.mode==='setup',pass=$('#gate-pass').value,repeat=$('#gate-repeat').value,err=$('#gate-error');err.classList.add('is-hidden');if(setup&&pass!==repeat){setText(err,'Пароли не совпадают');err.classList.remove('is-hidden');return}try{await withBusy($('#gate-go'),()=>auth(setup?'setup':'login',{username:$('#gate-user').value||'admin',password:pass}));$('#gate-pass').value='';$('#gate-repeat').value='';showApp();await refresh(true);app.stream.connect(true)}catch(x){setText(err,x.human||x.message);err.classList.remove('is-hidden')}}
$('#logout').onclick=async()=>{try{await auth('logout')}finally{app.stream.stop();showGate(false)}}
window.addEventListener('hub:auth-required',()=>showGate(false))
window.addEventListener('hub:offline',()=>{app.online=false;banner('offline',{kind:'bad',text:'Связь с панелью потеряна. Данные могут быть устаревшими.',action:{label:'Повторить',run:()=>refresh(true)}})})
window.addEventListener('hub:online',()=>{app.online=true;dismissBanner('offline')})

function nav(){const host=clear($('#nav'));host.append(el('div.nav-group',{text:'Панель'}));for(const [id,label] of routes)host.append(navButton(id,label));if(app.state?.services?.length){host.append(el('div.nav-group',{text:'MCP-сервисы'}));for(const s of app.state.services)host.append(navButton('svc:'+s.id,s.label||s.id,s.status?.state))}}
function navButton(id,label,state){const b=el('button.nav-item'+(app.route===id?'.active':''),{type:'button',dataset:{route:id},onclick:()=>go(id)},[state?el('span.dot.'+(state||'off')):null,el('span.label',{text:label})]);return b}
function go(route){location.hash='#/'+route}
window.addEventListener('hashchange',()=>{app.route=(location.hash.replace(/^#\/?/,'')||'overview');render()})

async function refresh(force=false){try{const state=await rpc('state.get',{components:true},{quiet:!force});app.state=state;setText($('#chip-domain'),state.domain||'локально');nav();render(true)}catch(e){reportError(e,'Обновление состояния')}}
const scheduled=createScheduler(()=>refresh(false),{name:'state',debounce:250,minInterval:1200})

function title(name,sub=''){setText($('#page-title'),name);setText($('#page-sub'),sub)}
function destroyView(){if(app.view?.destroy)app.view.destroy();app.view=null}
function render(patch=false){if(!app.state)return;destroyView();nav();const host=$('#view'),r=app.route;if(r==='studio'){title('MCP Studio','Создание, OAuth и проверка tools');app.view=mountStudio(host,{getState:()=>app.state,refresh,go});return}if(r==='calls'){title('Журнал MCP','Вызовы инструментов и ошибки');app.view=mountCalls(host);return}if(r==='logs'){title('Логи','Подробная диагностика в реальном времени');app.view=mountLogs(host);return}if(r.startsWith('svc:')){renderService(host,r.slice(4));return}if(r==='components'){renderComponents(host);return}if(r==='routes'){renderRoutes(host);return}if(r==='settings'){title('Настройки','Порты, сеть и Windows Firewall');app.view=mountSettings(host,{getState:()=>app.state,refresh});return}renderOverview(host)}

function renderProblems(){const host=clear($('#problems')),items=app.state.problems||[];host.classList.toggle('is-hidden',!items.length);for(const p of items){const action=p.action?el('button.btn.ghost.small',{type:'button',onclick:async()=>{try{await rpc(p.action.method,p.action.params||{});toast('Действие выполнено','ok');refresh(true)}catch(e){reportError(e)} }},p.action.label||'Исправить'):null;host.append(el('div.problem.'+(p.level==='warn'?'warn':''),null,[el('span.text',{text:p.text||p.message||p.detail||'Обнаружена проблема'}),action]))}}
function renderOverview(host){title('Обзор','Состояние MCP Hub');renderProblems();const s=app.state,t=s.totals||{},services=s.services||[];const metrics=el('div.grid.cols-4',null,[metric('Включено',`${s.enabledCount||0} / ${services.length}`,'MCP-сервисов'),metric('Вызовы за час',fmtNum(t.calls1h||0),'все сервисы'),metric('Ошибки',fmtNum(t.failed1h||0),fmtPct(t.errorRate||0),t.failed1h?'bad':'ok'),metric('P95',fmtMs(t.p95Ms),'задержка')]);const serviceCards=services.map(v=>{const st=v.status?.state||'off';return el('button.card',{type:'button',onclick:()=>go('svc:'+v.id),style:{textAlign:'left',cursor:'pointer'}},[el('div.svc-head',null,[el('div.svc-title',null,[el('span.dot.'+st),el('div',null,[el('h2',{text:v.label||v.id}),el('p.muted',{text:v.status?.detail||v.note||''})])]),badge(v.enabled?(st==='up'?'работает':st):'выключен',st==='up'?'ok':v.enabled?'warn':'off')]),el('div.grid.cols-3',null,[metric('Вызовы',fmtNum(v.metrics?.calls1h||0),'1 час'),metric('Ошибки',fmtNum(v.metrics?.failed1h||0),'1 час'),metric('P95',fmtMs(v.metrics?.p95Ms),'')])])});clear(host).append(metrics,el('div.grid.cols-2',null,serviceCards.length?serviceCards:[empty('Сервисы ещё не добавлены')]))}

function svcOpButton(label,method,id,kind='ghost'){return el('button.btn.'+kind+'.small',{type:'button',onclick:async e=>{try{await withBusy(e.currentTarget,()=>rpc(method,{id}));toast(label+' — готово','ok');refresh(true)}catch(x){reportError(x,label)}}},label)}
function renderService(host,id){const s=(app.state.services||[]).find(x=>x.id===id);if(!s){go('overview');return}title(s.label||id,s.note||'MCP-сервис');const enabled=el('input',{type:'checkbox'});enabled.checked=!!s.enabled;enabled.onchange=async()=>{enabled.disabled=true;try{await rpc('service.setEnabled',{id,enabled:enabled.checked});toast(enabled.checked?'Сервис включён':'Сервис выключен','ok');refresh(true)}catch(e){enabled.checked=!enabled.checked;reportError(e)}finally{enabled.disabled=false}};const toggle=el('label.switch',null,[enabled,el('span.track'),el('span',{text:s.enabled?'Включён':'Выключен'})]);const edit=el('button.btn.ghost.small',{type:'button',onclick:()=>{selectStudioService(id);go('studio')}},'Редактировать в Studio');const actions=[toggle,edit,svcOpButton('Запустить','service.start',id),svcOpButton('Перезапустить','service.restart',id),svcOpButton('Остановить','service.stop',id,'danger')];const info=card('Состояние',s.status?.detail||'',actions,[el('div.grid.cols-3',null,[metric('Статус',s.status?.state||'—',''),metric('Вызовы / ч',fmtNum(s.metrics?.calls1h||0),'Ошибок '+fmtNum(s.metrics?.failed1h||0)),metric('Последний вызов',fmtAgo(s.metrics?.lastCallAt),'')]),s.url?el('div.url-row',null,[el('span.url',{text:s.url}),el('button.btn.ghost.small',{onclick:()=>copy(s.url)},'Копировать')]):null]);clear(host).append(info,el('div',{id:'svc-calls'}));app.view=mountCalls($('#svc-calls'),id)}

const componentVisuals={
	caddy:{icon:'C',group:'Сеть и HTTPS'},
	node:{icon:'JS',group:'Среда выполнения'},
	npx:{icon:'>_',group:'Среда выполнения'},
	supergateway:{icon:'SG',group:'HTTP-мост'},
	'desktop-commander':{icon:'DC',group:'MCP-сервер'},
}
const expandedComponents=new Set()
const installState={
	jobId:null,component:null,label:'',status:'idle',percent:0,lines:[],message:'',detail:'',
	phase:'idle',indeterminate:false,downloadedBytes:0,totalBytes:0,speedBps:0,elapsedSec:0,
	pollTimer:null,notifiedJob:null,dismissedJob:null,
}

function componentDependencyChip(item){
	return el('span.component-dependency'+(item?.found?'.ready':'.missing'),null,[
		el('span.component-dependency-mark',{text:item?.found?'✓':'!','aria-hidden':'true'}),
		el('span',{text:item?.name||item?.id||'неизвестно'}),
	])
}
function componentField(label,value,mono=false){return el('div.component-field',null,[el('span.component-field-label',{text:label}),el('span.component-field-value'+(mono?'.mono':''),{text:value||'—'})])}
function componentInstallTarget(item,byId){
	if(item.installable)return item
	if(item.providedBy&&byId.get(item.providedBy)?.installable)return byId.get(item.providedBy)
	for(const id of item.dependsOn||[]){const dep=byId.get(id);if(!dep?.found&&dep?.installable)return dep;if(!dep?.found&&dep?.providedBy&&byId.get(dep.providedBy)?.installable)return byId.get(dep.providedBy)}
	return null
}
function phaseLabel(phase){return({starting:'Подготовка',download:'Скачивание',install:'Установка',verify:'Проверка',done:'Завершено'})[phase]||'Установка'}
function progressLabel(){
	if(installState.status==='ok')return '100%'
	if(installState.status==='error')return 'Ошибка'
	if(installState.status!=='running')return 'Ожидание'
	return installState.indeterminate?'Выполняется':Math.max(0,Math.min(100,installState.percent||0))+'%'
}
function transferLabel(){
	const parts=[]
	if(installState.downloadedBytes){parts.push(installState.totalBytes?fmtBytes(installState.downloadedBytes)+' / '+fmtBytes(installState.totalBytes):fmtBytes(installState.downloadedBytes))}
	if(installState.speedBps)parts.push(fmtBytes(installState.speedBps)+'/с')
	if(installState.elapsedSec)parts.push(installState.elapsedSec+' с')
	return parts.join(' · ')||phaseLabel(installState.phase)
}
function statusVisual(){
	if(installState.status==='ok')return['Готово','ok']
	if(installState.status==='error')return['Ошибка','bad']
	if(installState.status==='running')return['В процессе','info']
	return['Ожидание','off']
}
function setProgress(node){
	if(!node)return
	const running=installState.status==='running',indeterminate=running&&installState.indeterminate
	node.classList.toggle('running',running);node.classList.toggle('indeterminate',indeterminate);node.classList.toggle('complete',installState.status==='ok');node.classList.toggle('failed',installState.status==='error')
	node.setAttribute('aria-valuenow',String(installState.percent||0));node.setAttribute('aria-valuetext',progressLabel())
	const bar=node.querySelector('i');if(bar)bar.style.width=Math.max(0,Math.min(100,installState.percent||0))+'%'
}
function ensureInstallDock(){
	let dock=$('#install-dock');if(dock)return dock
	dock=el('aside.install-dock.is-hidden',{id:'install-dock','aria-live':'polite','aria-label':'Ход установки'},[
		el('div.install-dock-head',null,[
			el('span.install-activity',{id:'install-dock-activity','aria-hidden':'true'}),
			el('div.install-dock-copy',null,[el('span.install-dock-kicker',{id:'install-dock-phase',text:'Установка'}),el('strong',{id:'install-dock-title',text:'Компонент'})]),
			badge('В процессе','info'),
			el('button.install-dock-close',{id:'install-dock-close',type:'button','aria-label':'Скрыть индикатор',onclick:()=>{if(installState.status!=='running'){installState.dismissedJob=installState.jobId;dock.classList.add('is-hidden');document.body.classList.remove('has-install-dock')}}},'✕'),
		]),
		el('p.install-dock-detail',{id:'install-dock-detail',text:'Подготовка…'}),
		el('div.progress.install-dock-progress',{id:'install-dock-progress',role:'progressbar','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':'0'},el('i')),
		el('div.install-dock-foot',null,[el('span.install-transfer',{id:'install-dock-transfer',text:'Подготовка'}),el('strong.install-percent',{id:'install-dock-percent',text:'0%'}),el('button.btn.ghost.small',{type:'button',onclick:()=>{go('components');setTimeout(()=>$('#component-install-panel')?.scrollIntoView({behavior:'smooth',block:'center'}),120)}},'Открыть журнал')]),
	])
	document.body.append(dock)
	return dock
}
function applyInstallUpdate(event){
	if(!event)return false
	if(installState.jobId&&event.jobId&&event.jobId!==installState.jobId)return false
	if(installState.status!=='running'&&installState.component&&event.component&&event.component!==installState.component)return false
	for(const key of ['jobId','component','status','detail','phase','indeterminate','downloadedBytes','totalBytes','speedBps','elapsedSec'])if(event[key]!==undefined&&event[key]!==null)installState[key]=event[key]
	if(event.percent!==undefined&&event.percent!==null)installState.percent=event.percent
	if(Array.isArray(event.lines))installState.lines=event.lines.slice(-200)
	if(event.line&&!installState.lines.includes(event.line)){installState.lines.push(event.line);installState.lines=installState.lines.slice(-200)}
	if(event.message)installState.message=event.message
	const item=(app.state?.components?.components||[]).find(x=>x.id===installState.component);if(item)installState.label=item.name
	syncInstallVisuals();return true
}
function stopInstallPolling(){if(installState.pollTimer){clearTimeout(installState.pollTimer);installState.pollTimer=null}}
function startInstallPolling(){
	stopInstallPolling()
	const poll=async()=>{
		if(installState.status!=='running'||!installState.jobId)return
		try{const result=await rpc('install.job',{jobId:installState.jobId},{timeout:6000,retries:0,quiet:true}),job=result.job||{};applyInstallUpdate(job);if(job.status&&job.status!=='running'){onInstallFinished({...job,message:job.detail});return}}catch(error){diag.warn('Не удалось обновить прогресс установки',{event:'install.pollFailed',message:error.message})}
		if(installState.status==='running')installState.pollTimer=setTimeout(poll,850)
	}
	installState.pollTimer=setTimeout(poll,500)
}
function inlineProgress(item){
	return el('div.component-inline-progress.is-hidden',{dataset:{inlineProgress:item.id},'aria-live':'polite'},[
		el('div.component-inline-head',null,[el('span.install-activity',{'aria-hidden':'true'}),el('span.component-inline-detail',{text:'Подготовка…'}),el('strong.component-inline-percent',{text:'0%'})]),
		el('div.progress.component-inline-bar',{role:'progressbar','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':'0'},el('i')),
		el('div.component-inline-meta',{text:'Ожидание данных'}),
	])
}
function componentCard(item,byId){
	const visual=componentVisuals[item.id]||{icon:(item.name||'?').slice(0,2).toUpperCase(),group:'Компонент'}
	const expanded=expandedComponents.has(item.id)
	const dependencies=(item.dependsOn||[]).map(id=>byId.get(id)||{id,name:id,found:false})
	const status=item.found?badge('Установлен','ok'):item.required?badge('Требуется','bad'):badge('Не установлен','off')
	const detail=el('div.component-detail'+(expanded?'':'.is-hidden'),{id:'component-detail-'+item.id},[
		el('p.component-description',{text:item.detail||item.note||item.purpose||''}),
		el('div.component-fields',null,[componentField('Версия',item.version||'не определена',true),componentField('Размер',item.sizeHint||'—'),componentField('Путь',item.path||'появится после установки',true)]),
		el('div.component-dependency-row',null,[el('span.component-field-label',{text:'Зависимости'}),dependencies.length?el('div.component-dependencies',null,dependencies.map(componentDependencyChip)):el('span.component-no-deps',{text:'Нет'})]),
	])
	const toggle=el('button.btn.ghost.small.component-detail-toggle.component-action-detail',{type:'button','aria-expanded':expanded?'true':'false','aria-controls':'component-detail-'+item.id,onclick:()=>{const next=!expandedComponents.has(item.id);if(next)expandedComponents.add(item.id);else expandedComponents.delete(item.id);detail.classList.toggle('is-hidden',!next);toggle.setAttribute('aria-expanded',String(next));setText(toggle,next?'Скрыть детали':'Подробнее')}},expanded?'Скрыть детали':'Подробнее')
	const target=componentInstallTarget(item,byId),actions=[toggle]
	if(target){
		const own=target.id===item.id,label=own?(item.found?'Проверить':'Установить'):'Установить '+target.name
		const checkOnly=own&&item.found
		const button=el('button.btn.primary.small.component-action-install',{type:'button',dataset:{installComponent:target.id,idleLabel:label},disabled:installState.status==='running',onclick:()=>checkOnly?checkInstalledComponent(item,button):startComponentInstall(target,button)},label)
		actions.unshift(button)
	}
	if(item.downloadUrl)actions.push(el('a.btn.ghost.small.component-action-source',{href:item.downloadUrl,target:'_blank',rel:'noopener noreferrer'},'Страница пакета'))
	return el('article.component-card.'+(item.found?'installed':item.required?'required':'missing'),{dataset:{component:item.id}},[
		el('div.component-card-main',null,[el('div.component-icon',{text:visual.icon,'aria-hidden':'true'}),el('div.component-copy',null,[el('div.component-eyebrow',{text:visual.group}),el('h2',{text:item.name}),el('p.muted',{text:item.purpose||''})]),status]),
		inlineProgress(item),detail,el('div.component-card-actions',null,actions),
	])
}
function installPanel(){
	const idle='Выберите компонент и нажмите «Установить». Здесь появятся этап, скорость и журнал.'
	return el('section.component-install-panel',{id:'component-install-panel','aria-live':'polite'},[
		el('div.component-install-head',null,[el('div',null,[el('div.component-eyebrow',{text:'Установка'}),el('h2',{id:'component-install-title',text:installState.label||'Журнал установки'}),el('p.muted',{id:'component-install-message',text:installState.detail||idle})]),badge('Ожидание','off')]),
		el('div.component-progress-readout',null,[el('span.install-activity',{'aria-hidden':'true'}),el('span',{id:'component-install-phase',text:'Ожидание'}),el('span.install-transfer',{id:'component-install-transfer',text:''}),el('strong.install-percent',{id:'component-install-percent',text:'0%'})]),
		el('div.progress.component-progress',{id:'component-install-progress',role:'progressbar','aria-valuemin':'0','aria-valuemax':'100','aria-valuenow':String(installState.percent||0)},el('i',{id:'component-install-bar',style:{width:(installState.percent||0)+'%'}})),
		el('pre.log.component-install-log',{id:'component-install-log',text:installState.lines.length?installState.lines.join('\n'):idle}),
	])
}
function syncInstallVisuals(){
	const running=installState.status==='running',activeId=installState.component,status=statusVisual(),detail=installState.detail||installState.message||(running?'Компонент скачивается и устанавливается…':'Выберите компонент для установки.'),transfer=transferLabel(),percent=progressLabel()
	const panel=$('#component-install-panel')
	if(panel){
		setText($('#component-install-title'),installState.label||'Журнал установки');setText($('#component-install-message'),detail);setText($('#component-install-phase'),phaseLabel(installState.phase));setText($('#component-install-transfer'),transfer);setText($('#component-install-percent'),percent)
		const log=$('#component-install-log');setText(log,installState.lines.length?installState.lines.join('\n'):'Журнал пока пуст.');if(log)log.scrollTop=log.scrollHeight
		setProgress($('#component-install-progress'));const stateBadge=panel.querySelector('.component-install-head .badge');if(stateBadge){stateBadge.className='badge '+status[1];setText(stateBadge,status[0])}
		panel.querySelector('.install-activity')?.classList.toggle('active',running)
	}
	document.querySelectorAll('.component-card').forEach(card=>card.classList.toggle('installing',running&&card.dataset.component===activeId))
	document.querySelectorAll('[data-inline-progress]').forEach(node=>{
		const active=running&&node.dataset.inlineProgress===activeId;node.classList.toggle('is-hidden',!active)
		if(active){setText(node.querySelector('.component-inline-detail'),detail);setText(node.querySelector('.component-inline-percent'),percent);setText(node.querySelector('.component-inline-meta'),transfer);setProgress(node.querySelector('[role="progressbar"]'));node.querySelector('.install-activity')?.classList.add('active')}
	})
	document.querySelectorAll('[data-install-component]').forEach(button=>{const active=running&&button.dataset.installComponent===activeId;button.disabled=running;button.classList.toggle('installing-button',active);setText(button,active?'Скачивается…':button.dataset.idleLabel)})
	const dock=ensureInstallDock(),show=installState.status!=='idle'&&installState.dismissedJob!==installState.jobId;dock.classList.toggle('is-hidden',!show);document.body.classList.toggle('has-install-dock',show)
	if(show){setText($('#install-dock-title'),installState.label||'Компонент');setText($('#install-dock-phase'),phaseLabel(installState.phase));setText($('#install-dock-detail'),detail);setText($('#install-dock-transfer'),transfer);setText($('#install-dock-percent'),percent);setProgress($('#install-dock-progress'));const dockBadge=dock.querySelector('.badge');dockBadge.className='badge '+status[1];setText(dockBadge,status[0]);$('#install-dock-close').disabled=running;$('#install-dock-activity').classList.toggle('active',running)}
}
async function checkInstalledComponent(item,button){
	if(installState.status==='running'){toast('Дождитесь завершения текущей установки','warn');return}
	try{
		const summary=await withBusy(button,()=>rpc('install.detect',{}, {quiet:true}))
		const fresh=(summary.components||[]).find(component=>component.id===item.id)
		if(app.state)app.state.components=summary
		if(fresh?.found){const version=fresh.version?' · '+fresh.version:'';toast('Проверка пройдена: '+item.name+' найден'+version,'ok')}
		else toast(item.name+' не найден — теперь доступна установка','warn')
		render(true)
	}catch(error){reportError(error,'Проверка '+item.name)}
}
async function startComponentInstall(item,button){
	if(installState.status==='running'){toast('Дождитесь завершения текущей установки','warn');return}
	stopInstallPolling();Object.assign(installState,{jobId:null,component:item.id,label:item.name,status:'running',percent:2,lines:[],message:'',detail:'Подготавливаю загрузку…',phase:'starting',indeterminate:true,downloadedBytes:0,totalBytes:0,speedBps:0,elapsedSec:0,notifiedJob:null,dismissedJob:null})
	syncInstallVisuals()
	try{
		button.classList.add('busy');button.disabled=true
		const result=await rpc('install.component',{component:item.id},{timeout:20000}),job=result.job||{}
		button.classList.remove('busy');applyInstallUpdate(job);installState.status=job.status||'running';installState.detail=job.detail||'Установка выполняется в фоне…';syncInstallVisuals();if(installState.status==='running')startInstallPolling();else onInstallFinished({...job,message:job.detail})
	}catch(error){button.classList.remove('busy');installState.status='error';installState.indeterminate=false;installState.detail=error.human||error.message;installState.message=installState.detail;installState.lines.push('Ошибка: '+installState.detail);syncInstallVisuals();reportError(error,'Установка '+item.name)}
}
function onInstallProgress(event){applyInstallUpdate(event)}
function onInstallFinished(event){
	if(!applyInstallUpdate(event))return
	installState.status=event.status==='ok'?'ok':'error';installState.indeterminate=false;installState.percent=event.status==='ok'?100:(event.percent??installState.percent);installState.message=event.message||event.detail||'Установка завершена';installState.detail=installState.message;stopInstallPolling();syncInstallVisuals()
	if(installState.notifiedJob!==installState.jobId){installState.notifiedJob=installState.jobId;toast(installState.message,installState.status==='ok'?'ok':'bad')}
}
function renderComponents(host){
	title('Компоненты','Проверка и отдельная установка каждой зависимости')
	const summary=app.state.components||{},items=summary.components||[],byId=new Map(items.map(item=>[item.id,item]))
	const installed=items.filter(item=>item.found).length,requiredMissing=items.filter(item=>item.required&&!item.found).length
	const detectButton=el('button.btn.ghost',{type:'button',onclick:async event=>{try{await withBusy(event.currentTarget,()=>refresh(true));toast('Список компонентов обновлён','ok')}catch(error){reportError(error,'Проверка компонентов')}}},'Перепроверить')
	const overview=el('section.component-overview',null,[el('div.component-overview-copy',null,[el('div.component-eyebrow',{text:'Локальная среда'}),el('h2',{text:'Каждый компонент — отдельно'}),el('p.muted',{text:'Во время установки прогресс виден прямо в карточке и в закреплённом индикаторе.'})]),el('div.component-overview-stats',null,[el('div.component-stat',null,[el('strong',{text:String(installed)}),el('span',{text:'установлено'})]),el('div.component-stat'+(requiredMissing?'.bad':''),null,[el('strong',{text:String(items.length-installed)}),el('span',{text:'не найдено'})])]),detectButton])
	const grid=items.length?el('div.component-grid',null,items.map(item=>componentCard(item,byId))):empty('Не удалось получить список компонентов')
	clear(host).append(el('div.components-shell',null,[overview,grid,installPanel()]))
	app.view={onInstallProgress,onInstallFinished};syncInstallVisuals()
}

function renderRoutes(host){title('Маршруты и токен','Публичные адреса MCP');const s=app.state;const rows=(s.routes||[]).map(r=>el('div.url-row',null,[el('span.badge.info',{text:r.service||'MCP'}),el('span.url',{text:r.url||r.path||pretty(r)}),r.url?el('button.btn.ghost.small',{onclick:()=>copy(r.url)},'Копировать'):null]));const tok=s.token||'';clear(host).append(card('Маршруты','Доступны только для включённых сервисов',null,rows.length?rows:empty('Нет опубликованных маршрутов')),card('Токен доступа','Не передавайте токен посторонним',[el('button.btn.ghost.small',{onclick:()=>copy(tok)},'Копировать'),el('button.btn.danger.small',{onclick:async()=>{if(await confirmDialog('Старый токен перестанет работать. Выпустить новый?',{okLabel:'Выпустить'})){await rpc('token.rotate');toast('Токен обновлён','ok');refresh(true)}}},'Сменить')],[el('pre.log',{text:tok||'—'})]))}
function renderSettings(host){title('Настройки','Основная конфигурация панели');const s=app.state;clear(host).append(card('Текущие настройки','Изменение сервисов и расширенные параметры остаются доступны через config/hub.json',null,el('dl.kv',null,[el('dt',{text:'Домен'}),el('dd',{text:s.domain||'—'}),el('dt',{text:'HTTPS-порт'}),el('dd',{text:s.httpsPort||'—'}),el('dt',{text:'Порт панели'}),el('dd',{text:s.adminPort||'—'}),el('dt',{text:'Автоперезапуск'}),el('dd',{text:s.autoRestart?'включён':'выключен'}),el('dt',{text:'Хранение телеметрии'}),el('dd',{text:(s.telemetryDays||0)+' дней'})])),card('Пароль администратора','Задайте новый пароль длиной от 8 символов',null,passwordForm()))}
function passwordForm(){const old=el('input',{type:'password',autocomplete:'current-password'}),next=el('input',{type:'password',autocomplete:'new-password'}),btn=el('button.btn.primary',{type:'button',onclick:async()=>{try{await withBusy(btn,()=>auth('password',{current:old.value,password:next.value}));old.value='';next.value='';toast('Пароль изменён','ok')}catch(e){reportError(e,'Пароль')}}},'Изменить пароль');return el('div.form-grid',null,[el('label.field',null,['Текущий пароль',old]),el('label.field',null,['Новый пароль',next]),el('div.row-actions',null,[btn])])}

$('#btn-refresh').onclick=e=>withBusy(e.currentTarget,()=>refresh(true));$('#btn-health').onclick=async e=>{try{const r=await withBusy(e.currentTarget,()=>rpc('health.check'));openDrawer({title:'Проверка компонентов',subtitle:'Проверено сейчас',json:pretty(r),body:el('pre.log',{text:pretty(r)})})}catch(x){reportError(x,'Проверка')}}
$('#btn-theme').onclick=()=>{const now=document.documentElement.dataset.theme||'dark',next=now==='dark'?'light':'dark';document.documentElement.dataset.theme=next;store.set('theme',next)}
$('#drawer-copy').onclick=async()=>{const ok=await copy(drawerJson());toast(ok?'Скопировано':'Не удалось скопировать',ok?'ok':'bad')}
function sidebar(open){$('#sidebar').classList.toggle('open',open);$('#sidebar-scrim').hidden=!open}$('#sidebar-open').onclick=()=>sidebar(true);$('#sidebar-close').onclick=()=>sidebar(false);$('#sidebar-scrim').onclick=()=>sidebar(false);$('#nav').addEventListener('click',()=>sidebar(false))
document.addEventListener('keydown',e=>{if((e.key==='r'||e.key==='R')&&!/INPUT|TEXTAREA/.test(document.activeElement?.tagName)){e.preventDefault();refresh(true)}})

app.stream.onStatus(status=>{const n=$('#live-indicator');n.className='live '+(status==='online'?'online':status==='offline'?'offline':'pending');setText($('#live-text'),status==='online'?'события онлайн':status==='reconnecting'?'переподключение…':status==='offline'?'нет потока':'подключение…')})
for(const kind of ['state.dirty','service.state','service.toggled','service.changed','install.finished','service.giveup'])app.stream.on(kind,scheduled)
app.stream.on('install.progress',onInstallProgress);app.stream.on('install.finished',onInstallFinished)
app.stream.on('call.started',e=>app.view?.onStarted?.(e));app.stream.on('call.finished',e=>{app.view?.onFinished?.(e);scheduled()});app.stream.on('calls.purged',scheduled)
app.stream.on('log.entry',e=>app.view?.onEntry?.(e));app.stream.on('log.cleared',()=>app.view?.onClear?.());app.stream.on('log.overflow',e=>app.view?.onOverflow?.(e))

async function boot(){document.documentElement.dataset.theme=store.get('theme',matchMedia('(prefers-color-scheme: light)').matches?'light':'dark');app.route=location.hash.replace(/^#\/?/,'')||'overview';try{const s=await session();setText($('#app-version'),s.version?'v'+s.version:'');if(!s.authenticated){showGate(!!s.needsSetup);return}showApp();await refresh(true);app.stream.connect(true)}catch(e){showGate(false);setText($('#gate-error'),e.human||e.message);$('#gate-error').classList.remove('is-hidden')}}
boot()
