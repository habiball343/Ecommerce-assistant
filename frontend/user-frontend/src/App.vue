<script setup>
import DOMPurify from 'dompurify'
import { marked } from 'marked'
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref, watch } from 'vue'
import { commerceApi, createRealtimeConnection, customerApi, listDemoUsers, login, newId } from './api'

marked.setOptions({ gfm: true, breaks: true })

const mallProductIds = new Set(['PRODUCT_001', 'PRODUCT_002', 'PRODUCT_003', 'PRODUCT_004', 'PRODUCT_005'])
const session = reactive({ token: localStorage.getItem('customer_token') || '', user: null })
const demoUsers = ref([])
const selectedUser = ref(localStorage.getItem('customer_id') || 'user_001')
const route = ref(`${location.pathname}${location.search}`)
const loading = ref(false)
const error = ref('')
const notice = ref('')
const orders = ref([])
const products = ref([])
const order = ref(null)
const logistics = ref(null)
const afterSales = ref([])
const messages = ref([])
const historyMessages = ref([])
const expandedConversationIds = ref(new Set())
const conversationId = ref(null)
const conversationMode = ref('BOT')
const draft = ref('')
const chatState = ref('closed')
const aiProcessingState = ref('idle')
const aiProcessingMessage = ref('')
const choiceSending = ref(false)
const pendingConsult = ref(null)
const messageList = ref(null)
const afterSalesPanel = ref(null)
const form = reactive({ address: '', kind: 'refund', reason: '' })
let realtime

const path = computed(() => route.value.split('?')[0])
const query = computed(() => new URLSearchParams(route.value.split('?')[1] || ''))
const orderMatch = computed(() => path.value.match(/^\/me\/orders\/([^/]+)(?:\/(cancel|address|after-sale))?$/))
const orderId = computed(() => orderMatch.value?.[1] || '')
const operation = computed(() => orderMatch.value?.[2] || '')
const selectedProfile = computed(() => demoUsers.value.find((user) => user.id === selectedUser.value))
const canCancel = computed(() => ['paid', 'pending_shipment'].includes(order.value?.status))
const canEditAddress = computed(() => ['paid', 'pending_shipment'].includes(order.value?.status))
const canApplyAfterSale = computed(() => !['cancelled', 'refunded'].includes(order.value?.status))
const serviceLabel = computed(() => conversationModeLabel(conversationMode.value))
const historyGroups = computed(() => {
  const groups = new Map()
  for (const message of historyMessages.value) {
    if (!groups.has(message.conversation_id)) {
      groups.set(message.conversation_id, {
        id: message.conversation_id,
        messages: []
      })
    }
    groups.get(message.conversation_id).messages.push(message)
  }
  return [...groups.values()]
    .map((group) => ({
      ...group,
      title: messageText(group.messages[0]),
      preview: messageText(group.messages[group.messages.length - 1])
    }))
    .reverse()
})
const pageTitle = computed(() => {
  if (path.value === '/mall') return '商城'
  if (path.value === '/chat' || path.value === '/') return '智能客服'
  if (path.value === '/history') return '历史会话'
  if (path.value === '/me') return '我的'
  if (operation.value === 'cancel') return '取消订单'
  if (operation.value === 'address') return '修改收货地址'
  if (operation.value === 'after-sale') return '申请售后'
  return '订单详情'
})

function navigate(to, { keepNotice = false } = {}) {
  if (!keepNotice) notice.value = ''
  history.pushState({}, '', to)
  route.value = `${location.pathname}${location.search}`
}

function conversationModeLabel(mode) {
  return {
    AI: 'AI 客服',
    BOT: 'AI 客服',
    QUEUED: '等待人工',
    HUMAN: '人工服务中',
    CLOSED: '已结束',
  }[mode] || mode
}

function unwrap(result) {
  if (!result.success) throw new Error(result.message || '请求失败')
  return result.data
}

function unwrapAfterSales(result) {
  if (result.code === 'AFTER_SALE_NOT_FOUND') return []
  return unwrap(result)
}

function messageText(message) {
  const content = message.content || {}
  if (content.text) return content.text
  if (Array.isArray(content.blocks)) return content.blocks.map((block) => block.content).join('\n\n')
  if (content.object_type === 'order') return `已发送订单 ${content.object_id}`
  if (content.object_type === 'product') return `已发送商品 ${content.object_id}`
  return '暂不支持的消息'
}

