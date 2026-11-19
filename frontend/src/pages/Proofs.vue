<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const rows = ref([])
const audit = ref(null)
const err = ref('')

onMounted(async () => {
  try {
    const [p, a] = await Promise.all([api.proofs(), api.configAudit()])
    rows.value = p
    audit.value = a
  } catch (e) {
    err.value = String(e.message || e)
  }
})
</script>

<template>
  <section class="panel">
    <h1>会话核证</h1>
    <p class="muted">会话击点集合与起止须与织机同源可核；closed 成员不因改切窗而变。</p>
    <p v-if="err" class="err">{{ err }}</p>
    <p v-if="audit && audit.integrity" :class="audit.integrity.ok ? 'ok' : 'err'">
      核证 {{ audit.integrity.ok ? '通过' : '发现偏差' }} ·
      {{ audit.closed_sessions }} 个 closed 会话成员与证明
      {{ audit.integrity.closed_members_match_proofs ? '一致' : '不一致' }} ·
      {{ audit.cut_edges.length }} 条切分边
      {{ audit.integrity.cut_edges_match_sessions ? '与织机一致' : '与织机不一致' }}
    </p>
    <table class="data">
      <thead>
        <tr><th>会话</th><th>访客</th><th>点数</th><th>起止</th><th>击点集</th></tr>
      </thead>
      <tbody>
        <tr v-for="r in rows" :key="r.id">
          <td class="mono">{{ r.session_id }}</td>
          <td>{{ r.visitor_id }}</td>
          <td>{{ r.hit_count }}</td>
          <td class="mono">{{ r.started_at }} → {{ r.ended_at }}</td>
          <td class="mono">{{ r.hit_ids_json }}</td>
        </tr>
      </tbody>
    </table>
  </section>
</template>
