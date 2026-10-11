<script setup>
import { computed, onMounted, onUnmounted, ref } from 'vue'

const links = ref('')
const jobs = ref([])
const submitting = ref(false)
const error = ref('')
const connectionError = ref(false)
const loaded = ref(false)
const announcement = ref('')
const needsSignIn = ref(false)
const password = ref('')
const signInError = ref('')
const signingIn = ref(false)
const pendingTransfers = new Set()
const labels = { queued: 'Queued', downloading: 'Downloading', ready: 'Ready', sending: 'Sending file', error: 'Failed' }
const active = computed(() => jobs.value.filter(job => ['queued', 'downloading', 'sending'].includes(job.status)).length)
let timer
let stopped = false

async function api(path, options) {
  const response = await fetch(`/api${path}`, options)
  if (!response.ok) {
    const body = await response.json().catch(() => ({}))
    const failure = new Error(typeof body.detail === 'string' ? body.detail : 'Use HTTPS links to TikTok videos or Twitter posts. Add up to 10 links at a time.')
    failure.status = response.status
    throw failure
  }
  return response.status === 204 ? null : response.json()
}

async function refresh() {
  try {
    const next = await api('/jobs')
    for (const id of pendingTransfers) {
      const job = next.find(job => job.id === id)
      if (!job) pendingTransfers.delete(id)
      else if (job.status === 'ready') job.status = 'sending'
    }
    const changed = next.filter(job => jobs.value.some(old => old.id === job.id && old.status !== job.status))
    if (changed.length) announcement.value = changed.map(job => `${job.platform === 'X' ? 'Twitter' : job.platform}: ${labels[job.status]}`).join('. ')
    jobs.value = next
    connectionError.value = false
  } catch (cause) {
    if (cause.status === 401) {
      needsSignIn.value = true
      jobs.value = []
      pendingTransfers.clear()
      connectionError.value = false
    } else {
      connectionError.value = true
    }
  } finally {
    loaded.value = true
  }
}

async function poll() {
  await refresh()
  if (!stopped && !needsSignIn.value) timer = setTimeout(poll, 2000)
}

async function signIn() {
  signInError.value = ''
  signingIn.value = true
  try {
    await api('/login', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ password: password.value }) })
    password.value = ''
    needsSignIn.value = false
    error.value = ''
    clearTimeout(timer)
    await poll()
  } catch (cause) {
    signInError.value = cause.status ? cause.message : 'Could not connect. Try again.'
  } finally {
    signingIn.value = false
  }
}

async function signOut() {
  try {
    await api('/logout', { method: 'POST' })
    clearTimeout(timer)
    needsSignIn.value = true
    jobs.value = []
    pendingTransfers.clear()
    links.value = ''
    error.value = ''
    signInError.value = ''
  } catch (cause) {
    error.value = cause.message
  }
}

async function submit() {
  const urls = links.value.trim().split(/\s+/).filter(Boolean)
  error.value = ''
  if (!urls.length || urls.length > 10) {
    error.value = 'Add 1 to 10 links, one per line.'
    return
  }
  submitting.value = true
  try {
    await api('/jobs', { method: 'POST', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify({ urls }) })
    links.value = ''
    announcement.value = 'Downloads added to the queue.'
    await refresh()
  } catch (cause) {
    error.value = cause.message === 'Failed to fetch' ? 'Could not connect. Try again.' : cause.message
  } finally {
    submitting.value = false
  }
}

async function remove(job) {
  try {
    await api(`/jobs/${job.id}`, { method: 'DELETE' })
    await refresh()
    announcement.value = 'Download removed.'
  } catch (cause) {
    error.value = cause.message
  }
}

function startTransfer(job, event) {
  if (pendingTransfers.has(job.id)) {
    event.preventDefault()
    return
  }
  pendingTransfers.add(job.id)
  job.status = 'sending'
  announcement.value = 'Sending file to your browser.'
}

onMounted(poll)
onUnmounted(() => { stopped = true; clearTimeout(timer) })
</script>

