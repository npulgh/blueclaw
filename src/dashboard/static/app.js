// Lynxclaw Dashboard — Alpine.js components

// ---------------------------------------------------------------------------
// Global store
// ---------------------------------------------------------------------------
document.addEventListener('alpine:init', () => {
  Alpine.store('app', {
    token: sessionStorage.getItem('dashboard_token') || '',
    currentPage: 'overview',
    loading: false,
    error: null,

    async api(path, params = {}) {
      const filtered = Object.fromEntries(
        Object.entries(params).filter(([, v]) => v !== null && v !== undefined)
      )
      const qs = new URLSearchParams(filtered).toString()
      const url = qs ? `/api${path}?${qs}` : `/api${path}`
      const res = await fetch(url, {
        headers: { Authorization: `Bearer ${this.token}` },
      })
      if (!res.ok) throw new Error(`HTTP ${res.status}`)
      return res.json()
    },

    setToken(t) {
      this.token = t
      sessionStorage.setItem('dashboard_token', t)
    },

    clearToken() {
      this.token = ''
      sessionStorage.removeItem('dashboard_token')
    },
  })
})

// ---------------------------------------------------------------------------
// Root component
// ---------------------------------------------------------------------------
function appRoot() {
  return {
    store: null,
    tokenInput: '',
    loginError: '',
    navItems: [
      { page: 'overview', label: '系统概览' },
      { page: 'messages', label: '消息审计' },
      { page: 'tasks',    label: '任务管理' },
      { page: 'metrics',  label: '指标图表' },
    ],

    init() { this.store = Alpine.store('app') },

    async login() {
      this.loginError = ''
      Alpine.store('app').setToken(this.tokenInput)
      try {
        await Alpine.store('app').api('/system/overview')
      } catch {
        Alpine.store('app').clearToken()
        this.loginError = '认证失败，请检查 Token'
      }
    },

    logout() {
      Alpine.store('app').clearToken()
      Alpine.store('app').error = null
    },
  }
}

// ---------------------------------------------------------------------------
// Helper: format unix timestamp
// ---------------------------------------------------------------------------
function fmtTs(ts) {
  if (!ts) return '—'
  return new Date(ts * 1000).toLocaleString('zh-CN')
}

