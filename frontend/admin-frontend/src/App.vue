<script setup>
import { computed, nextTick, onBeforeUnmount, onMounted, reactive, ref } from 'vue'
import { adminApi, createRealtimeConnection, login } from './api'

const section = ref('handoffs')
const account = ref(localStorage.getItem('staff_id') || 'admin_001')
const session = reactive({ token: '', principal: null, name: '' })
const accepting = ref(false)
const error = ref('')
const notice = ref('')
const handoffs = ref([])
const selectedHandoff = ref(null)
const conversation = ref(null)
const transcriptRef = ref(null)
const reply = ref('')
const runs = ref([])
const selectedRun = ref(null)
const knowledgeChunks = ref([])
const pdfFile = ref(null)
const uploading = ref(false)
const metrics = reactive({ service: {}, commerce: {}, ai: {} })
let realtime

const isAdmin = computed(() => account.value.startsWith('admin_'))
const canManageSelectedHandoff = computed(() => (
  selectedHandoff.value?.status === 'ACTIVE'
  && selectedHandoff.value.staff_id === session.principal?.user_id
))

function messageText(message) {
  const content = message.content || {}
  return content.text || content.blocks?.map((item) => item.content).join('\n\n') || '对象消息'
}

function isSystemNotice(message) {
  if (message.role !== 'HUMAN') return false
  const text = messageText(message)
  return text.includes('已经接入') || text.includes('已接入') || text.includes('由 AI')
}

function scrollToBottom() {
  nextTick(() => transcriptRef.value?.scrollTo({ top: transcriptRef.value.scrollHeight, behavior: 'smooth' }))
}

async function authenticate() {
  section.value = isAdmin.value ? 'observability' : 'handoffs'
  localStorage.setItem('staff_id', account.value)
  await execute(async () => {
    const result = await login(account.value)
    session.token = result.access_token
    session.principal = result.principal
    session.name = result.display_name
    realtime?.close()
    realtime = null
    if (!isAdmin.value) {
      let hasConnected = false
      realtime = createRealtimeConnection(session.token, {
        onState: (state) => {
          if (state !== 'connected') return
          if (hasConnected) loadHandoffs().catch((cause) => { error.value = cause.message })
          hasConnected = true
        },
        onEvent: () => {
          if (section.value === 'handoffs') {
            loadHandoffs().catch((cause) => { error.value = cause.message })
          }
        },
      })
    }
    await loadSection()
  })
}

async function loadSection() {
  selectedRun.value = null
  if (section.value === 'handoffs') await loadHandoffs()
  if (section.value === 'observability') await loadObservability()
  if (section.value === 'knowledge') await loadKnowledge()
}

async function loadKnowledge() {
  await execute(async () => {
    knowledgeChunks.value = await adminApi.knowledgeList(session.token)
  })
}

function onFileChange(event) {
  pdfFile.value = event.target.files?.[0] || null
}

async function submitKnowledgeUpload() {
  if (!pdfFile.value) return
  uploading.value = true
  error.value = ''
  try {
    const res = await adminApi.uploadKnowledge(session.token, pdfFile.value)
    notice.value = `已上传 ${res.file_name}，切分并向量化 ${res.chunks} 个片段`
    pdfFile.value = null
    await loadKnowledge()
  } catch (cause) {
    error.value = cause.message
  } finally {
    uploading.value = false
  }
}

async function loadHandoffs() {
  handoffs.value = await adminApi.handoffs(session.token)
  if (selectedHandoff.value) {
    selectedHandoff.value = handoffs.value.find((item) => item.id === selectedHandoff.value.id) || null
    if (selectedHandoff.value) await openHandoff(selectedHandoff.value)
  }
}

async function openHandoff(item) {
  selectedHandoff.value = item
  conversation.value = await adminApi.messages(session.token, item.id)
  scrollToBottom()
}

async function acceptHandoff() {
  accepting.value = true
  error.value = ''
  try {
    await adminApi.accept(session.token, selectedHandoff.value.id)
    notice.value = '会话已接入'
    await loadHandoffs()
  } catch (cause) {
    if (cause.status === 409) {
      const handoffId = selectedHandoff.value?.id
      await loadHandoffs()
      const handoff = handoffs.value.find((item) => item.id === handoffId)
      notice.value = handoff?.staff_id
        ? `该工单已被客服 ${handoff.staff_id} 接入`
        : '该工单已被其他客服接入'
    } else {
      error.value = cause.message
    }
  } finally {
    accepting.value = false
  }
}

async function sendReply() {
  const text = reply.value.trim()
  if (!text) return
  await execute(async () => {
    await adminApi.reply(session.token, selectedHandoff.value.id, text)
    reply.value = ''
    conversation.value.messages.push({ role: 'HUMAN', content: { text } })
    scrollToBottom()
  })
}

