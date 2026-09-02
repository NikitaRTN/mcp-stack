import { el, clear, setText } from "./util.js"
import { rpc, auth, withBusy } from "./api.js"
import { toast, reportError, confirmDialog, badge } from "./ui.js"

function option(value, label, selected = false) {
	return el("option", { value, selected }, label)
}

function field(label, control, hint = "") {
	return el("label.field", null, [label, control, hint ? el("span.hint", { text: hint }) : null])
}

function numberInput(value, name, min = 1, max = 65535) {
	return el("input", { name, type: "number", value, min, max, inputmode: "numeric" })
}

function switchField(label, name, checked, hint) {
	const input = el("input", { type: "checkbox", name })
	input.checked = !!checked
	return el("label.settings-switch", null, [
		el("span", null, [el("strong", { text: label }), el("small", { text: hint })]),
		el("span.switch", null, [input, el("span.track")]),
	])
}

export function mountSettings(host, { getState, refresh }) {
	const state = getState()
	const form = el("form.settings-form")
	const domain = el("input", { name: "domain", value: state.domain || "localhost", autocomplete: "off" })
	const email = el("input", { name: "email", type: "email", value: state.email || "", autocomplete: "email" })
	const bind = el("input", { name: "bind", value: state.bind || "", placeholder: "пусто = все интерфейсы" })
	const httpsPort = numberInput(state.httpsPort || 8443, "httpsPort")
	const adminPort = numberInput(state.adminPort || 8765, "adminPort")
	const inspectorPort = numberInput(state.inspectorPort || 8770, "inspectorPort")
	const telemetryDays = numberInput(state.telemetryDays || 14, "telemetryDays", 1, 3650)
	const save = el("button.btn.primary", { type: "submit" }, "Сохранить порты и сеть")
	form.append(
		el("div.form-grid", null, [
			field("Домен", domain, "Например mcp.riseshield.ru или localhost"),
			field("Email для сертификата", email, "Необязательно, используется Caddy / ACME"),
			field("Публичный HTTPS-порт", httpsPort, "Применяется сразу после проверки Caddyfile"),
			field("Порт панели", adminPort, "После изменения потребуется перезапуск MCP Hub"),
			field("Порт инспектора", inspectorPort, "После изменения потребуется перезапуск MCP Hub"),
			field("Bind IP", bind, "Оставьте пустым или укажите локальный IP"),
			field("Хранение телеметрии, дней", telemetryDays),
		]),
		el("div.settings-switches", null, [
			switchField("Автоперезапуск MCP", "autoRestart", state.autoRestart, "Следить только за включёнными MCP"),
			switchField("Открывать браузер", "openBrowser", state.openBrowser, "Открывать панель после запуска"),
		]),
		el("div.row-actions", null, [save, el("span.muted", { text: "Порты MCP меняются отдельно в MCP Studio." })]),
	)
	form.onsubmit = async (event) => {
		event.preventDefault()
		const data = new FormData(form)
		const payload = {
			domain: data.get("domain"), email: data.get("email"), bind: data.get("bind"),
			httpsPort: data.get("httpsPort"), adminPort: data.get("adminPort"),
			inspectorPort: data.get("inspectorPort"), telemetryDays: data.get("telemetryDays"),
			autoRestart: form.elements.autoRestart.checked,
			openBrowser: form.elements.openBrowser.checked,
		}
		try {
			const result = await withBusy(save, () => rpc("settings.update", payload, { timeout: 30000 }))
			if (result.restartRequired?.length) {
				toast("Сохранено. Перезапустите MCP Hub, чтобы применить порт панели и инспектора.", "warn", { ttl: 12000 })
			} else toast("Сетевые настройки применены", "ok")
			await refresh(true)
		} catch (error) { reportError(error, "Сохранение настроек") }
	}

	const firewallState = el("div.firewall-state", null, [badge("Проверяю…", "off")])
	const profile = el("select", { name: "profile" }, [
		option("private,domain", "Частная + доменная сеть", true),
		option("private", "Только частная сеть"),
		option("domain", "Только доменная сеть"),
		option("any", "Все профили, включая публичные"),
	])
	const allow = el("button.btn.primary", { type: "button" }, "Разрешить HTTPS-порт")
	const remove = el("button.btn.ghost", { type: "button" }, "Удалить правило")
	let firewall = null
	function paintFirewall(next) {
		firewall = next
		clear(firewallState).append(
			badge(next.supported ? (next.configured ? "Разрешён" : "Не добавлен") : "Только Windows",
				next.configured ? "ok" : next.supported ? "warn" : "off"),
			el("span", { text: next.detail || "" }),
		)
		allow.disabled = !next.supported
		remove.disabled = !next.supported || !next.configured
	}
	async function loadFirewall() {
		try { paintFirewall(await rpc("firewall.status", { port: Number(httpsPort.value) }, { quiet: true })) }
		catch (error) { reportError(error, "Windows Firewall") }
	}
	allow.onclick = async () => {
		const port = Number(httpsPort.value)
		const allProfiles = profile.value === "any"
		const confirmed = await confirmDialog(
			"Windows покажет системный запрос UAC. Разрешить входящие TCP-подключения к порту " + port +
			(allProfiles ? " во всех сетях, включая публичные?" : " в выбранных доверенных сетях?"),
			{ title: "Разрешить порт в Windows Firewall", okLabel: "Открыть UAC", danger: allProfiles })
		if (!confirmed) return
		try {
			const result = await withBusy(allow, () => rpc("firewall.authorize", { port, profile: profile.value }, { timeout: 190000 }))
			paintFirewall(result); toast(result.detail, "ok")
		} catch (error) { reportError(error, "Брандмауэр") }
	}
	remove.onclick = async () => {
		const port = Number(httpsPort.value)
		if (!await confirmDialog("Удалить правило MCP Hub для TCP " + port + "?", { okLabel: "Удалить" })) return
		try {
			const result = await withBusy(remove, () => rpc("firewall.remove", { port }, { timeout: 190000 }))
			paintFirewall(result); toast(result.detail, "ok")
		} catch (error) { reportError(error, "Брандмауэр") }
	}
	httpsPort.addEventListener("change", loadFirewall)

	const sslState = el("div.ssl-state", null, [badge("Проверяю SSL…", "off")])
	const sslFacts = el("div.ssl-facts")
	const sslNotes = el("div.ssl-notes")
	const issueSsl = el("button.btn.primary", { type: "button", disabled: true }, "Выпустить / проверить SSL")
	const checkSsl = el("button.btn.ghost", { type: "button" }, "Обновить статус")
	let sslStatus = null
	function sslFact(label, value, tone = "") {
		return el("div.ssl-fact" + (tone ? "." + tone : ""), null, [el("span", { text: label }), el("strong", { text: value || "—" })])
	}
	function paintSsl(next) {
		sslStatus = next || {}
		const domainInfo = sslStatus.domain || {}, cert = sslStatus.cert || {}, caddy = sslStatus.caddy || {}
		const local = domainInfo.mode === "local" || cert.applicable === false
		const label = local ? "Локальный режим" : cert.ok ? "SSL активен" : cert.subject ? "Сертификат не доверен" : "SSL не выпущен"
		clear(sslState).append(badge(label, cert.ok ? "ok" : local ? "off" : "warn"), el("span", { text: cert.detail || "Ожидается проверка сертификата" }))
		const expiry = cert.expiresAt ? new Date(cert.expiresAt * 1000).toLocaleDateString("ru-RU") + (cert.daysRemaining != null ? " · " + cert.daysRemaining + " дн." : "") : "—"
		const port = Number(domainInfo.httpsPort || state.httpsPort || 8443)
		clear(sslFacts).append(
			sslFact("Домен", domainInfo.domain || state.domain || "localhost"),
			sslFact("DNS", domainInfo.dns || domainInfo.dnsError || "—", domainInfo.dnsError ? "bad" : ""),
			sslFact("Внешний HTTPS", local ? "не используется" : port === 443 ? "443 напрямую" : "WAN 443 → LAN " + port),
			sslFact("Caddy", caddy.running && caddy.listening ? "запущен и слушает" : caddy.running ? "запускается" : "не запущен", caddy.running ? "ok" : ""),
			sslFact("Издатель", cert.issuer || "—", cert.ok ? "ok" : ""),
			sslFact("Действителен до", expiry, cert.expired ? "bad" : ""),
		)
		clear(sslNotes)
		for (const note of domainInfo.notes || []) {
			const informational = /WAN 443|Caddy слушает локальный порт|внешний адрес остаётся/i.test(note)
			sslNotes.append(el("div.ssl-note." + (informational ? "info" : "warn"), { text: note }))
		}
		if (!local && sslStatus.caddyfile && !sslStatus.caddyfile.ok) sslNotes.append(el("div.ssl-note.bad", { text: sslStatus.caddyfile.detail || "Caddyfile не прошёл проверку" }))
		issueSsl.disabled = local
	}
	async function loadSsl(button = null) {
		try { paintSsl(button ? await withBusy(button, () => rpc("domain.status", {}, { quiet: true })) : await rpc("domain.status", {}, { quiet: true })) }
		catch (error) { reportError(error, "Проверка SSL") }
	}
	checkSsl.onclick = () => loadSsl(checkSsl)
	issueSsl.onclick = async () => {
		const info = sslStatus?.domain || {}
		if (info.mode !== "public") { toast("Сначала сохраните публичный домен", "warn"); return }
		const port = Number(info.httpsPort || state.httpsPort || 8443)
		const hint = port === 443 ? "Внешний TCP 443 должен вести на этот компьютер." : "Нужен проброс WAN 443 → этот компьютер:" + port + "."
		if (!await confirmDialog("Caddy обратится к Let's Encrypt для домена " + info.domain + ". " + hint, { title: "Выпустить SSL-сертификат", okLabel: "Запустить проверку" })) return
		try {
			const result = await withBusy(issueSsl, () => rpc("certificate.issue", { wait: 60 }, { timeout: 105000 }))
			paintSsl(result)
			toast(result.ok ? "SSL-сертификат выпущен и проверен" : "Сертификат пока не подтверждён. Проверьте DNS и проброс 443.", result.ok ? "ok" : "warn", { ttl: 12000 })
		} catch (error) { reportError(error, "Выпуск SSL") }
	}

	const current = el("input", { type: "password", autocomplete: "current-password" })
	const next = el("input", { type: "password", autocomplete: "new-password", minlength: 8 })
	const passwordButton = el("button.btn.primary", { type: "button" }, "Изменить пароль")
	passwordButton.onclick = async () => {
		try {
			await withBusy(passwordButton, () => auth("password", { current: current.value, password: next.value }))
			current.value = ""; next.value = ""; toast("Пароль изменён", "ok")
		} catch (error) { reportError(error, "Пароль") }
	}

	clear(host).append(
		el("section.card", null, [
			el("div.card-head", null, [el("div", null, [el("h2", { text: "Порты и сеть" }), el("p.muted", { text: "Все сетевые параметры редактируются без правки hub.json." })])]),
			form,
		]),
		el("section.card", null, [
			el("div.card-head", null, [el("div", null, [el("h2", { text: "SSL-сертификат домена" }), el("p.muted", { text: "Caddy автоматически выпускает и продлевает сертификат Let's Encrypt." })])]),
			sslState, sslFacts, sslNotes,
			el("div.row-actions", null, [issueSsl, checkSsl]),
		]),
		el("section.card", null, [
			el("div.card-head", null, [el("div", null, [el("h2", { text: "Windows Firewall" }), el("p.muted", { text: "Открывается только публичный HTTPS-порт. Внутренние MCP-порты остаются локальными." })])]),
			firewallState,
			el("div.form-grid", null, [field("Профили сети", profile, "Рекомендуется частная + доменная сеть")]),
			el("div.row-actions", null, [allow, remove]),
		]),
		el("section.card", null, [
			el("div.card-head", null, [el("div", null, [el("h2", { text: "Пароль администратора" }), el("p.muted", { text: "Минимум 8 символов." })])]),
			el("div.form-grid", null, [field("Текущий пароль", current), field("Новый пароль", next)]),
			el("div.row-actions", null, [passwordButton]),
		]),
	)
	loadFirewall()
	loadSsl()
	return { destroy() {} }
}
