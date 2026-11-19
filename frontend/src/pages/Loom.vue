<script setup>
import { onMounted, ref } from 'vue'
import { api } from '../api.js'

const overview = ref(null)
const sessions = ref([])
const details = ref({})
const err = ref('')

async function load() {
  err.value = ''
  try {
    overview.value = await api.overview()
    sessions.value = await api.sessions()
    const map = {}
    for (const s of sessions.value) {
      map[s.id] = await api.session(s.id)
    }
    details.value = map
  } catch (e) {
    err.value = String(e.message || e)
  }
}

onMounted(load)
</script>

<template>
  <div>
    <div class="stats" v-if="overview">
      <div class="stat"><b>{{ overview.visitors }}</b><span>访客</span></div>
      <div class="stat"><b>{{ overview.sessions }}</b><span>会话</span></div>
      <div class="stat"><b>{{ overview.hits_primary }}</b><span>正式击点</span></div>
      <div class="stat"><b>{{ overview.quarantine }}</b><span>隔离</span></div>
      <div class="stat"><b>{{ overview.idle_timeout_sec }}s</b><span>空闲超时</span></div>
      <div class="stat"><b>v{{ overview.config_version }}</b><span>配置版本</span></div>
    </div>
    <p v-if="err" class="err">{{ err }}</p>

    <section class="panel">
      <h1>会话织机</h1>
      <p class="muted">按空闲间隙切开；closed 成员与切分边一旦定下不再回溯。</p>
      <div class="lane-list">
        <article v-for="s in sessions" :key="s.id" class="lane" :class="s.status">
          <div class="lane-head">
            <div>
              <strong>{{ s.id }}</strong>
              <span class="muted"> · {{ s.visitor_id }} · {{ s.status }}</span>
            </div>
            <span class="mono">{{ s.hit_count }} 点 · {{ s.started_at }} → {{ s.ended_at || '…' }}</span>
          </div>
          <div class="beads" v-if="details[s.id]">
            <template v-for="(h, i) in details[s.id].hits" :key="h.id">
              <span v-if="i" class="connector" aria-hidden="true"></span>
              <span class="hit-bead">
                <strong>{{ h.event_name }}</strong>
                <span>{{ h.page_path }}</span>
              </span>
            </template>
          </div>
          <div class="cuts" v-if="details[s.id] && details[s.id].cuts.length">
            <span
              v-for="c in details[s.id].cuts"
              :key="c.id"
              class="cut-tag mono"
              :title="`间隙 ${c.gap_sec}s · 当时超时 ${c.timeout_sec_used}s`"
            >
              切 #{{ c.id }} {{ c.gap_sec }}s / {{ c.timeout_sec_used }}s · v{{ c.config_version ?? '—' }}
            </span>
          </div>
        </article>
      </div>
    </section>
  </div>
</template>
