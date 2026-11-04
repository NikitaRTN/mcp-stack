import express from 'express'
import cors, { type CorsOptions } from 'cors'
import { spawn } from 'child_process'
import { Server } from '@modelcontextprotocol/sdk/server/index.js'
import { StreamableHTTPServerTransport } from '@modelcontextprotocol/sdk/server/streamableHttp.js'
import { JSONRPCMessage } from '@modelcontextprotocol/sdk/types.js'
import { Logger } from '../types.js'
import { getVersion } from '../lib/getVersion.js'
import { onSignals } from '../lib/onSignals.js'
import { serializeCorsOrigin } from '../lib/serializeCorsOrigin.js'
import { randomUUID } from 'node:crypto'
import { isInitializeRequest } from '@modelcontextprotocol/sdk/types.js'
import { SessionAccessCounter } from '../lib/sessionAccessCounter.js'

export interface StdioToStreamableHttpArgs {
  stdioCmd: string
  port: number
  streamableHttpPath: string
  logger: Logger
  corsOrigin: CorsOptions['origin']
  healthEndpoints: string[]
  headers: Record<string, string>
  sessionTimeout: number | null
}

const setResponseHeaders = ({
  res,
  headers,
}: {
  res: express.Response
  headers: Record<string, string>
}) =>
  Object.entries(headers).forEach(([key, value]) => {
    res.setHeader(key, value)
  })

