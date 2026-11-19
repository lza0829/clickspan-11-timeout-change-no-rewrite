<script setup>
import { computed, onMounted, ref } from 'vue'
import { api } from '../api.js'

const sec = ref(1800)
const minSec = ref(60)
const current = ref(null)
const audit = ref(null)
const msg = ref('')
const err = ref('')
const busy = ref(false)

const changed = computed(() => current.value && Number(sec.value) !== current.value.idle_timeout_sec)
const integrity = computed(() => audit.value?.integrity || null)

async function load() {
  err.value = ''
  try {
    const [s, a] = await Promise.all([api.settings(), api.configAudit()])
    sec.value = s.idle_timeout_sec
    minSec.value = s.min_idle_timeout_sec ?? 60
    current.value = s
    audit.value = a
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)

async function save() {
  err.value = ''
  msg.value = ''
  busy.value = true
  try {
    const res = await api.saveTimeout(sec.value)
    msg.value = res.changed
      ? `已记配置版本 v${res.config_version}：${res.previous_sec}s → ${res.idle_timeout_sec}s，仅影响之后收流`
      : `值未变化（当前 v${res.config_version}，${res.idle_timeout_sec}s）`
    await load()
  } catch (e) {
    err.value = String(e.message || e)
  } finally {
    busy.value = false
  }
}

async function tryResplit() {
  err.value = ''
  msg.value = ''
  try {
    await api.resplitHistory()
    msg.value = '重切历史已执行（不应出现）'
  } catch (e) {
    err.value = `已拒绝：${String(e.message || e)}`
  }
}
</script>

<template>
  <section class="panel">
    <h1>空闲切窗</h1>
    <p class="muted">
      修改秒数只影响之后收流的裁决；已 closed 的会话成员与既有切分边不回溯。
      <template v-if="current">
        当前 <b class="mono">v{{ current.config_version }}</b> · {{ current.idle_timeout_sec }}s。
      </template>
    </p>
    <label>
      秒数
      <input v-model.number="sec" type="number" :min="minSec" step="60" class="mono" />
    </label>
    <p>
      <button class="btn" :disabled="busy || !changed" @click="save">
        {{ busy ? '写入中…' : '保存新配置版本' }}
      </button>
      <button class="btn ghost" :disabled="busy" @click="tryResplit">重切历史（固定拒绝）</button>
    </p>
    <p v-if="msg" class="ok">{{ msg }}</p>
    <p v-if="err" class="err">{{ err }}</p>
  </section>

  <section class="panel" v-if="audit">
    <h2>配置版本</h2>
    <p class="muted">只追加、可审计；切分边引用当时的配置版本。</p>
    <table class="data">
      <thead><tr><th>版本</th><th>超时</th><th>变更前</th><th>来源</th><th>时刻</th></tr></thead>
      <tbody>
        <tr v-for="v in audit.config_versions" :key="v.version">
          <td class="mono">v{{ v.version }}</td>
          <td class="mono">{{ v.idle_timeout_sec }}s</td>
          <td class="mono">{{ v.previous_sec === null ? '—' : v.previous_sec + 's' }}</td>
          <td>{{ v.source }}</td>
          <td class="mono">{{ v.created_at }}</td>
        </tr>
      </tbody>
    </table>
  </section>

  <section class="panel" v-if="audit">
    <h2>历史只读核证</h2>
    <p v-if="integrity" :class="integrity.ok ? 'ok' : 'err'">
      {{ integrity.ok ? '核证通过' : '核证发现偏差' }} ·
      closed {{ integrity.closed_members_match_proofs ? '成员与证明一致' : '成员与证明不一致' }} ·
      切分边 {{ integrity.cut_edges_match_sessions ? '与织机一致' : '与织机不一致' }}
    </p>
    <table class="data" v-if="audit.cut_edges.length">
      <thead>
        <tr><th>#</th><th>前序</th><th>后继</th><th>间隙</th><th>当时超时</th><th>版本</th><th>来源</th></tr>
      </thead>
      <tbody>
        <tr v-for="c in audit.cut_edges" :key="c.id">
          <td class="mono">{{ c.id }}</td>
          <td class="mono">{{ c.prev_session_id }}</td>
          <td class="mono">{{ c.next_session_id }}</td>
          <td class="mono">{{ c.gap_sec }}s</td>
          <td class="mono">{{ c.timeout_sec_used }}s</td>
          <td class="mono">{{ c.config_version ?? '—' }}</td>
          <td>{{ c.reason }}</td>
        </tr>
      </tbody>
    </table>
    <p v-else class="muted">尚无切分边。</p>
    <ul v-if="integrity && integrity.violations.length" class="err mono">
      <li v-for="(v, i) in integrity.violations" :key="i">{{ v }}</li>
    </ul>
  </section>
</template>