// ---------------------------------------------------------------------------
// Overview page
// ---------------------------------------------------------------------------
function overviewPage() {
  return {
    data: null,
    groups: [],

    async init() {
      const s = Alpine.store('app')
      s.error = null
      try {
        this.data = await s.api('/system/overview')
        const g = await s.api('/groups')
        this.groups = g.items
      } catch (e) {
        s.error = e.message
      }
    },

    render() {
      if (!this.data) return '<p class="text-gray-500 text-sm">加载中...</p>'
      const d = this.data
      const cards = [
        { label: 'Groups',     value: d.groups_count },
        { label: '活跃容器',   value: d.active_containers },
        { label: '今日消息',   value: d.messages_today },
        { label: '累计 Token', value: (d.total_input_tokens + d.total_output_tokens).toLocaleString() },
      ]
      const cardHtml = cards.map(c => `
        <div class="bg-gray-900 border border-gray-800 rounded-xl p-5">
          <p class="text-xs text-gray-500 mb-1">${c.label}</p>
          <p class="text-2xl font-bold text-white">${c.value}</p>
        </div>
      `).join('')

      const rows = this.groups.map(g => `
        <tr class="border-t border-gray-800 hover:bg-gray-800/50">
          <td class="px-4 py-2 text-sm font-medium text-blue-400">${g.name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${g.channel}</td>
          <td class="px-4 py-2 text-xs font-mono text-gray-400">${g.chat_id}</td>
          <td class="px-4 py-2 text-sm text-gray-400">${g.trigger || '—'}</td>
          <td class="px-4 py-2 text-xs font-mono text-gray-500">${g.session_id ? g.session_id.slice(0, 12) + '…' : '无会话'}</td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(g.last_active)}</td>
        </tr>
      `).join('')

      return `
        <h1 class="text-xl font-bold mb-6 text-white">系统概览</h1>
        <div class="grid grid-cols-2 md:grid-cols-4 gap-4 mb-8">${cardHtml}</div>
        <h2 class="text-base font-semibold mb-3 text-gray-300">Groups 状态</h2>
        <div class="overflow-x-auto rounded-xl border border-gray-800">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">名称</th>
                <th class="px-4 py-3">渠道</th>
                <th class="px-4 py-3">Chat ID</th>
                <th class="px-4 py-3">触发词</th>
                <th class="px-4 py-3">Session</th>
                <th class="px-4 py-3">最后活跃</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="6" class="px-4 py-6 text-gray-600 text-sm text-center">暂无 Group 数据</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}

// ---------------------------------------------------------------------------
// Messages / Audit page
// ---------------------------------------------------------------------------
function messagesPage() {
  return {
    tab: 'messages',
    messages: [], msgTotal: 0, msgPage: 0,
    audit: [],    audTotal: 0, audPage: 0,

    async init() {
      Alpine.store('app').error = null
      await this.loadMessages()
      await this.loadAudit()
    },

    async loadMessages() {
      try {
        const r = await Alpine.store('app').api('/messages', {
          limit: 50, offset: this.msgPage * 50,
        })
        this.messages = r.items
        this.msgTotal = r.total
      } catch (e) { Alpine.store('app').error = e.message }
    },

    async loadAudit() {
      try {
        const r = await Alpine.store('app').api('/audit', {
          limit: 50, offset: this.audPage * 50,
        })
        this.audit = r.items
        this.audTotal = r.total
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const tabs = `
        <div class="flex gap-2 mb-6">
          <button
            onclick="Alpine.store('app') && (document.querySelector('[x-data]').__x)"
            class="${this.tab === 'messages' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'} px-4 py-1.5 rounded-lg text-sm transition"
            @click.stop="tab='messages'; loadMessages()">消息历史</button>
          <button
            class="${this.tab === 'audit' ? 'bg-blue-600 text-white' : 'bg-gray-800 text-gray-400 hover:text-white'} px-4 py-1.5 rounded-lg text-sm transition"
            @click.stop="tab='audit'; loadAudit()">审计日志</button>
        </div>
      `

      if (this.tab === 'messages') {
        const rows = this.messages.map(m => `
          <tr class="border-t border-gray-800 hover:bg-gray-800/50">
            <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(m.created_at)}</td>
            <td class="px-4 py-2 text-sm text-blue-400">${m.group_name || '—'}</td>
            <td class="px-4 py-2 text-xs">
              <span class="px-2 py-0.5 rounded ${m.direction === 'inbound' ? 'bg-green-900/50 text-green-400' : 'bg-purple-900/50 text-purple-400'}">${m.direction}</span>
            </td>
            <td class="px-4 py-2 text-xs">
              <span class="px-2 py-0.5 rounded ${m.status === 'completed' ? 'bg-blue-900/50 text-blue-400' : m.status === 'failed' ? 'bg-red-900/50 text-red-400' : 'bg-yellow-900/50 text-yellow-400'}">${m.status}</span>
            </td>
            <td class="px-4 py-2 text-xs text-gray-400 content-cell">${(m.content || '').slice(0, 80)}</td>
          </tr>
        `).join('')
        return `
          <h1 class="text-xl font-bold mb-6 text-white">消息与审计</h1>
          <div x-html="tabs"></div>
          <p class="text-xs text-gray-500 mb-3">共 ${this.msgTotal} 条，第 ${this.msgPage + 1} 页</p>
          <div class="overflow-x-auto rounded-xl border border-gray-800">
            <table class="w-full text-left">
              <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
                <tr>
                  <th class="px-4 py-3">时间</th>
                  <th class="px-4 py-3">Group</th>
                  <th class="px-4 py-3">方向</th>
                  <th class="px-4 py-3">状态</th>
                  <th class="px-4 py-3">内容</th>
                </tr>
              </thead>
              <tbody>${rows || '<tr><td colspan="5" class="px-4 py-6 text-gray-600 text-sm text-center">暂无消息</td></tr>'}</tbody>
            </table>
          </div>
        `
      } else {
        const rows = this.audit.map(a => `
          <tr class="border-t border-gray-800 hover:bg-gray-800/50">
            <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(a.created_at)}</td>
            <td class="px-4 py-2 text-sm text-blue-400">${a.group_name}</td>
            <td class="px-4 py-2 text-sm font-mono text-gray-300">${a.tool_name}</td>
            <td class="px-4 py-2">
              <span class="px-2 py-0.5 rounded text-xs ${a.blocked ? 'bg-red-900/50 text-red-400' : 'bg-gray-800 text-gray-500'}">${a.blocked ? '已拦截' : '通过'}</span>
            </td>
            <td class="px-4 py-2 text-xs text-gray-400 content-cell">${a.input_summary || '—'}</td>
          </tr>
        `).join('')
        return `
          <h1 class="text-xl font-bold mb-6 text-white">消息与审计</h1>
          <div x-html="tabs"></div>
          <p class="text-xs text-gray-500 mb-3">共 ${this.audTotal} 条</p>
          <div class="overflow-x-auto rounded-xl border border-gray-800">
            <table class="w-full text-left">
              <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
                <tr>
                  <th class="px-4 py-3">时间</th>
                  <th class="px-4 py-3">Group</th>
                  <th class="px-4 py-3">工具</th>
                  <th class="px-4 py-3">状态</th>
                  <th class="px-4 py-3">摘要</th>
                </tr>
              </thead>
              <tbody>${rows || '<tr><td colspan="5" class="px-4 py-6 text-gray-600 text-sm text-center">暂无审计记录</td></tr>'}</tbody>
            </table>
          </div>
        `
      }
    },
  }
}

// ---------------------------------------------------------------------------
// Tasks page
// ---------------------------------------------------------------------------
function tasksPage() {
  return {
    tasks: [],

    async init() {
      Alpine.store('app').error = null
      try {
        const r = await Alpine.store('app').api('/tasks')
        this.tasks = r.items
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const rows = this.tasks.map(t => `
        <tr class="border-t border-gray-800 hover:bg-gray-800/50">
          <td class="px-4 py-2 text-xs font-mono text-gray-500">${t.id.slice(0, 8)}…</td>
          <td class="px-4 py-2 text-sm text-blue-400">${t.group_name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${t.type}</td>
          <td class="px-4 py-2 text-xs font-mono text-gray-400">${t.schedule}</td>
          <td class="px-4 py-2">
            <span class="px-2 py-0.5 rounded text-xs ${t.status === 'active' ? 'bg-green-900/50 text-green-400' : 'bg-gray-700 text-gray-500'}">${t.status}</span>
          </td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(t.last_run)}</td>
          <td class="px-4 py-2 text-xs text-gray-500">${fmtTs(t.next_run)}</td>
        </tr>
      `).join('')

      return `
        <h1 class="text-xl font-bold mb-6 text-white">任务管理</h1>
        <div class="overflow-x-auto rounded-xl border border-gray-800">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">ID</th>
                <th class="px-4 py-3">Group</th>
                <th class="px-4 py-3">类型</th>
                <th class="px-4 py-3">Cron</th>
                <th class="px-4 py-3">状态</th>
                <th class="px-4 py-3">上次运行</th>
                <th class="px-4 py-3">下次运行</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="7" class="px-4 py-6 text-gray-600 text-sm text-center">暂无定时任务</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}

