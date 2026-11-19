async function req(path, opts = {}) {
  const res = await fetch(path, {
    headers: { 'Content-Type': 'application/json', ...(opts.headers || {}) },
    ...opts,
  })
  if (!res.ok) {
    const text = await res.text()
    throw new Error(text || res.statusText)
  }
  return res.json()
}

export const api = {
  health: () => req('/api/health'),
  overview: () => req('/api/overview'),
  visitors: () => req('/api/visitors'),
  sessions: (visitorId) =>
    req(visitorId ? `/api/sessions?visitor_id=${encodeURIComponent(visitorId)}` : '/api/sessions'),
  session: (id) => req(`/api/sessions/${encodeURIComponent(id)}`),
  quarantine: () => req('/api/quarantine'),
  ingest: (fixture) =>
    req('/api/ingest/run', {
      method: 'POST',
      body: JSON.stringify(fixture ? { fixture } : {}),
    }),
  proofs: (sessionId) =>
    req(sessionId ? `/api/session-proofs?session_id=${encodeURIComponent(sessionId)}` : '/api/session-proofs'),
  settings: () => req('/api/settings'),
  saveTimeout: (sec) =>
    req('/api/settings/idle-timeout', {
      method: 'PUT',
      body: JSON.stringify({ idle_timeout_sec: Number(sec) }),
    }),
  configAudit: () => req('/api/config-audit'),
  resplitHistory: () => req('/api/resplit-history', { method: 'POST' }),
}
