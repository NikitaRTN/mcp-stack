// Клиентская диагностика.
//
// Раньше ошибки в браузере глотались (try/catch без тела) и нигде не видны.
// Теперь любое исключение, отказ промиса и ключевые шаги UI попадают:
//   1) в локальное кольцо (видно во вкладке «Логи → браузер» и в выгрузке),
//   2) на сервер пачками через client.log — там они ложатся в logs/hub.jsonl.

const RING = 500
const BATCH_MAX = 20
const FLUSH_MS = 3000
const DEDUPE_MS = 4000

const entries = []
const pending = []
const lastSeen = new Map()
let seq = 0
let sender = null
let flushTimer = 0
let flushing = false
const listeners = new Set()

function signature(level, message, fields) {
	return level + "|" + message + "|" + (fields && fields.event ? fields.event : "")
}

function push(level, message, fields) {
	seq += 1
	const entry = {
		seq,
		ts: Date.now() / 1000,
		level,
		source: "browser",
		message: String(message ?? ""),
		event: (fields && fields.event) || "",
		fields: fields || {},
	}
	entries.push(entry)
	if (entries.length > RING) entries.splice(0, entries.length - RING)
	for (const fn of listeners) {
		try { fn(entry) } catch { /* подписчик не должен ронять логгер */ }
	}
	return entry
}

function queueForServer(entry) {
	const sig = signature(entry.level, entry.message, entry.fields)
	const now = Date.now()
	const prev = lastSeen.get(sig)
	if (prev && now - prev < DEDUPE_MS) return          // не засорять журнал повторами
	lastSeen.set(sig, now)
	if (lastSeen.size > 200) lastSeen.clear()
	pending.push({
		level: entry.level,
		message: entry.message,
		event: entry.event,
		fields: entry.fields,
	})
	if (pending.length >= BATCH_MAX) flush()
	else if (!flushTimer) flushTimer = setTimeout(flush, FLUSH_MS)
}

export async function flush() {
	clearTimeout(flushTimer)
	flushTimer = 0
	if (flushing || !sender || !pending.length) return
	const batch = pending.splice(0, BATCH_MAX)
	flushing = true
	try {
		await sender({ entries: batch })
	} catch {
		// Сервер недоступен — записи остаются в локальном кольце и в выгрузке.
	} finally {
		flushing = false
		if (pending.length && !flushTimer) flushTimer = setTimeout(flush, FLUSH_MS)
	}
}

function record(level, message, fields, toServer) {
	const entry = push(level, message, fields)
	if (toServer) queueForServer(entry)
	return entry
}

export const diag = {
	debug: (message, fields) => record("debug", message, fields, false),
	info: (message, fields) => record("info", message, fields, false),
	/** Событие, о котором стоит знать и на сервере. */
	note: (message, fields) => record("info", message, fields, true),
	warn: (message, fields) => record("warn", message, fields, true),
	error: (message, fields) => record("error", message, fields, true),

	list(filter = {}) {
		const { level, search } = filter
		const order = { debug: 0, info: 1, warn: 2, error: 3 }
		const min = level ? order[level] ?? 0 : 0
		const needle = (search || "").toLowerCase()
		return entries.filter((entry) => {
			if ((order[entry.level] ?? 1) < min) return false
			if (!needle) return true
			return (entry.message + " " + entry.event + " " + JSON.stringify(entry.fields)).toLowerCase().includes(needle)
		})
	},

	subscribe(fn) {
		listeners.add(fn)
		return () => listeners.delete(fn)
	},

	setSender(fn) {
		sender = fn
		if (pending.length) flush()
	},

	dump() {
		return JSON.stringify({
			generatedAt: new Date().toISOString(),
			userAgent: navigator.userAgent,
			language: navigator.language,
			viewport: { w: window.innerWidth, h: window.innerHeight, dpr: window.devicePixelRatio },
			entries,
		}, null, 2)
	},
}

/** Один раз на старте: ловим всё, что падает мимо наших try/catch. */
export function installGlobalHandlers() {
	window.addEventListener("error", (event) => {
		const error = event.error
		diag.error(event.message || "Ошибка скрипта", {
			event: "js.error",
			source: (event.filename || "").split("/").pop() + ":" + (event.lineno || 0),
			stack: error && error.stack ? String(error.stack).split("\n").slice(0, 6).join(" | ") : undefined,
		})
	})

	window.addEventListener("unhandledrejection", (event) => {
		const reason = event.reason
		diag.error("Необработанный отказ промиса: " + (reason && reason.message ? reason.message : String(reason)), {
			event: "js.unhandledRejection",
			kind: reason && reason.kind ? reason.kind : undefined,
			stack: reason && reason.stack ? String(reason.stack).split("\n").slice(0, 6).join(" | ") : undefined,
		})
	})

	window.addEventListener("beforeunload", () => { flush() })
	document.addEventListener("visibilitychange", () => { if (document.hidden) flush() })
}
