<script setup lang="ts">
// 找圖主頁：推薦 feed + 搜尋 + 無限捲動 + 抽屜/開發者/lightbox。
import type { PostView } from '~/utils/pose'

const {
  q,
  posts,
  loading,
  capped,
  status,
  statusErr,
  feedTitle,
  feedMeta,
  statsText,
  loadPage,
  search,
  onTag,
  clearSearch,
  loadStats,
} = useFeed()

// ── UI 狀態（彈層）──────────────────────────────────────
const drawerOpen = ref(false)
const devOpen = ref(false)
const lightbox = reactive({ open: false, src: '', cap: '', id: null as number | null })

// 開彈層時鎖背景捲動；全關才解鎖。
function lockBody(lock: boolean) {
  if (import.meta.client) document.body.style.overflow = lock ? 'hidden' : ''
}
function openDrawer() {
  drawerOpen.value = true
  lockBody(true)
}
function closeDrawer() {
  drawerOpen.value = false
  if (!devOpen.value) lockBody(false)
}
function goHome() {
  closeDrawer()
  clearSearch()
}
function openDev() {
  drawerOpen.value = false
  devOpen.value = true
  lockBody(true)
}
function closeDev() {
  devOpen.value = false
  lockBody(false)
}
// FB 式：開大圖時把網址列換成這張圖的分享連結 /p/<id>（不換頁、feed 留在背後），
// 關閉或按瀏覽器上一頁就退回 feed，捲動位置不丟。
let pushedShareUrl = false
let savedScrollY = 0
function openLightbox(p: PostView) {
  if (!p.thumb) return
  if (import.meta.client) savedScrollY = window.scrollY
  lightbox.src = p.thumb
  lightbox.cap = p.desc
  lightbox.id = p.id
  lightbox.open = true
  lockBody(true)
  if (import.meta.client) {
    history.pushState({ ...history.state, lb: p.id }, '', `/p/${p.id}`)
    pushedShareUrl = true
  }
}
// 只收 UI 狀態，不碰 history（給 popstate / 直接呼叫共用）。
function clearLightboxUi() {
  lightbox.open = false
  lightbox.src = ''
  lightbox.id = null
  if (!drawerOpen.value && !devOpen.value) lockBody(false)
  // 防呆：退回 feed 時若捲動位置被路由還原弄掉了（issue #5 對跳版很敏感），補回原位。
  if (import.meta.client) {
    requestAnimationFrame(() => {
      if (Math.abs(window.scrollY - savedScrollY) > 2) window.scrollTo(0, savedScrollY)
    })
  }
}
function closeLightbox() {
  if (import.meta.client && pushedShareUrl) {
    pushedShareUrl = false
    history.back() // 退回 feed 的歷史項 → 觸發 popstate → clearLightboxUi
  } else {
    clearLightboxUi()
  }
}
// 使用者按瀏覽器「上一頁」關掉大圖時，同步收掉 UI（history 已由瀏覽器處理）。
function onPopState() {
  pushedShareUrl = false
  if (lightbox.open) clearLightboxUi()
}

// ── 無限捲動 + 鍵盤（client only）───────────────────────
const sentinel = ref<HTMLElement | null>(null)
let io: IntersectionObserver | null = null

function onKeydown(e: KeyboardEvent) {
  if (e.key !== 'Escape') return
  if (lightbox.open) closeLightbox()
  else if (devOpen.value) closeDev()
  else if (drawerOpen.value) closeDrawer()
}

onMounted(() => {
  document.addEventListener('keydown', onKeydown)
  window.addEventListener('popstate', onPopState)
  io = new IntersectionObserver(
    (entries) => {
      if (entries.some((en) => en.isIntersecting)) loadPage()
    },
    { rootMargin: '600px 0px' },
  )
  nextTick(() => {
    if (sentinel.value && io) io.observe(sentinel.value)
  })
  loadStats()
  // 支援帶 ?tag=category=name / ?q=關鍵字 進頁（單圖頁點標籤、或別人分享搜尋連結用），
  // 否則就先給推薦 feed（無關鍵字＝依收藏/評分/時間排序）。
  const route = useRoute()
  const qTag = typeof route.query.tag === 'string' ? route.query.tag : ''
  const qKw = typeof route.query.q === 'string' ? route.query.q : ''
  if (qTag) search(qTag)
  else if (qKw) q.value = qKw // watcher 會自動觸發搜尋
  else search(null)
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  window.removeEventListener('popstate', onPopState)
  io?.disconnect()
})
</script>

<template>
  <div>
    <TopBar
      v-model:q="q"
      :stats-text="statsText"
      @open-drawer="openDrawer"
      @submit="search(null)"
      @clear="clearSearch"
    />

    <main class="container">
      <div class="feed-head">
        <h2>{{ feedTitle }}</h2>
        <span class="feed-meta">{{ feedMeta }}</span>
      </div>

      <div v-if="status" class="status" :class="{ error: statusErr }">{{ status }}</div>

      <div class="feed">
        <PostCard
          v-for="p in posts"
          :key="p.id"
          :post="p"
          @open-lightbox="openLightbox"
          @tag="onTag"
        />
      </div>

      <div v-if="loading" class="more-spin">載入中…</div>
      <div v-else-if="capped" class="more-spin">
        已顯示前 {{ posts.length }} 張 — 想找更多請用上方搜尋縮小範圍
      </div>
      <div ref="sentinel" class="sentinel" />
    </main>

    <AppDrawer v-model:open="drawerOpen" @home="goHome" @dev="openDev" />
    <DevModal v-if="devOpen" :stats-text="statsText" @close="closeDev" />
    <LightBox
      v-if="lightbox.open"
      :src="lightbox.src"
      :cap="lightbox.cap"
      :id="lightbox.id"
      @close="closeLightbox"
    />
  </div>
</template>