<template>
  <header class="masthead">
    <span class="wordmark">TT<span>downloader</span></span>
    <div class="header-actions"><span class="platforms">TikTok & Twitter <span class="alias">(X)</span></span><button v-if="loaded && !needsSignIn" class="secondary" @click="signOut">Sign out</button></div>
  </header>
  <main v-if="needsSignIn" class="sign-in">
    <form @submit.prevent="signIn" aria-label="Sign in">
      <h1>Sign in</h1>
      <p class="sign-in-help">Enter the shared password to use TTdownloader.</p>
      <label for="password">Password</label>
      <input id="password" v-model="password" type="password" autocomplete="current-password" required maxlength="256" :aria-invalid="Boolean(signInError)" :aria-describedby="signInError ? 'sign-in-error' : undefined">
      <button class="primary" :disabled="signingIn">{{ signingIn ? 'Signing in…' : 'Sign in' }}</button>
      <p v-if="signInError" id="sign-in-error" class="error" role="alert">{{ signInError }}</p>
    </form>
  </main>
  <main v-else>
    <div class="page-heading">
      <div><h1>Downloads</h1><p>Save videos from TikTok and Twitter.</p></div>
      <span v-if="active" class="activity" role="status">{{ active }} in progress</span>
    </div>
    <div class="workspace">
      <form @submit.prevent="submit" aria-label="New download">
        <h2>New download</h2>
        <label for="links">Video links</label>
        <textarea id="links" v-model="links" rows="6" required maxlength="20480" spellcheck="false"
          autocapitalize="off" autocomplete="off" :aria-describedby="error ? 'link-help form-error' : 'link-help'"
          :aria-invalid="Boolean(error)" placeholder="Paste links here"></textarea>
        <p id="link-help">One link per line. Up to 10 at a time.</p>
        <button class="primary" :disabled="submitting || !loaded || connectionError">{{ submitting ? 'Adding to queue…' : 'Download videos' }}</button>
        <p v-if="error" id="form-error" class="error" role="alert">{{ error }}</p>
        <dl class="limits"><div><dt>At once</dt><dd>2 downloads</dd></div><div><dt>File limit</dt><dd>250 MB</dd></div></dl>
      </form>
      <section class="queue" aria-labelledby="queue-title">
        <div class="queue-heading"><h2 id="queue-title">Queue <span class="count">{{ jobs.length }}</span></h2><span>Current session</span></div>
        <p v-if="connectionError" class="error connection-error" role="alert">Connection lost. Reconnecting…</p>
        <p v-else-if="!loaded" class="empty">Loading downloads…</p>
        <div v-else-if="!jobs.length" class="empty"><p>No downloads yet</p><p>Add a video link to start.</p></div>
        <ul v-if="jobs.length" class="jobs">
          <li v-for="job in jobs" :key="job.id">
            <div class="job-heading"><strong>{{ job.platform === 'X' ? 'Twitter' : job.platform }}</strong><span :class="['status', job.status]">{{ labels[job.status] }}</span></div>
            <a class="source" :href="job.url" target="_blank" rel="noreferrer">{{ job.filename ? job.filename.replaceAll('_', ' ') : job.url }}</a>
            <p v-if="job.error" class="error">{{ job.error }}</p>
            <div class="job-actions">
              <a v-if="job.status === 'ready'" class="download" :href="`/api/jobs/${job.id}/file`" @click="startTransfer(job, $event)">Save file<span class="sr-only">: {{ job.filename }}</span></a>
              <button v-if="['ready', 'error'].includes(job.status)" class="secondary" @click="remove(job)" :aria-label="`Remove download: ${job.url}`">Remove</button>
            </div>
          </li>
        </ul>
      </section>
    </div>
    <footer>Files are deleted after transfer or after 10 minutes. Interrupted downloads need to be started again.</footer>
    <p class="sr-only" role="status">{{ announcement }}</p>
  </main>
</template>