export async function stdioToStatefulStreamableHttp(
  args: StdioToStreamableHttpArgs,
) {
  const {
    stdioCmd,
    port,
    streamableHttpPath,
    logger,
    corsOrigin,
    healthEndpoints,
    headers,
    sessionTimeout,
  } = args

  logger.info(
    `  - Headers: ${Object(headers).length ? JSON.stringify(headers) : '(none)'}`,
  )
  logger.info(`  - port: ${port}`)
  logger.info(`  - stdio: ${stdioCmd}`)
  logger.info(`  - streamableHttpPath: ${streamableHttpPath}`)

  logger.info(
    `  - CORS: ${corsOrigin ? `enabled (${serializeCorsOrigin({ corsOrigin })})` : 'disabled'}`,
  )
  logger.info(
    `  - Health endpoints: ${healthEndpoints.length ? healthEndpoints.join(', ') : '(none)'}`,
  )
  logger.info(
    `  - Session timeout: ${sessionTimeout ? `${sessionTimeout}ms` : 'disabled'}`,
  )

  onSignals({ logger })

  const app = express()
  app.use(express.json())

  if (corsOrigin) {
    app.use(
      cors({
        origin: corsOrigin,
        exposedHeaders: ['Mcp-Session-Id'],
      }),
    )
  }

  for (const ep of healthEndpoints) {
    app.get(ep, (_req, res) => {
      setResponseHeaders({
        res,
        headers,
      })
      res.send('ok')
    })
  }

  // Map to store transports by session ID
  const transports: { [sessionId: string]: StreamableHTTPServerTransport } = {}

  // Session access counter for timeout management
  const sessionCounter = sessionTimeout
    ? new SessionAccessCounter(
        sessionTimeout,
        (sessionId: string) => {
          logger.info(`Session ${sessionId} timed out, cleaning up`)
          const transport = transports[sessionId]
          if (transport) {
            transport.close()
          }
          delete transports[sessionId]
        },
        logger,
      )
    : null

  // Handle POST requests for client-to-server communication
  app.post(streamableHttpPath, async (req, res) => {
    // Check for existing session ID
    const sessionId = req.headers['mcp-session-id'] as string | undefined
    let activeSessionId = sessionId
    let transport: StreamableHTTPServerTransport

    if (sessionId && transports[sessionId]) {
      // Reuse existing transport
      transport = transports[sessionId]
      // Increment session access count
      sessionCounter?.inc(sessionId, 'POST request for existing session')
    } else if (!sessionId && isInitializeRequest(req.body)) {
      // New initialization request

      const server = new Server(
        { name: 'supergateway', version: getVersion() },
        { capabilities: {} },
      )

      transport = new StreamableHTTPServerTransport({
        sessionIdGenerator: () => randomUUID(),
        onsessioninitialized: (newSessionId) => {
          activeSessionId = newSessionId
          // Store the transport by session ID
          transports[newSessionId] = transport
          // Initialize session access count
          sessionCounter?.inc(newSessionId, 'session initialization')
        },
      })
      await server.connect(transport)
      const child = spawn(stdioCmd, { shell: true })
      let childStopped = false
      const stopChild = () => {
        if (childStopped) return
        childStopped = true
        if (process.platform === 'win32' && child.pid) {
          const killer = spawn(
            'taskkill',
            ['/PID', String(child.pid), '/T', '/F'],
            { windowsHide: true },
          )
          killer.on('error', () => child.kill())
        } else {
          child.kill()
        }
      }
      const initializeRequestId = (req.body as { id?: string | number | null })?.id
      const initializeTimer = setTimeout(() => {
        logger.error('Initialize timed out after 20000ms')
        if (!res.headersSent) {
          res.status(504).json({
            jsonrpc: '2.0',
            error: { code: -32000, message: 'MCP initialize timed out' },
            id: initializeRequestId ?? null,
          })
        }
        Promise.resolve(transport.close()).catch(() => undefined)
        stopChild()
      }, 20000)
      child.on('exit', (code, signal) => {
        childStopped = true
        clearTimeout(initializeTimer)
        logger.error(`Child exited: code=${code}, signal=${signal}`)
        Promise.resolve(transport.close()).catch(() => undefined)
      })

      let buffer = ''
      child.stdout.on('data', (chunk: Buffer) => {
        buffer += chunk.toString('utf8')
        const lines = buffer.split(/\r?\n/)
        buffer = lines.pop() ?? ''
        lines.forEach((line) => {
          if (!line.trim()) return
          try {
            const jsonMsg = JSON.parse(line)
            logger.info('Child → StreamableHttp:', line)
            if (jsonMsg?.id === initializeRequestId) {
              clearTimeout(initializeTimer)
            }
            try {
              const pendingSend = transport.send(jsonMsg)
              Promise.resolve(pendingSend).catch((e) => {
                logger.error(`Failed to send to StreamableHttp`, e)
                Promise.resolve(transport.close()).catch(() => undefined)
                stopChild()
              })
            } catch (e) {
              logger.error(`Failed to send to StreamableHttp`, e)
            }
          } catch {
            logger.error(`Child non-JSON: ${line}`)
          }
        })
      })

      child.stderr.on('data', (chunk: Buffer) => {
        logger.error(`Child stderr: ${chunk.toString('utf8')}`)
      })

      transport.onmessage = (msg: JSONRPCMessage) => {
        logger.info(`StreamableHttp → Child: ${JSON.stringify(msg)}`)
        child.stdin.write(JSON.stringify(msg) + '\n')
      }

      transport.onclose = () => {
        clearTimeout(initializeTimer)
        logger.info(`StreamableHttp connection closed (session ${sessionId})`)
        if (activeSessionId) {
          sessionCounter?.clear(
            activeSessionId,
            false,
            'transport being closed',
          )
          delete transports[activeSessionId]
        }
        stopChild()
      }

      transport.onerror = (err) => {
        clearTimeout(initializeTimer)
        logger.error(`StreamableHttp error (session ${sessionId}):`, err)
        if (activeSessionId) {
          sessionCounter?.clear(
            activeSessionId,
            false,
            'transport emitting error',
          )
          delete transports[activeSessionId]
        }
        stopChild()
      }
    } else {
      // Invalid request
      res.status(400).json({
        jsonrpc: '2.0',
        error: {
          code: -32000,
          message: 'Bad Request: No valid session ID provided',
        },
        id: null,
      })
      return
    }

    // Decrement session access count when response ends
    let responseEnded = false
    const handleResponseEnd = (event: string) => {
      if (!responseEnded && activeSessionId) {
        responseEnded = true
        logger.info(`Response ${event}`, activeSessionId)
        sessionCounter?.dec(activeSessionId, `POST response ${event}`)
      }
    }

    res.on('finish', () => handleResponseEnd('finished'))
    res.on('close', () => handleResponseEnd('closed'))

    // Handle the request
    await transport.handleRequest(req, res, req.body)
  })

  // Reusable handler for GET and DELETE requests
  const handleSessionRequest = async (
    req: express.Request,
    res: express.Response,
  ) => {
    const sessionId = req.headers['mcp-session-id'] as string | undefined
    if (!sessionId || !transports[sessionId]) {
      res.status(400).send('Invalid or missing session ID')
      return
    }

    // Increment session access count
    sessionCounter?.inc(sessionId, `${req.method} request for existing session`)

    // Decrement session access count when response ends
    let responseEnded = false
    const handleResponseEnd = (event: string) => {
      if (!responseEnded) {
        responseEnded = true
        logger.info(`Response ${event}`, sessionId)
        sessionCounter?.dec(sessionId, `${req.method} response ${event}`)
      }
    }

    res.on('finish', () => handleResponseEnd('finished'))
    res.on('close', () => handleResponseEnd('closed'))

    const transport = transports[sessionId]
    await transport.handleRequest(req, res)
  }

  // Handle GET requests for server-to-client notifications via SSE
  app.get(streamableHttpPath, handleSessionRequest)

  // Handle DELETE requests for session termination
  app.delete(streamableHttpPath, handleSessionRequest)

  app.listen(port, () => {
    logger.info(`Listening on port ${port}`)
    logger.info(
      `StreamableHttp endpoint: http://localhost:${port}${streamableHttpPath}`,
    )
  })
}