function messageHtml(message) {
  return DOMPurify.sanitize(marked.parse(messageText(message)), {
    USE_PROFILES: { html: true }
  })
}

function toggleHistoryGroup(conversationId) {
  const next = new Set(expandedConversationIds.value)
  if (next.has(conversationId)) next.delete(conversationId)
  else next.add(conversationId)
  expandedConversationIds.value = next
}

function appendMessage(message) {
  messages.value.push(message)
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
}

function updateAiProcessing(state = 'idle', message = '') {
  aiProcessingState.value = state
  aiProcessingMessage.value = message
  nextTick(() => messageList.value?.scrollTo({ top: messageList.value.scrollHeight, behavior: 'smooth' }))
}

function handleRealtimeEvent(message) {
  if (!message || !message.content) return

  if (message.conversation_mode) {
    conversationMode.value = message.conversation_mode
  }
  appendMessage(message)
  if (path.value === '/history') {
    loadHistory().catch((cause) => {
      error.value = `历史会话刷新失败：${cause.message}`
    })
  }
  if (message.role === 'AI' && message.content?.kind === 'error') {
    updateAiProcessing('failed', messageText(message))
  } else {
    updateAiProcessing()
  }
}

async function establishSession() {
  error.value = ''
  loading.value = true
  updateAiProcessing()
  try {
    const result = await login(selectedUser.value)
    session.token = result.access_token
    session.user = result
    localStorage.setItem('customer_token', session.token)
    localStorage.setItem('customer_id', selectedUser.value)
    realtime?.close()
    realtime = createRealtimeConnection(session.token, {
      onState: (state) => { chatState.value = state },
      onEvent: handleRealtimeEvent,
    })
    await loadPage()
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function loadHistory() {
  if (!session.token) return
  const data = await customerApi.history(session.token)
  conversationId.value = data.conversation_id
  conversationMode.value = data.conversation_mode
  historyMessages.value = data.messages
  messages.value = data.messages.filter((message) => message.conversation_id === conversationId.value)
  if (!expandedConversationIds.value.size && conversationId.value) {
    expandedConversationIds.value = new Set([conversationId.value])
  }
  await nextTick()
  messageList.value?.scrollTo({ top: messageList.value.scrollHeight })
}

async function loadChat() {
  const [historyResult, orderResult] = await Promise.allSettled([
    loadHistory(),
    commerceApi.orders(session.token),
  ])

  const failures = []
  if (historyResult.status === 'rejected') {
    failures.push(`聊天记录加载失败：${historyResult.reason?.message || '未知错误'}`)
  }

  if (orderResult.status === 'fulfilled') {
    try {
      orders.value = unwrap(orderResult.value)
    } catch (cause) {
      failures.push(`订单加载失败：${cause.message}`)
    }
  } else {
    failures.push(`订单加载失败：${orderResult.reason?.message || '未知错误'}`)
  }

  if (failures.length) error.value = failures.join('；')
}

async function loadOrder() {
  const [orderResult, afterSaleResult] = await Promise.all([
    commerceApi.order(session.token, orderId.value),
    commerceApi.afterSales(session.token, orderId.value),
  ])
  order.value = unwrap(orderResult)
  afterSales.value = unwrapAfterSales(afterSaleResult)
  form.address = order.value.address || ''
  logistics.value = null
  if (!operation.value) {
    try { logistics.value = unwrap(await commerceApi.logistics(session.token, orderId.value)) } catch { /* no logistics yet */ }
    if (query.value.get('section') === 'after-sales') {
      await nextTick()
      afterSalesPanel.value?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }
  }
}

async function loadPage() {
  if (!session.token) return
  error.value = ''
  loading.value = true
  try {
    if (path.value === '/mall') {
      const catalog = unwrap(await commerceApi.products(session.token))
      products.value = catalog.filter((product) => mallProductIds.has(product.id))
    }
    else if (path.value === '/' || path.value === '/chat') {
      await loadChat()
      if (pendingConsult.value) {
        const { objectType, objectId } = pendingConsult.value
        pendingConsult.value = null
        await sendPayload('object', { object_type: objectType, object_id: objectId })
      }
    }
    else if (path.value === '/history') await loadHistory()
    else if (path.value === '/me') orders.value = unwrap(await commerceApi.orders(session.token))
    else if (orderId.value) await loadOrder()
    else navigate('/mall')
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

async function sendPayload(type, content) {
  if (conversationMode.value === 'AI') {
    updateAiProcessing('active', 'AI 正在处理你的问题')
  } else {
    updateAiProcessing()
  }
  const optimistic = {
    message_id: newId('msg'),
    role: 'USER',
    type,
    content,
    delivery_status: 'sending',
  }
  appendMessage(optimistic)
  try {
    await customerApi.send(session.token, {
      message_id: optimistic.message_id,
      type,
      content,
    })
    const current = messages.value.find((item) => item.message_id === optimistic.message_id)
    if (current?.delivery_status === 'sending') current.delivery_status = 'sent'
  } catch (cause) {
    updateAiProcessing()
    const current = messages.value.find((item) => item.message_id === optimistic.message_id)
    if (current?.delivery_status === 'sending') current.delivery_status = 'failed'
    error.value = cause.message
  }
}

async function sendMessage() {
  const text = draft.value.trim()
  if (!text || loading.value) return
  draft.value = ''
  await sendPayload('text', { text })
}

function consultObject(objectType, objectId) {
  pendingConsult.value = { objectType, objectId }
  navigate('/chat')
}

function productArt(productId) {
  return {
    PRODUCT_001: 'headphones',
    PRODUCT_002: 'earbuds',
    PRODUCT_003: 'watch',
    PRODUCT_004: 'keyboard',
    PRODUCT_005: 'mouse',
  }[productId] || 'headphones'
}

async function submitCancel() {
  if (!confirm(`确认取消订单 ${orderId.value}？此操作会立即生效。`)) return
  await runWrite(async () => unwrap(await commerceApi.cancel(session.token, orderId.value)), '订单已取消', `/me/orders/${orderId.value}`)
}

async function submitAddress() {
  await runWrite(async () => unwrap(await commerceApi.updateAddress(session.token, orderId.value, form.address)), '收货地址已更新', `/me/orders/${orderId.value}`)
}

async function submitAfterSale() {
  await runWrite(
    async () => unwrap(await commerceApi.createAfterSale(session.token, orderId.value, { kind: form.kind, reason: form.reason })),
    '售后申请已提交',
    `/me/orders/${orderId.value}?section=after-sales`,
  )
}

async function runWrite(action, successMessage, target) {
  loading.value = true
  error.value = ''
  try {
    await action()
    notice.value = successMessage
    navigate(target, { keepNotice: true })
  } catch (cause) {
    error.value = cause.message
  } finally {
    loading.value = false
  }
}

function actionHref(message) {
  return message.content?.page_action?.href
}

function messageChoices(message) {
  const choices = message.content?.choices
  if (!Array.isArray(choices)) return []
  return choices.filter((choice) => (
    ['order', 'product'].includes(choice?.object_type)
    && typeof choice?.object_id === 'string'
    && typeof choice?.label === 'string'
  ))
}

async function selectChoice(choice) {
  if (choiceSending.value) return
  choiceSending.value = true
  try {
    const content = {
      object_type: choice.object_type,
      object_id: choice.object_id,
    }
    if (typeof choice.query_text === 'string') {
      content.query_text = choice.query_text
    }
    await sendPayload('object', content)
  } finally {
    choiceSending.value = false
  }
}

function afterSaleKind(kind) {
  return { refund: '仅退款', return: '退货退款', exchange: '换货' }[kind] || kind
}

function afterSaleStatus(status) {
  return { pending: '待处理', approved: '已通过', processing: '处理中', completed: '已完成', rejected: '未通过' }[status] || status
}

function logisticsStatus(status) {
  return { pending: '待揽收', picked_up: '已揽收', in_transit: '运输中', delivered: '已签收', exception: '物流异常' }[status] || status
}

onMounted(async () => {
  window.addEventListener('popstate', () => { route.value = `${location.pathname}${location.search}` })
  demoUsers.value = await listDemoUsers().catch(() => [])
  await establishSession()
})
onBeforeUnmount(() => realtime?.close())
watch(route, loadPage)
</script>

<template>
  <div class="app-shell">
    <header class="topbar">
      <button class="brand" @click="navigate('/mall')"><span>小二</span> 商城</button>
      <nav aria-label="主导航">
        <button :class="{ active: path === '/mall' }" :aria-current="path === '/mall' ? 'page' : undefined" @click="navigate('/mall')">商城</button>
        <button :class="{ active: path === '/chat' || path === '/' }" :aria-current="path === '/chat' || path === '/' ? 'page' : undefined" @click="navigate('/chat')">客服</button>
        <button :class="{ active: path === '/history' }" :aria-current="path === '/history' ? 'page' : undefined" @click="navigate('/history')">历史</button>
        <button :class="{ active: path.startsWith('/me') }" :aria-current="path.startsWith('/me') ? 'page' : undefined" @click="navigate('/me')">我的</button>
      </nav>
      <div class="account">
        <select v-model="selectedUser" aria-label="切换体验账号" @change="establishSession">
          <option v-for="user in demoUsers" :key="user.id" :value="user.id">{{ user.name }}</option>
        </select>
      </div>
    </header>

    <main :class="['page', { 'chat-page': path === '/chat' || path === '/' }]">
      <div v-if="error" class="alert error" role="alert">{{ error }} <button aria-label="关闭错误提示" @click="error = ''">×</button></div>
      <div v-if="notice" class="alert success" role="status">{{ notice }} <button aria-label="关闭成功提示" @click="notice = ''">×</button></div>

      <template v-if="path === '/mall'">
        <section class="mall-hero">
          <div>
            <p class="eyebrow">XIAOER SELECT</p>
            <h1>挑一件，问清楚</h1>
            <p>这里仅展示商品的必要信息。想了解规格、功能或适用场景，直接带着商品去问客服。</p>
          </div>
          <span>{{ products.length }} 件精选</span>
        </section>
        <section class="mall-grid" aria-label="精选商品">
          <article v-for="(item, index) in products" :key="item.id" class="mall-card">
            <div class="mall-art" :data-index="String(index + 1).padStart(2, '0')">
              <svg role="img" :aria-label="`${item.name} 商品插画`" viewBox="0 0 320 220">
                <use :href="`/product-sprite.svg#${productArt(item.id)}`" />
              </svg>
            </div>
            <div class="mall-card-body">
              <p>{{ item.category }}</p>
              <h2>{{ item.name }}</h2>
              <div class="mall-card-footer">
                <strong>¥{{ item.price.toFixed(2) }}</strong>
                <button @click="consultObject('product', item.id)">咨询商品 <span>→</span></button>
              </div>
            </div>
          </article>
          <div v-if="!products.length" class="empty-state"><h2>暂无商品</h2><p>精选商品正在准备中。</p></div>
        </section>
      </template>

      <template v-else-if="path === '/chat' || path === '/'">
        <section class="chat-layout">
          <div class="chat-workspace">
            <section class="chat-intro">
              <div><p class="eyebrow">AI CUSTOMER SERVICE</p><h1>有问题，直接问</h1><p>我会查询订单和规则，说明结果，并带你到正确的业务页面。</p></div>
              <div class="chat-status">
                <span :class="['service-mode', conversationMode.toLowerCase()]">{{ serviceLabel }}</span>
                <span :class="['connection', chatState]">{{ chatState === 'connected' ? '在线' : '连接中' }}</span>
              </div>
            </section>
            <section ref="messageList" class="messages" aria-label="对话消息" aria-live="polite">
              <div v-if="!messages.length" class="empty-chat">
                <strong>可以试试</strong>
                <button @click="draft = '我的订单什么时候发货？'; sendMessage()">我的订单什么时候发货？</button>
                <button @click="draft = '我想取消订单'; sendMessage()">我想取消订单</button>
                <button @click="draft = '我想了解平台售后规则'; sendMessage()">我想了解平台售后规则</button>
              </div>
              <article v-for="(message, index) in messages" :key="index" :class="['message', message.role.toLowerCase()]">
                <span class="avatar">{{ message.role === 'USER' ? '我' : message.role === 'HUMAN' ? '人' : 'AI' }}</span>
                <div class="bubble">
                  <div v-if="message.role === 'USER'" class="message-text">{{ messageText(message) }}</div>
                  <div v-else class="message-text markdown-body" v-html="messageHtml(message)"></div>
                  <small v-if="message.delivery_status === 'sending'" class="delivery-state">发送中</small>
                  <small v-else-if="message.delivery_status === 'failed'" class="delivery-state failed">发送失败</small>
                  <div v-if="messageChoices(message).length" class="choice-list" aria-label="请选择咨询对象">
                    <button
                      v-for="choice in messageChoices(message)"
                      :key="`${choice.object_type}:${choice.object_id}`"
                      :disabled="choiceSending"
                      @click="selectChoice(choice)"
                    >
                      <span><strong>{{ choice.label }}</strong><small>{{ choice.description }}</small></span>
                      <b>→</b>
                    </button>
                  </div>
                  <button v-if="actionHref(message)" class="action-card" @click="navigate(actionHref(message))">
                    <span><strong>{{ message.content.page_action.label }}</strong><small>{{ message.content.page_action.description }}</small></span><b>→</b>
                  </button>
                </div>
              </article>
              <article
                v-if="aiProcessingMessage"
                :class="['message', 'ai-processing', aiProcessingState]"
                role="status"
                aria-live="polite"
              >
                <span class="avatar">AI</span>
                <div class="bubble processing-bubble">
                  <span v-if="aiProcessingState === 'active'" class="processing-dots" aria-hidden="true">
                    <i></i><i></i><i></i>
                  </span>
                  <span v-else class="processing-failed-mark" aria-hidden="true">!</span>
                  <span>{{ aiProcessingMessage }}</span>
                </div>
              </article>
            </section>
            <form class="composer" @submit.prevent="sendMessage">
              <textarea v-model="draft" rows="2" aria-label="输入客服问题" placeholder="描述你的问题；业务操作会在对应页面确认" @keydown.enter.exact.prevent="sendMessage" />
              <button :disabled="!draft.trim()">发送</button>
            </form>
          </div>
        </section>
      </template>

      <template v-else-if="path === '/history'">
        <section class="history-hero">
          <div>
            <p class="eyebrow">CONVERSATION ARCHIVE</p>
            <h1>历史会话</h1>
            <p>按照会话开始时间整理，展开即可回看当时的完整咨询记录。</p>
          </div>
          <span>{{ historyGroups.length }} 次会话</span>
        </section>

        <section class="history-list" aria-label="历史会话列表">
          <article
            v-for="(group, index) in historyGroups"
            :key="group.id"
            :class="['history-group', { expanded: expandedConversationIds.has(group.id) }]"
          >
            <button
              class="history-group-head"
              :aria-expanded="expandedConversationIds.has(group.id)"
              @click="toggleHistoryGroup(group.id)"
            >
              <span class="history-index">{{ String(index + 1).padStart(2, '0') }}</span>
              <span class="history-summary">
                <span>
                  <strong>{{ group.title }}</strong>
                  <b v-if="group.id === conversationId">当前会话</b>
                </span>
                <small>{{ group.preview }}</small>
              </span>
              <span class="history-count">{{ group.messages.length }} 条 <b>⌄</b></span>
            </button>

            <div v-if="expandedConversationIds.has(group.id)" class="history-transcript">
              <article
                v-for="(message, msgIndex) in group.messages"
                :key="msgIndex"
                :class="['history-message', message.role.toLowerCase()]"
              >
                <span>{{ message.role === 'USER' ? '我' : message.role === 'HUMAN' ? '人工客服' : 'AI 客服' }}</span>
                <div>
                  <p>{{ messageText(message) }}</p>
                </div>
              </article>
            </div>
          </article>

          <div v-if="!historyGroups.length" class="empty-state">
            <h2>还没有历史会话</h2>
            <p>完成第一次咨询后，会话记录会显示在这里。</p>
          </div>
        </section>
      </template>

      <template v-else-if="path === '/me'">
        <section class="profile-card">
          <div class="profile-avatar">{{ session.user?.display_name?.slice(0, 1) || '我' }}</div>
          <div><p class="eyebrow">MEMBER CENTER</p><h1>{{ session.user?.display_name || '我的' }}</h1><p>{{ selectedProfile?.membership || '普通会员' }} · {{ selectedUser }}</p></div>
        </section>
        <header class="section-header"><div><h2>我的订单</h2><p>查看订单详情，并在具体订单内处理取消、地址与售后。</p></div><span>{{ orders.length }} 笔</span></header>
        <section class="card-list">
          <article v-for="item in orders" :key="item.id" class="order-card">
            <div class="order-meta"><span>{{ item.id }}</span><b>{{ item.status_label }}</b></div>
            <h3>{{ item.items.map((entry) => entry.product_name).join('、') }}</h3>
            <div class="order-footer"><span>{{ new Date(item.created_at).toLocaleDateString() }}</span><strong>¥{{ item.total_amount.toFixed(2) }}</strong></div>
            <div class="order-actions">
              <button class="order-detail" @click="navigate(`/me/orders/${item.id}`)">订单详情</button>
              <button class="consult-order" @click="consultObject('order', item.id)">咨询订单 <span>→</span></button>
            </div>
          </article>
          <div v-if="!orders.length" class="empty-state"><h2>暂无订单</h2><p>当前账号还没有订单记录。</p></div>
        </section>
      </template>

      <template v-else-if="order">
        <header class="page-header"><div><button class="back" @click="navigate(operation ? `/me/orders/${order.id}` : '/me')">← 返回</button><p class="eyebrow">{{ order.id }}</p><h1>{{ pageTitle }}</h1></div><span class="status-pill">{{ order.status_label }}</span></header>

        <section v-if="!operation" class="detail-grid">
          <article class="panel wide"><h2>商品信息</h2><div v-for="item in order.items" :key="item.product_id" class="line-item"><span><strong>{{ item.product_name }}</strong><small>{{ item.product_id }} · ×{{ item.quantity }}</small></span><b>¥{{ (item.unit_price * item.quantity).toFixed(2) }}</b></div><div class="total"><span>实付款</span><strong>¥{{ order.total_amount.toFixed(2) }}</strong></div></article>
          <article class="panel"><h2>收货地址</h2><p>{{ order.address }}</p><button v-if="canEditAddress" class="text-button" @click="navigate(`/me/orders/${order.id}/address`)">修改地址 →</button><small v-else>当前订单状态不可修改</small></article>
          <article class="panel"><h2>物流信息</h2><template v-if="logistics"><strong>{{ logisticsStatus(logistics.status) }}</strong><p>{{ logistics.company }} · {{ logistics.tracking_number }}</p><small>{{ logistics.latest_event }}</small></template><p v-else>暂时没有物流信息</p></article>
          <article ref="afterSalesPanel" class="panel wide after-sales-panel"><div class="panel-title"><h2>售后记录</h2><button v-if="canApplyAfterSale" @click="navigate(`/me/orders/${order.id}/after-sale`)">申请售后</button></div><div v-for="item in afterSales" :key="item.id" class="after-sale-row"><span class="status-pill">{{ afterSaleStatus(item.status) }}</span><div><strong>{{ afterSaleKind(item.kind) }}</strong><p>{{ item.reason }}</p><small>{{ item.id }} · {{ new Date(item.created_at).toLocaleString() }}</small></div></div><p v-if="!afterSales.length" class="muted">该订单暂无售后记录。</p></article>
          <article class="panel actions wide"><h2>订单操作</h2><button v-if="canCancel" class="danger-outline" @click="navigate(`/me/orders/${order.id}/cancel`)">取消订单</button><span v-else class="muted">当前没有其他可用操作</span></article>
        </section>

        <section v-else class="operation-layout">
          <article class="panel order-summary"><span>{{ order.status_label }}</span><h2>{{ order.items.map((item) => item.product_name).join('、') }}</h2><p>订单 {{ order.id }} · ¥{{ order.total_amount.toFixed(2) }}</p></article>
          <article v-if="operation === 'cancel'" class="panel operation-panel"><h2>确认取消</h2><p>{{ canCancel ? '提交前已重新读取订单状态。取消成功后不可恢复。' : '订单当前状态不能直接取消。' }}</p><button class="danger" :disabled="loading || !canCancel" @click="submitCancel">确认取消订单</button></article>
          <form v-else-if="operation === 'address'" class="panel operation-panel" @submit.prevent="canEditAddress && submitAddress()"><h2>新的收货地址</h2><p v-if="!canEditAddress">订单当前状态不能修改收货地址。</p><label>详细地址<textarea v-model="form.address" rows="4" minlength="5" maxlength="300" required :disabled="!canEditAddress" /></label><button :disabled="loading || !canEditAddress">保存地址</button></form>
          <form v-else class="panel operation-panel" @submit.prevent="canApplyAfterSale && submitAfterSale()"><h2>售后信息</h2><p v-if="!canApplyAfterSale">该订单当前不能发起售后申请。</p><label>售后类型<select v-model="form.kind" :disabled="!canApplyAfterSale"><option value="refund">仅退款</option><option value="return">退货退款</option><option value="exchange">换货</option></select></label><label>问题描述<textarea v-model="form.reason" rows="5" minlength="2" maxlength="300" placeholder="请说明实际情况" required :disabled="!canApplyAfterSale" /></label><button :disabled="loading || !canApplyAfterSale">提交售后申请</button></form>
        </section>
      </template>
    </main>
  </div>
</template>
