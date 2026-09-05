// Поток событий (SSE) + планировщик перерисовки.
//
// Корень «дрожания» в старой панели: на КАЖДОЕ SSE-событие вызывался
// полный refresh() + render() через innerHTML. При активном MCP-трафике
// события идут десятками в секунду → страница перестраивалась непрерывно,
// терялся скролл и фокус.
//
// Здесь: события приходят адресно (подписка по типу), а тяжёлая перезагрузка
// состояния проходит через коалесцирующий планировщик с минимальным
// интервалом и паузой, пока вкладка скрыта или пользователь работает руками.

import { diag } from "./diag.js"
import { userIsBusy } from "./util.js"

const EVENT_KINDS = [
	"call.started", "call.finished", "state.dirty", "service.state", "service.toggled",
	"service.changed", "install.progress", "install.finished", "service.unreachable",
	"service.giveup", "calls.purged", "log.entry", "log.overflow", "log.cleared",
]

const BACKOFF_MIN = 1000
const BACKOFF_MAX = 15000

export class EventStream {
	constructor() {
		this.source = null
		this.lastSeq = 0
		this.status = "connecting"
		this.attempt = 0
		this.timer = 0
		this.handlers = new Map()
		this.statusHandlers = new Set()
		this.stopped = false

		document.addEventListener("visibilitychange", () => {
			if (!document.hidden && this.status !== "online") this.connect(true)
		})
		window.addEventListener("online", () => this.connect(true))
	}

	/** Подписка на тип события: on("call.finished", fn). */
	on(kind, fn) {
		if (!this.handlers.has(kind)) this.handlers.set(kind, new Set())
		this.handlers.get(kind).add(fn)
		return () => this.handlers.get(kind).delete(fn)
	}

	onStatus(fn) {
		this.statusHandlers.add(fn)
		fn(this.status)
		return () => this.statusHandlers.delete(fn)
	}

	setStatus(status, detail) {
		if (this.status === status) return
		this.status = status
		for (const fn of this.statusHandlers) {
			try { fn(status, detail) } catch { /* подписчик не должен ронять поток */ }
		}
	}

	emit(kind, payload) {
		const set = this.handlers.get(kind)
		if (!set) return
		for (const fn of set) {
			try {
				fn(payload)
			} catch (error) {
				diag.error("Обработчик события " + kind + " упал: " + error.message, {
					event: "sse.handlerFailed", kind,
				})
			}
		}
	}

	connect(immediate = false) {
		clearTimeout(this.timer)
		this.stopped = false
		if (this.source) { this.source.close(); this.source = null }
		if (!immediate && this.attempt > 0) {
			const base = Math.min(BACKOFF_MIN * 2 ** (this.attempt - 1), BACKOFF_MAX)
			const wait = Math.round(base * (0.7 + Math.random() * 0.6))   // jitter
			this.setStatus("reconnecting", wait)
			this.timer = setTimeout(() => this.open(), wait)
			return
		}
		this.open()
	}

	open() {
		if (this.stopped) return
		this.setStatus(this.attempt ? "reconnecting" : "connecting")
		let source
		try {
			source = new EventSource("api/events?lastSeq=" + this.lastSeq)
		} catch (error) {
			diag.error("Не удалось открыть поток событий: " + error.message, { event: "sse.openFailed" })
			this.attempt += 1
			this.connect()
			return
		}
		this.source = source

		source.onopen = () => {
			const wasDown = this.attempt > 0
			this.attempt = 0
			this.setStatus("online")
			if (wasDown) diag.note("Поток событий восстановлен", { event: "sse.reconnected", lastSeq: this.lastSeq })
			else diag.debug("Поток событий открыт", { event: "sse.open" })
		}

		source.onerror = () => {
			if (this.stopped) return
			source.close()
			if (this.source === source) this.source = null
			this.attempt += 1
			this.setStatus("offline")
			if (this.attempt === 1 || this.attempt % 5 === 0) {
				diag.warn("Поток событий оборван, попытка " + this.attempt, {
					event: "sse.error", attempt: this.attempt, lastSeq: this.lastSeq,
				})
			}
			this.connect()
		}

		const handle = (kind) => (message) => {
			const seq = Number(message.lastEventId || 0)
			if (seq) this.lastSeq = Math.max(this.lastSeq, seq)
			let envelope = {}
			try {
				envelope = message.data ? JSON.parse(message.data) : {}
			} catch {
				diag.warn("Неразборчивое событие " + kind, { event: "sse.badPayload", kind })
				return
			}
			// Сервер отправляет служебный конверт {seq, ts, kind, data}.
			// Подписчикам нужен именно payload из data, иначе status/message
			// установки остаются undefined и успешный результат выглядит ошибкой.
			const payload = envelope && typeof envelope === "object" && "data" in envelope
				? (envelope.data ?? {})
				: envelope
			this.emit(kind, payload)
		}

		for (const kind of EVENT_KINDS) source.addEventListener(kind, handle(kind))
		source.onmessage = handle("message")
	}

	stop() {
		this.stopped = true
		clearTimeout(this.timer)
		if (this.source) { this.source.close(); this.source = null }
		this.setStatus("offline")
	}
}

/**
 * Планировщик тяжёлых обновлений.
 *
 * Несколько событий склеиваются в один вызов; между вызовами гарантирован
 * интервал minInterval, так что шторм вызовов MCP не превращается в шторм рендеров.
 */
export function createScheduler(fn, { debounce = 250, minInterval = 1200, name = "refresh" } = {}) {
	let timer = 0
	let last = 0
	let queued = false
	let running = false
	let skipped = 0

	async function run() {
		timer = 0
		if (running) { queued = true; return }
		if (document.hidden) { queued = true; return }        // вкладка не видна — не тратить кадры
		if (userIsBusy()) { schedule(600); return }            // не вырывать ввод из-под рук
		const since = Date.now() - last
		if (since < minInterval) { schedule(minInterval - since); return }
		running = true
		last = Date.now()
		try {
			await fn()
		} catch (error) {
			diag.error("Обновление «" + name + "» не удалось: " + (error.message || error), {
				event: "ui.refreshFailed", name,
			})
		} finally {
			running = false
			if (queued) { queued = false; schedule(debounce) }
		}
	}

	function schedule(wait) {
		if (timer) { skipped += 1; return }
		timer = setTimeout(run, wait)
	}

	const api = () => schedule(debounce)
	api.now = () => { clearTimeout(timer); timer = 0; last = 0; return run() }
	api.stats = () => ({ skipped })

	document.addEventListener("visibilitychange", () => {
		if (!document.hidden && queued) { queued = false; schedule(100) }
	})

	return api
}
