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
const lightbox = reactive({ open: false, src: '', cap: '' })

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
function openLightbox(p: PostView) {
  if (!p.thumb) return
  lightbox.src = p.thumb
  lightbox.cap = p.desc
  lightbox.open = true
  lockBody(true)
}
function closeLightbox() {
  lightbox.open = false
  lightbox.src = ''
  lockBody(false)
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
  search(null) // 進頁就先給推薦 feed（無關鍵字＝依收藏/評分/時間排序）
})

onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
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
        已顯示前 {{ posts.length }} 張 — 想找更多請用上方搜尋縮小範圍 🔍
      </div>
      <div ref="sentinel" class="sentinel" />
    </main>

    <AppDrawer v-model:open="drawerOpen" @home="goHome" @dev="openDev" />
    <DevModal v-if="devOpen" :stats-text="statsText" @close="closeDev" />
    <LightBox v-if="lightbox.open" :src="lightbox.src" :cap="lightbox.cap" @close="closeLightbox" />
  </div>
</template>
