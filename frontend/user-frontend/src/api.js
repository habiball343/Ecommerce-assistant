export const CUSTOMER_SERVICE_URL = import.meta.env.VITE_CUSTOMER_SERVICE_URL || 'http://127.0.0.1:8000'
export const ECOMMERCE_SERVICE_URL = import.meta.env.VITE_ECOMMERCE_SERVICE_URL || 'http://127.0.0.1:8001'

async function parseResponse(response) {
  const body = await response.json().catch(() => ({ detail: response.statusText }))
  if (!response.ok) {
    const detail = typeof body.detail === 'string' ? body.detail : body.detail?.message
    throw new Error(detail || body.message || response.statusText)
  }
  return body
}

export async function login(userId) {
  return parseResponse(await fetch(`${ECOMMERCE_SERVICE_URL}/api/v1/auth/demo-token`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ user_id: userId, role: 'user' }),
  }))
}

export async function listDemoUsers() {
  return parseResponse(await fetch(`${ECOMMERCE_SERVICE_URL}/api/v1/demo/users`))
}

export async function apiFetch(baseUrl, path, token, options = {}) {
  const headers = { Authorization: `Bearer ${token}`, ...(options.headers || {}) }
  return parseResponse(await fetch(`${baseUrl}${path}`, { ...options, headers }))
}

export const customerApi = {
  history: (token) => apiFetch(CUSTOMER_SERVICE_URL, '/api/v1/chat/history', token),
  send: (token, payload) => apiFetch(CUSTOMER_SERVICE_URL, '/api/v1/chat/messages', token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  }),
}

export const commerceApi = {
  orders: (token) => apiFetch(ECOMMERCE_SERVICE_URL, '/api/v1/orders', token),
  products: (token) => apiFetch(ECOMMERCE_SERVICE_URL, '/api/v1/products', token),
  order: (token, id) => apiFetch(ECOMMERCE_SERVICE_URL, `/api/v1/orders/${encodeURIComponent(id)}`, token),
  logistics: (token, id) => apiFetch(ECOMMERCE_SERVICE_URL, `/api/v1/orders/${encodeURIComponent(id)}/logistics`, token),
  afterSales: (token, orderId = '') => apiFetch(
    ECOMMERCE_SERVICE_URL,
    `/api/v1/after-sales${orderId ? `?order_id=${encodeURIComponent(orderId)}` : ''}`,
    token,
  ),
  cancel: (token, id) => apiFetch(ECOMMERCE_SERVICE_URL, `/api/v1/orders/${encodeURIComponent(id)}/cancel`, token, {
    method: 'POST',
    headers: { 'Idempotency-Key': newId('cancel') },
  }),
  updateAddress: (token, id, address) => apiFetch(ECOMMERCE_SERVICE_URL, `/api/v1/orders/${encodeURIComponent(id)}/address`, token, {
    method: 'PATCH',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': newId('address') },
    body: JSON.stringify({ address }),
  }),
  createAfterSale: (token, id, payload) => apiFetch(ECOMMERCE_SERVICE_URL, `/api/v1/orders/${encodeURIComponent(id)}/after-sales`, token, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json', 'Idempotency-Key': newId('after_sale') },
    body: JSON.stringify(payload),
  }),
}

export function createRealtimeConnection(token, callbacks = {}) {
  let socket
  let stopped = false
  let retryTimer
  let attempts = 0
  let hasConnected = false
  const websocketUrl = `${CUSTOMER_SERVICE_URL.replace(/^http/, 'ws')}/api/v1/realtime`

  const clearTimers = () => {
    window.clearTimeout(retryTimer)
  }
  const connect = () => {
    if (stopped) return
    callbacks.onState?.(attempts ? 'reconnecting' : 'connecting')
    socket = new WebSocket(websocketUrl)
    socket.addEventListener('open', () => socket.send(JSON.stringify({ type: 'authenticate', token })))
    socket.addEventListener('message', (browserEvent) => {
      const event = JSON.parse(browserEvent.data)
      if (event.event_type === 'connected') {
        const reconnected = hasConnected
        hasConnected = true
        attempts = 0
        callbacks.onState?.('connected')
        callbacks.onConnected?.({ reconnected })
      } else {
        callbacks.onEvent?.(event)
      }
    })
    socket.addEventListener('close', () => {
      clearTimers()
      if (stopped) return callbacks.onState?.('closed')
      callbacks.onState?.('reconnecting')
      retryTimer = window.setTimeout(connect, Math.min(1000 * (2 ** attempts++), 10000))
    })
    socket.addEventListener('error', () => socket?.close())
  }
  connect()
  return { close() { stopped = true; clearTimers(); socket?.close(1000, 'client closed') } }
}

export function newId(prefix) {
  return `${prefix}_${Date.now()}_${Math.random().toString(16).slice(2)}`
}
