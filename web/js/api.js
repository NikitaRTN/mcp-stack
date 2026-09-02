// Транспорт к панели: таймауты, повторы, единый тип ошибки.
//
// Старый клиент делал fetch без таймаута и глотал ошибки: если сервер вис
// или отвечал 500, панель молча оставалась с устаревшими данными.
// Здесь каждый вызов либо успевает, либо бросает HubError с понятной причиной.

import { diag } from "./diag.js"

const DEFAULT_TIMEOUT = 15000
const READ_ONLY = /^(state\.get|calls\.|logs\.|install\.(job|detect)|caddy\.config|domain\.status|service\.logs)/

export class HubError extends Error {
	constructor(message, options = {}) {
		super(message)
		this.name = "HubError"
		this.kind = options.kind || "rpc"          // network | timeout | http | rpc | auth
		this.status = options.status || 0
		this.method = options.method || ""
		this.errorId = options.errorId || null
	}

	/** Текст для тоста/баннера. */
	get human() {
		if (this.kind === "network") return "Нет связи с панелью. Проверьте, что процесс MCP Hub запущен."
		if (this.kind === "timeout") return "Панель не ответила вовремя (больше 15 с)."
		return this.message + (this.errorId ? " · код " + this.errorId : "")
	}
}

let authRequired = false

function emit(name, detail) {
	window.dispatchEvent(new CustomEvent(name, { detail }))
}

async function request(path, init = {}, timeout = DEFAULT_TIMEOUT) {
	const controller = new AbortController()
	const timer = setTimeout(() => controller.abort(new DOMException("timeout", "TimeoutError")), timeout)
	try {
		return await fetch(path, { ...init, signal: init.signal || controller.signal })
	} catch (error) {
		if (error && (error.name === "AbortError" || error.name === "TimeoutError")) {
			throw new HubError("Запрос прерван по таймауту", { kind: "timeout" })
		}
		throw new HubError("Сетевая ошибка: " + (error && error.message ? error.message : error), { kind: "network" })
	} finally {
		clearTimeout(timer)
	}
}

async function readJson(response) {
	const text = await response.text()
	if (!text) return {}
	try {
		return JSON.parse(text)
	} catch {
		throw new HubError("Панель вернула не JSON (код " + response.status + ")", {
			kind: "http", status: response.status,
		})
	}
}

/**
 * Вызов RPC.
 * @param {string} method
 * @param {object} params
 * @param {{timeout?:number, retries?:number, signal?:AbortSignal, quiet?:boolean}} options
 */
export async function rpc(method, params = {}, options = {}) {
	const timeout = options.timeout ?? DEFAULT_TIMEOUT
	const retries = options.retries ?? (READ_ONLY.test(method) ? 2 : 0)
	const started = performance.now()
	let attempt = 0

	for (;;) {
		try {
			const response = await request("api/rpc", {
				method: "POST",
				headers: { "Content-Type": "application/json" },
				body: JSON.stringify({ method, params }),
				signal: options.signal,
			}, timeout)

			if (response.status === 401) {
				if (!authRequired) {
					authRequired = true
					emit("hub:auth-required", { method })
				}
				throw new HubError("Нужен вход в панель", { kind: "auth", status: 401, method })
			}
			authRequired = false

			const body = await readJson(response)
			if (!response.ok || body.ok === false) {
				throw new HubError(body.error || "Ошибка " + response.status, {
					kind: response.status >= 500 ? "http" : "rpc",
					status: body.status || response.status,
					method,
					errorId: body.errorId || null,
				})
			}

			const ms = Math.round(performance.now() - started)
			if (!options.quiet) {
				if (ms > 2000) diag.warn("Медленный вызов " + method, { event: "rpc.slow", method, ms })
				else diag.debug("Вызов " + method, { event: "rpc.ok", method, ms })
			}
			emit("hub:online", { method })
			return body.result === undefined ? body : body.result
		} catch (error) {
			const hub = error instanceof HubError ? error : new HubError(String(error && error.message || error), { method })
			const retriable = (hub.kind === "network" || hub.kind === "timeout" || hub.status >= 500) && attempt < retries
			if (retriable) {
				attempt += 1
				const wait = 400 * attempt * attempt
				diag.warn("Повтор " + method + " (попытка " + attempt + ")", {
					event: "rpc.retry", method, kind: hub.kind, status: hub.status,
				})
				await new Promise((resolve) => setTimeout(resolve, wait))
				continue
			}
			if (hub.kind === "network" || hub.kind === "timeout") emit("hub:offline", { method, kind: hub.kind })
			if (hub.kind !== "auth" && !options.quiet) {
				diag.error("Ошибка " + method + ": " + hub.message, {
					event: "rpc.failed", method, kind: hub.kind, status: hub.status, errorId: hub.errorId,
				})
			}
			throw hub
		}
	}
}

/** Несколько вызовов одним запросом: меньше круглых задержек и меньше разнобоя в UI. */
export async function batch(calls, options = {}) {
	if (!calls.length) return []
	const response = await request("api/rpc", {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(calls.map(([method, params]) => ({ method, params: params || {} }))),
	}, options.timeout ?? DEFAULT_TIMEOUT)

	if (response.status === 401) {
		if (!authRequired) { authRequired = true; emit("hub:auth-required", {}) }
		throw new HubError("Нужен вход в панель", { kind: "auth", status: 401 })
	}
	authRequired = false
	const body = await readJson(response)
	if (!response.ok) {
		throw new HubError(body.error || "Ошибка " + response.status, { kind: "http", status: response.status })
	}
	emit("hub:online", {})
	return (body.results || []).map((item, index) => {
		if (item.ok) return { ok: true, result: item.result, method: item.method }
		const hub = new HubError(item.error || "Ошибка", {
			kind: (item.status || 0) >= 500 ? "http" : "rpc",
			status: item.status || 400,
			method: item.method || calls[index][0],
			errorId: item.errorId || null,
		})
		diag.error("Ошибка " + hub.method + ": " + hub.message, {
			event: "rpc.failed", method: hub.method, status: hub.status, errorId: hub.errorId,
		})
		return { ok: false, error: hub, method: hub.method }
	})
}

/** Аутентификационные эндпоинты живут вне RPC. */
export async function auth(path, payload) {
	const response = await request("api/auth/" + path, {
		method: "POST",
		headers: { "Content-Type": "application/json" },
		body: JSON.stringify(payload || {}),
	}, 12000)
	const body = await readJson(response)
	if (!response.ok) {
		throw new HubError(body.error || "Ошибка " + response.status, {
			kind: "rpc", status: response.status, method: "auth/" + path,
		})
	}
	if (path === "login" || path === "setup") authRequired = false
	return body
}

export async function session() {
	const response = await request("api/session", { headers: { Accept: "application/json" } }, 10000)
	if (!response.ok) {
		throw new HubError("Не удалось проверить сессию (код " + response.status + ")", {
			kind: "http", status: response.status,
		})
	}
	return readJson(response)
}

/** Обёртка для кнопок: блокирует кнопку, показывает спиннер, всегда снимает состояние. */
export async function withBusy(button, fn) {
	if (button) {
		button.classList.add("busy")
		button.disabled = true
	}
	try {
		return await fn()
	} finally {
		if (button) {
			button.classList.remove("busy")
			button.disabled = false
		}
	}
}