async function resolveHandoff() {
  if (!confirm('确认结束人工服务并交还 AI 客服？')) return
  await execute(async () => {
    await adminApi.resolve(session.token, selectedHandoff.value.id)
    selectedHandoff.value = null
    conversation.value = null
    notice.value = '人工服务已结束'
    await loadHandoffs()
  })
}

async function loadObservability() {
  const requests = [
    adminApi.runs(session.token)
      .then((value) => { runs.value = value })
      .catch(() => { runs.value = [] }),
  ]
  if (isAdmin.value) {
    requests.push(
      adminApi.serviceMetrics(session.token)
        .then((value) => { metrics.service = value })
        .catch(() => { metrics.service = {} }),
      adminApi.commerceSummary(session.token)
        .then((value) => { metrics.commerce = value })
        .catch(() => { metrics.commerce = {} }),
      adminApi.aiMetrics(session.token)
        .then((value) => { metrics.ai = value })
        .catch(() => { metrics.ai = {} }),
    )
  }
  await Promise.allSettled(requests)
}

async function inspectRun(run) {
  await execute(async () => { selectedRun.value = await adminApi.run(session.token, run.id) })
}

async function execute(action) {
  error.value = ''
  try { await action() } catch (cause) { error.value = cause.message }
}

onMounted(authenticate)
onBeforeUnmount(() => {
  realtime?.close()
})
</script>

