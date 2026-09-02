// Мелкие утилиты: DOM, форматирование, хранилище.
// Никаких innerHTML: всё строится через el(), чтобы можно было обновлять
// точечно и не терять фокус/скролл при живых обновлениях.

export const $ = (q, r = document) => r.querySelector(q)
export const $$ = (q, r = document) => Array.from(r.querySelectorAll(q))

/**
 * el("div.card", { title: "x" }, [child, "текст"])
 * Поддерживает tag.class1.class2, атрибуты, dataset, on* обработчики.
 */
export function el(spec, attrs = null, children = null) {
	const [tag, ...classes] = String(spec).split(".")
	const node = document.createElement(tag || "div")
	if (classes.length) node.className = classes.join(" ")
	if (attrs) {
		for (const [key, value] of Object.entries(attrs)) {
			if (value === null || value === undefined || value === false) continue
			if (key === "class") node.className = node.className ? node.className + " " + value : value
			else if (key === "text") node.textContent = String(value)
			else if (key === "html") node.innerHTML = String(value)
			else if (key === "dataset") Object.assign(node.dataset, value)
			else if (key === "style" && typeof value === "object") Object.assign(node.style, value)
			else if (key.startsWith("on") && typeof value === "function") node.addEventListener(key.slice(2), value)
			else if (value === true) node.setAttribute(key, "")
			else node.setAttribute(key, String(value))
		}
	}
	append(node, children)
	return node
}

export function append(node, children) {
	if (children === null || children === undefined) return node
	const list = Array.isArray(children) ? children : [children]
	for (const child of list) {
		if (child === null || child === undefined || child === false) continue
		node.append(child instanceof Node ? child : document.createTextNode(String(child)))
	}
	return node
}

export function clear(node) {
	if (node) while (node.firstChild) node.removeChild(node.firstChild)
	return node
}

/**
 * Запись текста только при реальном изменении — главный приём против дрожания:
 * лишняя запись в textContent вызывает reflow даже если значение то же самое.
 */
export function setText(node, value) {
	if (!node) return
	const next = value === null || value === undefined ? "" : String(value)
	if (node.textContent !== next) node.textContent = next
}

export function setClass(node, name, on) {
	if (!node) return
	if (node.classList.contains(name) !== !!on) node.classList.toggle(name, !!on)
}

export function setAttr(node, name, value) {
	if (!node) return
	if (value === null || value === undefined || value === false) {
		if (node.hasAttribute(name)) node.removeAttribute(name)
		return
	}
	const next = value === true ? "" : String(value)
	if (node.getAttribute(name) !== next) node.setAttribute(name, next)
}

// ------------------------------ форматы ------------------------------ //

export const DASH = "—"

export function fmtMs(value) {
	if (value === null || value === undefined || Number.isNaN(Number(value))) return DASH
	const v = Number(value)
	if (v < 1000) return Math.round(v) + " мс"
	return (v / 1000).toFixed(v < 10000 ? 2 : 1) + " с"
}

export function fmtNum(value) {
	if (value === null || value === undefined || Number.isNaN(Number(value))) return DASH
	return Number(value).toLocaleString("ru-RU")
}

export function fmtPct(value, digits = 1) {
	if (value === null || value === undefined) return DASH
	return (Number(value) * 100).toFixed(digits) + " %"
}

export function fmtBytes(value) {
	if (!value) return "0 Б"
	const units = ["Б", "КБ", "МБ", "ГБ"]
	let v = Number(value)
	let i = 0
	while (v >= 1024 && i < units.length - 1) { v /= 1024; i += 1 }
	return (i === 0 ? Math.round(v) : v.toFixed(1)) + " " + units[i]
}

export function fmtTime(seconds) {
	if (!seconds) return DASH
	return new Date(seconds * 1000).toLocaleTimeString("ru-RU", {
		hour: "2-digit", minute: "2-digit", second: "2-digit",
	})
}

export function fmtClock(seconds) {
	if (!seconds) return DASH
	const d = new Date(seconds * 1000)
	const pad = (n) => String(n).padStart(2, "0")
	return pad(d.getHours()) + ":" + pad(d.getMinutes()) + ":" + pad(d.getSeconds())
}

export function fmtDateTime(seconds) {
	if (!seconds) return DASH
	return new Date(seconds * 1000).toLocaleString("ru-RU")
}

export function fmtAgo(seconds) {
	if (!seconds) return "никогда"
	const s = Date.now() / 1000 - seconds
	if (s < 5) return "только что"
	if (s < 60) return Math.round(s) + " с назад"
	if (s < 3600) return Math.round(s / 60) + " мин назад"
	if (s < 86400) return Math.round(s / 3600) + " ч назад"
	return Math.round(s / 86400) + " дн назад"
}

export function pretty(value) {
	if (value === null || value === undefined) return ""
	if (typeof value === "string") {
		const text = value.trim()
		if (!text.startsWith("{") && !text.startsWith("[")) return value
		try { return JSON.stringify(JSON.parse(text), null, 2) } catch { return value }
	}
	try { return JSON.stringify(value, null, 2) } catch { return String(value) }
}

export function truncate(text, limit = 160) {
	const s = String(text ?? "")
	return s.length > limit ? s.slice(0, limit - 1) + "…" : s
}

// ------------------------------- тайминг ------------------------------ //

export function debounce(fn, wait = 250) {
	let timer = 0
	const wrapped = (...args) => {
		clearTimeout(timer)
		timer = setTimeout(() => fn(...args), wait)
	}
	wrapped.cancel = () => clearTimeout(timer)
	return wrapped
}

export function throttle(fn, interval = 500) {
	let last = 0
	let timer = 0
	return (...args) => {
		const now = Date.now()
		const wait = interval - (now - last)
		if (wait <= 0) { last = now; fn(...args); return }
		clearTimeout(timer)
		timer = setTimeout(() => { last = Date.now(); fn(...args) }, wait)
	}
}

export const nextFrame = (fn) => requestAnimationFrame(() => requestAnimationFrame(fn))

// ------------------------------ хранилище ----------------------------- //

const PREFIX = "mcphub."

export const store = {
	get(key, fallback = null) {
		try {
			const raw = localStorage.getItem(PREFIX + key)
			return raw === null ? fallback : JSON.parse(raw)
		} catch { return fallback }
	},
	set(key, value) {
		try { localStorage.setItem(PREFIX + key, JSON.stringify(value)) } catch { /* private mode */ }
	},
}

export async function copy(text) {
	const value = String(text ?? "")
	try {
		await navigator.clipboard.writeText(value)
		return true
	} catch {
		try {
			const area = el("textarea", { style: { position: "fixed", opacity: "0" } })
			area.value = value
			document.body.append(area)
			area.select()
			const ok = document.execCommand("copy")
			area.remove()
			return ok
		} catch { return false }
	}
}

export function download(filename, text, mime = "text/plain;charset=utf-8") {
	const blob = new Blob([text], { type: mime })
	const url = URL.createObjectURL(blob)
	const link = el("a", { href: url, download: filename })
	document.body.append(link)
	link.click()
	link.remove()
	setTimeout(() => URL.revokeObjectURL(url), 2000)
}

/** Не мешать пользователю: перерисовка откладывается, пока он печатает или выбирает текст. */
export function userIsBusy() {
	const active = document.activeElement
	if (active && /^(INPUT|TEXTAREA|SELECT)$/.test(active.tagName)) return true
	const selection = window.getSelection()
	return !!(selection && !selection.isCollapsed)
}
