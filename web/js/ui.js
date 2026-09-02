// Общие элементы интерфейса: тосты, баннеры, подтверждения, панель деталей.
// Всё без innerHTML и без window.confirm/alert — чтобы не блокировать поток событий.

import { $, el, clear, setText, setClass, append, fmtMs } from "./util.js"
import { diag } from "./diag.js"

// ------------------------------- тосты -------------------------------- //

const toastIndex = new Map()   // текст -> {node, count, timer}

export function toast(message, kind = "", options = {}) {
	const host = $("#toasts")
	if (!host) return
	const text = String(message ?? "")
	const key = kind + "|" + text
	const ttl = options.ttl ?? (kind === "bad" ? 9000 : 4500)

	// Повторы склеиваются в один тост со счётчиком: раньше при шторме ошибок
	// экран заваливало десятками одинаковых плашек.
	const existing = toastIndex.get(key)
	if (existing) {
		existing.count += 1
		setText(existing.counter, "×" + existing.count)
		setClass(existing.counter, "is-hidden", false)
		clearTimeout(existing.timer)
		existing.timer = setTimeout(() => close(key), ttl)
		return existing.node
	}

	const counter = el("span.n.is-hidden")
	const node = el("div.toast" + (kind ? "." + kind : ""), null, [
		el("span", { text }),
		counter,
		el("button.x", { type: "button", "aria-label": "Закрыть", onclick: () => close(key) }, "✕"),
	])
	if (options.action) {
		node.insertBefore(el("button.btn.ghost.small", {
			type: "button",
			onclick: () => { close(key); options.action.run() },
		}, options.action.label), node.lastChild)
	}
	host.append(node)
	toastIndex.set(key, { node, counter, count: 1, timer: setTimeout(() => close(key), ttl) })
	while (host.children.length > 5) host.firstChild.remove()
	return node
}

function close(key) {
	const item = toastIndex.get(key)
	if (!item) return
	clearTimeout(item.timer)
	item.node.remove()
	toastIndex.delete(key)
}

/** Ошибка всегда видна пользователю и всегда падает в журнал. */
export function reportError(error, context = "") {
	const message = error && error.human ? error.human : (error && error.message) || String(error)
	toast((context ? context + ": " : "") + message, "bad")
	diag.error((context ? context + ": " : "") + message, {
		event: "ui.error",
		kind: error && error.kind,
		status: error && error.status,
		errorId: error && error.errorId,
	})
}

// ------------------------------ баннеры ------------------------------- //

const banners = new Map()

/** Постоянное сообщение вверху (оффлайн, деградация, перезапуск). */
export function banner(id, { text, kind = "warn", action = null } = {}) {
	const host = $("#banners")
	if (!host) return
	if (!text) return dismissBanner(id)
	let item = banners.get(id)
	if (!item) {
		const label = el("span.text")
		const node = el("div.banner", { dataset: { banner: id } }, [label])
		host.append(node)
		item = { node, label, action: null }
		banners.set(id, item)
	}
	item.node.className = "banner " + kind
	setText(item.label, text)
	if (item.action) { item.action.remove(); item.action = null }
	if (action) {
		item.action = el("button.btn.ghost.small", { type: "button", onclick: action.run }, action.label)
		item.node.append(item.action)
	}
}

export function dismissBanner(id) {
	const item = banners.get(id)
	if (!item) return
	item.node.remove()
	banners.delete(id)
}

// ---------------------------- подтверждение ---------------------------- //

let confirmResolve = null

export function confirmDialog(text, { okLabel = "Продолжить", title = "Подтвердите действие", danger = true } = {}) {
	const modal = $("#confirm")
	if (!modal) return Promise.resolve(window.confirm(text))
	setText($("#confirm-title"), title)
	setText($("#confirm-text"), text)
	const ok = $("#confirm-ok")
	setText(ok, okLabel)
	ok.className = "btn " + (danger ? "danger" : "primary")
	modal.classList.remove("is-hidden")
	ok.focus()
	return new Promise((resolve) => { confirmResolve = resolve })
}

function settleConfirm(value) {
	const modal = $("#confirm")
	if (modal) modal.classList.add("is-hidden")
	if (confirmResolve) { confirmResolve(value); confirmResolve = null }
}

// ------------------------------- детали -------------------------------- //

let drawerReturnFocus = null
let drawerPayload = ""

export function openDrawer({ title, subtitle = "", body = null, json = "" }) {
	const drawer = $("#drawer")
	if (!drawer) return
	drawerReturnFocus = document.activeElement
	drawerPayload = json || ""
	setText($("#drawer-title"), title)
	setText($("#drawer-sub"), subtitle)
	const host = clear($("#drawer-body"))
	append(host, body)
	setClass($("#drawer-copy"), "is-hidden", !drawerPayload)
	drawer.classList.remove("is-hidden")
	const focusable = drawer.querySelector("button, [href], input, select, textarea")
	if (focusable) focusable.focus()
}

export function closeDrawer() {
	const drawer = $("#drawer")
	if (!drawer || drawer.classList.contains("is-hidden")) return
	drawer.classList.add("is-hidden")
	if (drawerReturnFocus && drawerReturnFocus.focus) drawerReturnFocus.focus()
	drawerReturnFocus = null
}

export function drawerJson() { return drawerPayload }

