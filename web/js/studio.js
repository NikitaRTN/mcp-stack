import { el, clear, pretty, copy } from "./util.js"
import { rpc, withBusy } from "./api.js"
import { toast, reportError, badge, empty, confirmDialog } from "./ui.js"

const memory = {
	selectedId: null,
	tab: "tools",
	tools: [],
	selectedTool: null,
	toolArgs: {},
	toolResult: null,
	connection: null,
	connectionService: null,
	meta: null,
}

function option(value, label, selected = false) { return el("option", { value, selected }, label) }
function field(label, control, hint = "") { return el("label.field", null, [label, control, hint ? el("span.hint", { text: hint }) : null]) }
function input(name, value = "", attrs = {}) { return el("input", { name, value: value ?? "", ...attrs }) }
function select(name, value, options, attrs = {}) {
	return el("select", { name, ...attrs }, options.map(([id, label]) => option(id, label, id === value)))
}
function checkbox(name, checked) { const node = el("input", { name, type: "checkbox" }); node.checked = !!checked; return node }

function authLabel(mode) {
	return ({ token: "Токен Hub", oauth: "OAuth", none: "Открыт" })[mode] || mode || "Токен Hub"
}

function exampleFromSchema(schema, depth = 0) {
	if (!schema || depth > 4) return {}
	if (schema.default !== undefined) return schema.default
	if (Array.isArray(schema.examples) && schema.examples.length) return schema.examples[0]
	if (schema.type === "object" || schema.properties) {
		const out = {}
		for (const [key, value] of Object.entries(schema.properties || {})) {
			if ((schema.required || []).includes(key) || value.default !== undefined) out[key] = exampleFromSchema(value, depth + 1)
		}
		return out
	}
	if (schema.type === "array") return []
	if (schema.type === "boolean") return false
	if (schema.type === "integer" || schema.type === "number") return 0
	return ""
}

function serviceById(state, id) { return (state.services || []).find(service => service.id === id) }

export function selectStudioService(id) {
	memory.selectedId = id
	memory.tab = "config"
	memory.tools = []
	memory.selectedTool = null
	memory.connection = null
	memory.connectionService = null
}