// ---------------------------------------------------------------------------
// Metrics page
// ---------------------------------------------------------------------------
function metricsPage() {
  return {
    usage: [],

    async init() {
      Alpine.store('app').error = null
      try {
        const since7d = Math.floor(Date.now() / 1000) - 7 * 86400
        const r = await Alpine.store('app').api('/usage', { since: since7d })
        this.usage = r.items
      } catch (e) { Alpine.store('app').error = e.message }
    },

    render() {
      const rows = this.usage.map(u => `
        <tr class="border-t border-gray-800 hover:bg-gray-800/50">
          <td class="px-4 py-2 text-sm text-blue-400">${u.group_name}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${(u.input_tokens || 0).toLocaleString()}</td>
          <td class="px-4 py-2 text-sm text-gray-300">${(u.output_tokens || 0).toLocaleString()}</td>
          <td class="px-4 py-2 text-sm font-medium text-white">${((u.input_tokens || 0) + (u.output_tokens || 0)).toLocaleString()}</td>
        </tr>
      `).join('')

      return `
        <h1 class="text-xl font-bold mb-6 text-white">指标图表</h1>
        <h2 class="text-base font-semibold mb-3 text-gray-300">Token 用量（最近 7 天，按 Group 汇总）</h2>
        <div class="overflow-x-auto rounded-xl border border-gray-800 mb-8">
          <table class="w-full text-left">
            <thead class="bg-gray-900 text-xs text-gray-500 uppercase">
              <tr>
                <th class="px-4 py-3">Group</th>
                <th class="px-4 py-3">Input Tokens</th>
                <th class="px-4 py-3">Output Tokens</th>
                <th class="px-4 py-3">合计</th>
              </tr>
            </thead>
            <tbody>${rows || '<tr><td colspan="4" class="px-4 py-6 text-gray-600 text-sm text-center">暂无 Token 用量数据</td></tr>'}</tbody>
          </table>
        </div>
      `
    },
  }
}