/** Вызывается один раз при старте. */
export function installOverlayHandlers() {
	document.addEventListener("click", (event) => {
		if (event.target.closest("[data-close-drawer]")) closeDrawer()
		if (event.target.closest("[data-confirm-cancel]")) settleConfirm(false)
		if (event.target.closest("#confirm-ok")) settleConfirm(true)
	})
	document.addEventListener("keydown", (event) => {
		if (event.key !== "Escape") return
		if (!$("#confirm").classList.contains("is-hidden")) { settleConfirm(false); return }
		closeDrawer()
	})
	// простая ловушка фокуса для боковой панели
	document.addEventListener("focusin", (event) => {
		const drawer = $("#drawer")
		if (!drawer || drawer.classList.contains("is-hidden")) return
		if (!drawer.contains(event.target)) {
			const first = drawer.querySelector("button, [href], input, select, textarea")
			if (first) first.focus()
		}
	})
}

// --------------------------- конструкторы блоков --------------------------- //

/** Метрика с фиксированной высотой: обновляется точечно через patch(). */
export function metric(label, value, sub = "", kind = "") {
	const v = el("div.v", { text: value ?? "—" })
	const s = el("div.s", { text: sub })
	const node = el("div.metric" + (kind ? "." + kind : ""), null, [
		el("div.k", { text: label }), v, s,
	])
	node.patch = (nextValue, nextSub, nextKind) => {
		setText(v, nextValue ?? "—")
		if (nextSub !== undefined) setText(s, nextSub)
		if (nextKind !== undefined) node.className = "metric" + (nextKind ? " " + nextKind : "")
	}
	return node
}

export function badge(text, kind = "") {
	return el("span.badge" + (kind ? "." + kind : ""), { text })
}

export function statusBadge(status) {
	const map = {
		ok: ["ok", "успешно"],
		error: ["bad", "ошибка"],
		timeout: ["bad", "таймаут"],
		cancelled: ["warn", "отменён"],
		running: ["warn", "идёт"],
	}
	const [kind, label] = map[status] || ["off", status || "—"]
	return badge(label, kind)
}

export function empty(text) {
	return el("div.empty", { text })
}

export function skeleton(rows = 4) {
	const box = el("div", { style: { display: "flex", flexDirection: "column", gap: "6px" } })
	for (let i = 0; i < rows; i += 1) box.append(el("div.skeleton.row"))
	return box
}

export function kv(pairs) {
	const list = el("dl.kv")
	for (const [key, value] of pairs) {
		if (value === null || value === undefined || value === "") continue
		list.append(el("dt", { text: key }))
		list.append(value instanceof Node ? el("dd", null, value) : el("dd", { text: String(value) }))
	}
	return list
}

export function card(titleText, subtitleText, actions = null, children = null) {
	const head = el("div.card-head", null, [
		el("div", null, [
			titleText ? el("h2", { text: titleText }) : null,
			subtitleText ? el("p.muted", { text: subtitleText }) : null,
		]),
		actions ? el("div.row-actions", null, actions) : null,
	])
	return el("div.card", null, [titleText || actions ? head : null, ...(Array.isArray(children) ? children : [children])])
}

/** Сегментный переключатель с счётчиками; счётчики патчатся без пересборки. */
export function segmented(items, active, onPick) {
	const node = el("div.seg", { role: "tablist" })
	const counters = new Map()
	for (const item of items) {
		const counter = el("span.c")
		const button = el("button", {
			type: "button",
			class: item.id === active ? "active" : "",
			"aria-selected": item.id === active ? "true" : "false",
			dataset: { seg: item.id },
			onclick: () => {
				for (const other of node.children) {
					setClass(other, "active", other.dataset.seg === item.id)
					other.setAttribute("aria-selected", other.dataset.seg === item.id ? "true" : "false")
				}
				onPick(item.id)
			},
		}, [item.label, counter])
		counters.set(item.id, counter)
		node.append(button)
	}
	node.setCount = (id, value) => setText(counters.get(id), value === null || value === undefined ? "" : String(value))
	return node
}

export function pill(label, on, onToggle) {
	const node = el("button.toggle-pill" + (on ? ".on" : ""), { type: "button" }, label)
	node.addEventListener("click", () => {
		const next = !node.classList.contains("on")
		setClass(node, "on", next)
		onToggle(next)
	})
	node.setOn = (value) => setClass(node, "on", value)
	return node
}

/** Полоски активности. Фиксированная высота → нет скачков при обновлении. */
export function sparkline(buckets) {
	const node = el("div.spark")
	node.patch = (list) => {
		const data = list || []
		const max = Math.max(1, ...data.map((b) => (b.ok || 0) + (b.failed || 0)))
		while (node.children.length > data.length) node.lastChild.remove()
		while (node.children.length < data.length) {
			node.append(el("div.bar", null, [el("span.failed"), el("span.ok")]))
		}
		data.forEach((bucket, index) => {
			const bar = node.children[index]
			const total = (bucket.ok || 0) + (bucket.failed || 0)
			const height = total ? Math.max(6, Math.round((total / max) * 68)) : 2
			bar.style.height = height + "px"
			setClass(bar, "empty", !total)
			const failedShare = total ? (bucket.failed || 0) / total : 0
			bar.children[0].style.height = Math.round(failedShare * height) + "px"
			bar.children[1].style.height = Math.round((1 - failedShare) * height) + "px"
			bar.title = new Date((bucket.t || 0) * 1000).toLocaleTimeString("ru-RU")
				+ " · успешно " + (bucket.ok || 0) + ", ошибок " + (bucket.failed || 0)
		})
	}
	node.patch(buckets)
	return node
}

export function durationCell(value) {
	return el("td.num", { text: fmtMs(value) })
}