export function mountStudio(host, { getState, refresh }) {
	let currentHost = host
	function choose(id, tab = memory.tab) {
		memory.selectedId = id
		memory.tab = tab
		memory.tools = []
		memory.selectedTool = null
		memory.toolResult = null
		memory.connection = null
		memory.connectionService = null
		render()
	}

	function render() {
		if (!currentHost) return
		const state = getState()
		const services = state.services || []
		if (!memory.selectedId || (memory.selectedId !== "__new__" && memory.selectedId !== "__manual__" && !serviceById(state, memory.selectedId))) {
			memory.selectedId = (services.find(service => service.id === "roblox") || services[0] || {}).id || "__manual__"
		}
		const selected = serviceById(state, memory.selectedId)
		const nav = el("aside.studio-services.card", null, [
			el("div.studio-services-head", null, [el("div", null, [el("h2", { text: "MCP-серверы" }), el("p.muted", { text: services.length + " настроено" })]), el("button.btn.primary.small", { type: "button", onclick: () => choose("__new__", "config") }, "+ Новый")]),
			el("div.studio-service-list", null, [
				...services.map(service => el("button.studio-service" + (memory.selectedId === service.id ? ".active" : ""), { type: "button", onclick: () => choose(service.id) }, [
					el("span.dot." + (service.status?.state || "off")),
					el("span.studio-service-copy", null, [el("strong", { text: service.label }), el("small", { text: service.url || service.path })]),
					badge(authLabel(service.authMode), service.authMode === "oauth" ? "info" : service.authMode === "none" ? "warn" : "off"),
				])),
				el("button.studio-service.studio-service-manual" + (memory.selectedId === "__manual__" ? ".active" : ""), { type: "button", onclick: () => choose("__manual__", "tools") }, [
					el("span.studio-manual-icon", { text: "URL", "aria-hidden": "true" }), el("span.studio-service-copy", null, [el("strong", { text: "Произвольный адрес" }), el("small", { text: "Например mcp.riseshield.ru/roblox" })]),
				]),
			]),
		])
		const tabs = el("div.studio-tabs", null, [
			el("button" + (memory.tab === "config" ? ".active" : ""), { type: "button", disabled: memory.selectedId === "__manual__", onclick: () => { memory.tab = "config"; render() } }, memory.selectedId === "__new__" ? "Новый MCP" : "Конфигурация"),
			el("button" + (memory.tab === "tools" ? ".active" : ""), { type: "button", disabled: memory.selectedId === "__new__", onclick: () => { memory.tab = "tools"; render() } }, "Tools Explorer"),
		])
		const content = memory.tab === "config" ? renderConfig(state, selected, render) : renderExplorer(state, selected, render)
		clear(currentHost).append(
			el("section.studio-hero", null, [
				el("div", null, [el("div.component-eyebrow", { text: "Developer Workspace" }), el("h2", { text: "Создавайте MCP и запускайте tools из одной панели" }), el("p.muted", { text: "Порты, команды, маршруты, OAuth и ответы JSON-RPC доступны без ручного редактирования конфигов." })]),
				el("div.studio-hero-stats", null, [badge(services.length + " MCP", "info"), badge(services.filter(item => item.enabled).length + " включено", "ok")]),
			]),
			el("div.studio-layout", null, [nav, el("section.studio-workspace.card", null, [tabs, content])]),
		)
	}

	async function saveService(form, service, rerender) {
		const data = new FormData(form)
		const kind = service?.builtin ? service.kind : data.get("kind")
		const payload = {
			id: data.get("id"), label: data.get("label"), note: data.get("note"), path: data.get("path"), kind,
				authMode: data.get("authMode"),
				oauth: {
					mode: data.get("oauthRouteMode"),
					introspectionUrl: data.get("oauthIntrospectionUrl"), clientId: data.get("oauthClientId"),
				clientSecret: data.get("oauthClientSecret"), requiredScopes: data.get("oauthRequiredScopes"),
				verifyTls: form.elements.oauthVerifyTls.checked,
			},
		}
		if (kind === "stdio") Object.assign(payload, { port: data.get("port"), command: data.get("command"), upstreamPath: data.get("upstreamPath") })
		else Object.assign(payload, {
			upstream: data.get("upstream"), upstreamAuthMode: data.get("upstreamAuthMode"), upstreamToken: data.get("upstreamToken"),
			upstreamOAuth: {
				tokenUrl: data.get("upstreamOAuthTokenUrl"), clientId: data.get("upstreamOAuthClientId"),
				clientSecret: data.get("upstreamOAuthClientSecret"), scope: data.get("upstreamOAuthScope"),
				audience: data.get("upstreamOAuthAudience"), verifyTls: form.elements.upstreamOAuthVerifyTls.checked,
			},
		})
		const button = form.querySelector('[data-save-service]')
		try {
			if (service) await withBusy(button, () => rpc("service.update", { id: service.id, patch: payload }, { timeout: 45000 }))
			else {
				const result = await withBusy(button, () => rpc("service.create", payload, { timeout: 30000 }))
				memory.selectedId = result.service.id
			}
			toast(service ? "MCP сохранён" : "MCP создан", "ok")
			await refresh(true)
		} catch (error) { reportError(error, service ? "Сохранение MCP" : "Создание MCP") }
	}

	function renderConfig(state, service, rerender) {
		const isNew = memory.selectedId === "__new__"
			const model = service || { id: "", label: "", note: "", path: "/new-mcp", kind: "remote", port: "", upstreamPath: "/mcp", upstream: "", authMode: "token", oauth: { mode: "builtin" }, upstreamAuthMode: "none", upstreamOAuth: {} }
		const form = el("form.studio-config")
		const kind = select("kind", model.kind || "remote", [["remote", "Streamable HTTP / удалённый"], ["stdio", "stdio-команда через supergateway"]], { disabled: !!service?.builtin })
			const authMode = select("authMode", model.authMode || "token", [["token", "Токен MCP Hub"], ["oauth", "OAuth для ChatGPT"], ["none", "Без авторизации"]])
			const upstreamAuth = select("upstreamAuthMode", model.upstreamAuthMode || "none", [["none", "Нет"], ["bearer", "Bearer token"], ["oauth", "OAuth Client Credentials"]])
			const oauthRouteMode = select("oauthRouteMode", model.oauth?.mode || (model.oauth?.introspectionUrl ? "introspection" : "builtin"), [["builtin", "Автоматически — встроенный OAuth"], ["introspection", "Внешний OAuth — расширенный режим"]])
			const localDomain = !state.domain || ["localhost", "127.0.0.1"].includes(state.domain)
			const oauthBase = localDomain ? `http://localhost:${state.httpsPort || 8443}` : "https://" + state.domain
			const oauthResource = model.url || oauthBase + (model.path || "/new-mcp")
			const builtinOauth = el("div.oauth-auto-card", { dataset: { oauthBuiltin: "" } }, [
				el("div.oauth-auto-head", null, [el("span.oauth-auto-icon", { text: "✓" }), el("div", null, [el("strong", { text: "Поля обнаруживаются автоматически" }), el("p.muted", { text: "ChatGPT получит endpoints через OAuth metadata, зарегистрируется и откроет окно пароля MCP Hub." })])]),
				el("div.oauth-endpoints", null, [
					el("div", null, [el("span", { text: "Auth" }), el("code", { text: oauthBase + "/oauth/authorize" })]),
					el("div", null, [el("span", { text: "Token" }), el("code", { text: oauthBase + "/oauth/token" })]),
					el("div", null, [el("span", { text: "Registration" }), el("code", { text: oauthBase + "/oauth/register" })]),
					el("div", null, [el("span", { text: "Resource" }), el("code", { text: oauthResource })]),
				]),
			])
			const introspectionOauth = el("div", { dataset: { oauthIntrospection: "" } }, [
				el("p.muted.oauth-advanced-note", { text: "Только если у вас уже есть Keycloak, Auth0, Authentik или другой RFC 7662 сервер." }),
				el("div.form-grid", null, [
					field("Introspection URL", input("oauthIntrospectionUrl", model.oauth?.introspectionUrl, { placeholder: "https://auth.example.com/oauth/introspect" })),
					field("Client ID", input("oauthClientId", model.oauth?.clientId)),
					field("Client Secret", input("oauthClientSecret", "", { type: "password", placeholder: model.oauth?.hasClientSecret ? "сохранён — оставьте пустым" : "если требует OAuth-сервер" })),
					field("Обязательные scopes", input("oauthRequiredScopes", model.oauth?.requiredScopes, { placeholder: "mcp:tools" })),
				]),
				el("label.check-row", null, [checkbox("oauthVerifyTls", model.oauth?.verifyTls !== false), "Проверять TLS introspection endpoint"]),
			])
			const routeOauth = el("fieldset.studio-fieldset", null, [
				el("legend", { text: "OAuth-защита маршрута" }),
				field("Режим OAuth", oauthRouteMode), builtinOauth, introspectionOauth,
		])
		const stdioFields = el("fieldset.studio-fieldset", null, [
			el("legend", { text: "Локальный stdio MCP" }),
			el("div.form-grid", null, [
				field("Локальный порт", input("port", model.port, { type: "number", min: 1, max: 65535, placeholder: "8010" }), "Проверяется на конфликты"),
				field("Upstream path", input("upstreamPath", model.upstreamPath || "/mcp")),
				el("label.field.wide", null, ["Команда запуска", el("textarea", { name: "command", text: model.command || "", placeholder: "npx -y supergateway --stdio \"...\" --port {port} ..." }), el("span.hint", { text: "Используйте {port} для выбранного локального порта" })]),
			]),
		])
		const remoteFields = el("fieldset.studio-fieldset", null, [
			el("legend", { text: "Удалённый / уже запущенный MCP" }),
			el("div.form-grid", null, [
				field("Upstream URL", input("upstream", model.kind === "remote" ? model.upstream : "", { placeholder: "http://127.0.0.1:3872/mcp" })),
				field("Авторизация апстрима", upstreamAuth),
			]),
			el("div", { dataset: { upstreamBearer: "" } }, [field("Bearer token", input("upstreamToken", "", { type: "password", placeholder: model.hasUpstreamToken ? "сохранён — оставьте пустым" : "token" }))]),
			el("div.studio-oauth-grid", { dataset: { upstreamOauth: "" } }, [
				field("Token URL", input("upstreamOAuthTokenUrl", model.upstreamOAuth?.tokenUrl, { placeholder: "https://auth.example.com/oauth/token" })),
				field("Client ID", input("upstreamOAuthClientId", model.upstreamOAuth?.clientId)),
				field("Client Secret", input("upstreamOAuthClientSecret", "", { type: "password", placeholder: model.upstreamOAuth?.hasClientSecret ? "сохранён — оставьте пустым" : "обязателен" })),
				field("Scope", input("upstreamOAuthScope", model.upstreamOAuth?.scope)),
				field("Audience", input("upstreamOAuthAudience", model.upstreamOAuth?.audience)),
				el("label.check-row", null, [checkbox("upstreamOAuthVerifyTls", model.upstreamOAuth?.verifyTls !== false), "Проверять TLS OAuth endpoint"]),
			]),
		])
		const save = el("button.btn.primary", { type: "submit", dataset: { saveService: "" } }, isNew ? "Создать MCP" : "Сохранить изменения")
		const actions = [save]
		if (service) {
			actions.push(el("button.btn.ghost", { type: "button", onclick: async (event) => {
				try { await withBusy(event.currentTarget, () => rpc("service.setEnabled", { id: service.id, enabled: !service.enabled })); toast(service.enabled ? "MCP выключен" : "MCP включён", "ok"); await refresh(true) }
				catch (error) { reportError(error, "Переключение MCP") }
			} }, service.enabled ? "Выключить" : "Включить"))
			if (!service.builtin) actions.push(el("button.btn.danger", { type: "button", onclick: async () => {
				if (!await confirmDialog("Удалить MCP «" + service.label + "» и его маршрут?", { okLabel: "Удалить" })) return
				try { await rpc("service.delete", { id: service.id }); memory.selectedId = null; toast("MCP удалён", "ok"); await refresh(true) }
				catch (error) { reportError(error, "Удаление MCP") }
			} }, "Удалить"))
		}
		form.append(
			el("div.studio-section-title", null, [el("div", null, [el("h2", { text: isNew ? "Новый MCP" : model.label }), el("p.muted", { text: isNew ? "Создайте локальный stdio или удалённый Streamable HTTP MCP." : model.url || "" })]), service ? badge(service.status?.state || "off", service.status?.state === "up" ? "ok" : service.status?.state === "off" ? "off" : "warn") : null]),
			el("fieldset.studio-fieldset", null, [
				el("legend", { text: "Основное" }),
				el("div.form-grid", null, [
					field("ID", input("id", model.id, { pattern: "[a-z][a-z0-9_\\-]{0,23}", disabled: !isNew }), "Латиница, используется в конфиге"),
					field("Название", input("label", model.label, { required: true })),
					field("Тип MCP", kind), field("Публичный путь", input("path", model.path, { required: true, pattern: "/.*" })),
					el("label.field.wide", null, ["Описание", el("textarea", { name: "note", text: model.note || "" })]),
				]),
			]),
			stdioFields, remoteFields,
			el("fieldset.studio-fieldset", null, [el("legend", { text: "Доступ к публичному маршруту" }), field("Режим", authMode, "Выбирается отдельно для каждого MCP")]),
			routeOauth,
			el("div.row-actions", null, actions),
		)
		function sync() {
			const mode = kind.value
			stdioFields.classList.toggle("is-hidden", mode !== "stdio")
			remoteFields.classList.toggle("is-hidden", mode !== "remote")
				routeOauth.classList.toggle("is-hidden", authMode.value !== "oauth")
				builtinOauth.classList.toggle("is-hidden", oauthRouteMode.value !== "builtin")
				introspectionOauth.classList.toggle("is-hidden", oauthRouteMode.value !== "introspection")
			remoteFields.querySelector("[data-upstream-bearer]")?.classList.toggle("is-hidden", upstreamAuth.value !== "bearer")
			remoteFields.querySelector("[data-upstream-oauth]")?.classList.toggle("is-hidden", upstreamAuth.value !== "oauth")
		}
			kind.onchange = sync; authMode.onchange = sync; oauthRouteMode.onchange = sync; upstreamAuth.onchange = sync; sync()
		form.onsubmit = (event) => { event.preventDefault(); saveService(form, service, rerender) }
		return form
	}

	function connectionParams(form, selected) {
		const data = new FormData(form)
		const mode = data.get("connectionAuth")
		const direct = !!form.elements.directUpstream?.checked
		const auth = mode === "auto" ? {} : mode === "hub_token" ? { mode: "hub_token" } : mode === "bearer" ? { mode: "bearer", token: data.get("connectionToken") } : mode === "oauth_client_credentials" ? {
			mode, tokenUrl: data.get("connectionTokenUrl"), clientId: data.get("connectionClientId"), clientSecret: data.get("connectionClientSecret"), scope: data.get("connectionScope"), audience: data.get("connectionAudience"),
		} : { mode: "none" }
		return {
			service: selected?.id, url: direct ? "" : data.get("connectionUrl"), directUpstream: direct,
			auth, verifyTls: form.elements.connectionVerifyTls.checked,
			timeout: Number(data.get("connectionTimeout") || 25),
		}
	}

	function renderExplorer(state, selected, rerender) {
		if (memory.connectionService !== (selected?.id || "__manual__") || !memory.connection) {
			memory.connectionService = selected?.id || "__manual__"
			memory.connection = {
				url: selected?.url || "https://mcp.riseshield.ru/roblox", authMode: selected ? "auto" : "none",
				directUpstream: false, token: "", tokenUrl: "", clientId: "", clientSecret: "",
				scope: "", audience: "", verifyTls: true, timeout: 25,
			}
		}
		const c = memory.connection
		const form = el("form.studio-connect")
		const authMode = select("connectionAuth", c.authMode, [["auto", "Автоматически из настроек MCP"], ["hub_token", "Токен MCP Hub"], ["none", "Без авторизации"], ["bearer", "Bearer access token"], ["oauth_client_credentials", "OAuth Client Credentials"]])
		if (!selected) authMode.querySelector('option[value="auto"]').disabled = true
		const bearer = el("div", { dataset: { connectionBearer: "" } }, [field("Access token", input("connectionToken", c.token, { type: "password" }))])
		const oauthFields = el("div.studio-oauth-grid", { dataset: { connectionOauth: "" } }, [
			field("Token URL", input("connectionTokenUrl", c.tokenUrl, { placeholder: "https://auth.example.com/oauth/token" })),
			field("Client ID", input("connectionClientId", c.clientId)),
			field("Client Secret", input("connectionClientSecret", c.clientSecret, { type: "password" })),
			field("Scope", input("connectionScope", c.scope)), field("Audience", input("connectionAudience", c.audience)),
		])
		const direct = checkbox("directUpstream", c.directUpstream)
		const verifyTls = checkbox("connectionVerifyTls", c.verifyTls)
		const listButton = el("button.btn.primary", { type: "submit" }, "Получить список tools")
		form.append(
			el("div.studio-section-title", null, [el("div", null, [el("h2", { text: "Подключение к MCP" }), el("p.muted", { text: "Tools загружаются только у выбранного адреса." })]), memory.meta ? badge(memory.meta.count + " tools", "ok") : null]),
			el("div.form-grid", null, [
				field("MCP endpoint", input("connectionUrl", c.url, { type: "url", required: !selected, placeholder: "https://mcp.riseshield.ru/roblox" }), "Можно вставить полный URL конкретного MCP"),
				field("Авторизация", authMode),
				field("Таймаут, секунд", input("connectionTimeout", c.timeout, { type: "number", min: 3, max: 120 })),
			]),
			selected ? el("label.check-row", null, [direct, "Подключиться напрямую к upstream, минуя публичный маршрут"]) : el("span.is-hidden"),
			bearer, oauthFields,
			el("div.connection-flags", null, [el("label.check-row", null, [verifyTls, "Проверять TLS-сертификат"])]),
			el("div.row-actions", null, [listButton, memory.meta ? el("span.muted", { text: (memory.meta.serverInfo?.name || "MCP") + " · " + (memory.meta.protocolVersion || "protocol ?") + " · " + memory.meta.elapsedMs + " мс" }) : null]),
		)
		function syncAuth() { bearer.classList.toggle("is-hidden", authMode.value !== "bearer"); oauthFields.classList.toggle("is-hidden", authMode.value !== "oauth_client_credentials") }
		authMode.onchange = syncAuth; syncAuth()
		form.onsubmit = async (event) => {
			event.preventDefault()
			const data = new FormData(form)
			memory.connection = {
				url: data.get("connectionUrl"), authMode: data.get("connectionAuth"), directUpstream: !!form.elements.directUpstream?.checked,
				token: data.get("connectionToken"), tokenUrl: data.get("connectionTokenUrl"), clientId: data.get("connectionClientId"), clientSecret: data.get("connectionClientSecret"),
				scope: data.get("connectionScope"), audience: data.get("connectionAudience"), verifyTls: form.elements.connectionVerifyTls.checked, timeout: Number(data.get("connectionTimeout") || 25),
			}
			try {
				const result = await withBusy(listButton, () => rpc("mcp.tools.list", connectionParams(form, selected), { timeout: 130000 }))
				memory.tools = result.tools || []; memory.meta = result; memory.selectedTool = memory.tools[0]?.name || null; memory.toolResult = null
				toast("Получено tools: " + memory.tools.length, "ok"); rerender()
			} catch (error) { reportError(error, "Tools Explorer") }
		}
		return el("div.studio-explorer", null, [form, renderTools(selected, rerender)])
	}

	function renderTools(selected, rerender) {
		if (!memory.tools.length) return empty("Подключитесь к выбранному MCP — здесь появится список только его tools.")
		const search = input("toolSearch", "", { type: "search", placeholder: "Фильтр tools…" })
		const list = el("div.tool-list")
		function paint(filter = "") {
			clear(list)
			const needle = filter.trim().toLowerCase()
			const found = memory.tools.filter(tool => !needle || String(tool.name).toLowerCase().includes(needle) || String(tool.description || "").toLowerCase().includes(needle))
			for (const tool of found) list.append(el("button.tool-item" + (memory.selectedTool === tool.name ? ".active" : ""), { type: "button", onclick: () => { memory.selectedTool = tool.name; memory.toolResult = null; rerender() } }, [el("strong", { text: tool.name }), el("span", { text: tool.description || "Без описания" })]))
			if (!found.length) list.append(empty("Ничего не найдено"))
		}
		search.oninput = () => paint(search.value); paint()
		const tool = memory.tools.find(item => item.name === memory.selectedTool) || memory.tools[0]
		if (!memory.toolArgs[tool.name]) memory.toolArgs[tool.name] = pretty(exampleFromSchema(tool.inputSchema || { type: "object" }))
		const args = el("textarea.tool-arguments", { text: memory.toolArgs[tool.name], spellcheck: "false" })
		args.oninput = () => { memory.toolArgs[tool.name] = args.value }
		const run = el("button.btn.primary", { type: "button" }, "Запустить tool")
		run.onclick = async () => {
			let parsed
			try { parsed = JSON.parse(args.value || "{}") } catch (error) { reportError(error, "JSON аргументов"); return }
			if (!parsed || Array.isArray(parsed) || typeof parsed !== "object") { toast("Аргументы должны быть JSON-объектом", "bad"); return }
			const connection = memory.connection
			const auth = connection.authMode === "auto" ? {} : connection.authMode === "hub_token" ? { mode: "hub_token" } : connection.authMode === "bearer" ? { mode: "bearer", token: connection.token } : connection.authMode === "oauth_client_credentials" ? { mode: "oauth_client_credentials", tokenUrl: connection.tokenUrl, clientId: connection.clientId, clientSecret: connection.clientSecret, scope: connection.scope, audience: connection.audience } : { mode: "none" }
			const payload = { service: selected?.id, url: connection.directUpstream ? "" : connection.url, directUpstream: connection.directUpstream, auth, verifyTls: connection.verifyTls, timeout: connection.timeout, name: tool.name, arguments: parsed }
			try { memory.toolResult = await withBusy(run, () => rpc("mcp.tool.call", payload, { timeout: 130000 })); toast(tool.name + " выполнен", "ok"); rerender() }
			catch (error) { reportError(error, "Tool " + tool.name) }
		}
		const result = memory.toolResult ? el("section.tool-result", null, [el("div.card-head", null, [el("div", null, [el("h3", { text: "Ответ" }), el("p.muted", { text: memory.toolResult.elapsedMs + " мс" })]), el("button.btn.ghost.small", { type: "button", onclick: () => copy(pretty(memory.toolResult.result)) }, "Копировать")]), el("pre.log", { text: pretty(memory.toolResult.result) })]) : null
		return el("div.tools-workspace", null, [
			el("aside.tools-sidebar", null, [search, list]),
			el("section.tool-detail", null, [
				el("div.studio-section-title", null, [el("div", null, [el("h2", { text: tool.name }), el("p.muted", { text: tool.description || "Без описания" })]), badge("tool", "info")]),
				el("div.tool-split", null, [el("div", null, [el("h3", { text: "Input schema" }), el("pre.log.tool-schema", { text: pretty(tool.inputSchema || {}) })]), el("div", null, [el("h3", { text: "Arguments JSON" }), args])]),
				el("div.row-actions", null, [run, el("button.btn.ghost", { type: "button", onclick: () => { memory.toolArgs[tool.name] = pretty(exampleFromSchema(tool.inputSchema || { type: "object" })); rerender() } }, "Сбросить пример")]),
				result,
			]),
		])
	}

	render()
	return { destroy() { currentHost = null } }
}