<template>
  <div class="admin-shell">
    <header class="topbar">
      <div class="logo"><span>小二</span><div><strong>客服后台</strong><small>CONTROL CENTER</small></div></div>
      <nav aria-label="后台主导航">
        <button v-if="!isAdmin" :class="{ active: section === 'handoffs' }" :aria-current="section === 'handoffs' ? 'page' : undefined" @click="section = 'handoffs'; loadSection()">人工工作台</button>
        <button v-if="isAdmin" :class="{ active: section === 'observability' }" :aria-current="section === 'observability' ? 'page' : undefined" @click="section = 'observability'; loadSection()">监控指标</button>
        <button v-if="isAdmin" :class="{ active: section === 'knowledge' }" :aria-current="section === 'knowledge' ? 'page' : undefined" @click="section = 'knowledge'; loadSection()">知识库</button>
      </nav>
      <div class="account">
        <select v-model="account" aria-label="切换后台账号" @change="authenticate"><option value="admin_001">系统管理员</option><option value="agent_001">客服小周</option></select>
        <small>{{ session.principal?.role || 'loading' }}</small>
      </div>
    </header>

    <main class="admin-main">
      <div v-if="error" class="alert error" role="alert">{{ error }} <button aria-label="关闭错误提示" @click="error = ''">×</button></div>
      <div v-if="notice" class="alert success" role="status">{{ notice }} <button aria-label="关闭成功提示" @click="notice = ''">×</button></div>

      <section v-if="section === 'handoffs'" class="workspace">
        <div class="queue panel">
          <div class="panel-title"><h2>待处理会话</h2><span>{{ handoffs.length }}</span></div>
          <div class="queue-scroll">
            <button v-for="item in handoffs" :key="item.id" :class="['queue-item', { active: selectedHandoff?.id === item.id }]" @click="openHandoff(item)">
              <span class="customer-avatar">{{ item.user_id.slice(-2) }}</span><span><strong>{{ item.user_id }}</strong><small>{{ item.summary }}</small></span><b>{{ item.status === 'ACTIVE' ? `处理中 · ${item.staff_id}` : '等待' }}</b>
            </button>
            <div v-if="!handoffs.length" class="empty"><strong>队列已清空</strong><span>当前没有需要人工介入的会话</span></div>
          </div>
        </div>
        <div class="conversation panel">
          <template v-if="selectedHandoff && conversation">
            <div class="conversation-head">
              <div><strong>{{ selectedHandoff.user_id }}</strong><small>{{ selectedHandoff.summary }}</small></div>
              <div>
                <button v-if="selectedHandoff.status === 'WAITING'" :disabled="accepting" @click="acceptHandoff">{{ accepting ? '接入中…' : '接入会话' }}</button>
                <button v-else-if="canManageSelectedHandoff" class="ghost" @click="resolveHandoff">结束服务</button>
                <template v-else>
                  <small>由 {{ selectedHandoff.staff_id }} 处理中</small>
                </template>
              </div>
            </div>
            <div ref="transcriptRef" class="transcript" aria-label="会话记录" aria-live="polite">
              <template v-for="(message, index) in conversation.messages" :key="index">
                <div v-if="isSystemNotice(message)" class="system-notice"><span>{{ messageText(message) }}</span></div>
                <article v-else :class="message.role.toLowerCase()"><small>{{ message.role }}</small><p>{{ messageText(message) }}</p></article>
              </template>
            </div>
            <form class="reply-box" @submit.prevent="sendReply"><textarea v-model="reply" rows="2" aria-label="回复用户" :disabled="!canManageSelectedHandoff" :placeholder="canManageSelectedHandoff ? '回复用户' : selectedHandoff.status === 'WAITING' ? '接入后回复用户' : `由 ${selectedHandoff.staff_id} 处理中`" /><button :disabled="!canManageSelectedHandoff || !reply.trim()">发送</button></form>
          </template>
          <div v-else class="empty large"><strong>选择一条会话</strong><span>查看上下文并接入人工服务</span></div>
        </div>
      </section>

      <section v-else-if="section === 'knowledge'" class="knowledge-layout">
        <form class="panel upload" @submit.prevent="submitKnowledgeUpload">
          <p>RAG KNOWLEDGE</p>
          <div><h2>上传规则 PDF</h2><small>自动提取文本、分段切块并写入向量</small></div>
          <label>选择 PDF 文档<input type="file" accept=".pdf" @change="onFileChange" /></label>
          <button :disabled="!pdfFile || uploading">{{ uploading ? '向量化入库中…' : '上传并构建索引' }}</button>
        </form>
        <div class="panel documents">
          <div class="panel-title"><h2>已入库知识切片</h2><span>{{ knowledgeChunks.length }}</span></div>
          <div class="document-scroll">
            <table v-if="knowledgeChunks.length">
              <thead><tr><th>来源文件 / ID</th><th>切片内容</th></tr></thead>
              <tbody>
                <tr v-for="chunk in knowledgeChunks" :key="chunk.id">
                  <td><strong>{{ chunk.file_name }}</strong><small>{{ chunk.id }}</small></td>
                  <td>{{ chunk.content }}</td>
                </tr>
              </tbody>
            </table>
            <div v-else class="empty large"><strong>暂无知识切片</strong><span>请在左侧上传平台规则 PDF 文档</span></div>
          </div>
        </div>
      </section>

      <section v-else class="observability-layout">
        <div v-if="isAdmin" class="metrics"><article><span>AI Runs</span><strong>{{ metrics.ai.agent_runs || 0 }}</strong><small>平均 {{ metrics.ai.average_latency_ms || 0 }} ms</small></article><article><span>Token 用量</span><strong>{{ (metrics.ai.input_tokens || 0) + (metrics.ai.output_tokens || 0) }}</strong><small>输入 {{ metrics.ai.input_tokens || 0 }} · 输出 {{ metrics.ai.output_tokens || 0 }}</small></article><article><span>会话</span><strong>{{ metrics.service.conversations || 0 }}</strong><small>{{ metrics.service.queued || 0 }} 个排队</small></article><article><span>人工介入</span><strong>{{ metrics.service.handoffs || 0 }}</strong><small>{{ metrics.service.human || 0 }} 个进行中</small></article><article><span>售后单</span><strong>{{ metrics.commerce.after_sales || 0 }}</strong><small>{{ metrics.commerce.orders || 0 }} 笔订单</small></article></div>
        <div class="audit-grid">
          <div class="panel runs"><div class="panel-title"><h2>最近运行</h2><span>{{ runs.length }}</span></div><button v-for="run in runs" :key="run.id" class="run-row" @click="inspectRun(run)"><span><strong>{{ run.id }}</strong><small>{{ run.conversation_id }} · {{ run.model_name }}</small></span><span :class="['state', run.state.toLowerCase()]">{{ run.state }}</span><b>{{ run.latency_ms || 0 }} ms</b></button></div>
          <div class="panel inspection"><template v-if="selectedRun"><div class="panel-title"><h2>Run 详情</h2><span>{{ selectedRun.run.state }}</span></div><dl><dt>Prompt</dt><dd>{{ selectedRun.run.prompt_version }}</dd><dt>Tokens</dt><dd>输入 {{ selectedRun.run.input_tokens || 0 }} · 输出 {{ selectedRun.run.output_tokens || 0 }}</dd><dt>Outcome</dt><dd><pre>{{ JSON.stringify(selectedRun.run.outcome, null, 2) }}</pre></dd></dl><h3>工具调用</h3><article v-for="call in selectedRun.tool_calls" :key="call.tool_call_id" class="tool-call"><strong>{{ call.tool_name }}</strong><small>{{ call.latency_ms }} ms</small><pre>{{ JSON.stringify(call.arguments, null, 2) }}</pre></article></template><div v-else class="empty large"><strong>选择一次 Run</strong><span>查看最终结果与只读工具调用审计</span></div></div>
        </div>
      </section>
    </main>
  </div>
</template>
