<script setup lang="ts">
// 單圖分享頁 /p/<id>：別人開分享連結時直接 SSR 出這一張圖（縮圖 + 敘述/標籤）。
// 與 feed 共用 PostCard / LightBox；找不到就給友善訊息 + 回首頁。
import { normalize, type PostView, type RawPose } from '~/utils/pose'

const route = useRoute()
const id = computed(() => String(route.params.id || ''))

// 私有頁，不要被搜尋引擎收錄（與首頁一致）。
useHead({
  title: () => `PosePlanner — 圖片 #${id.value}`,
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const { data, error } = await useFetch<RawPose>(() => `/api/pose/${id.value}`)
const post = computed<PostView | null>(() => (data.value ? normalize(data.value) : null))

const lightbox = reactive({ open: false, src: '', cap: '', id: null as number | null })
function lockBody(lock: boolean) {
  if (import.meta.client) document.body.style.overflow = lock ? 'hidden' : ''
}
function openLightbox(p: PostView) {
  if (!p.thumb) return
  lightbox.src = p.thumb
  lightbox.cap = p.desc
  lightbox.id = p.id
  lightbox.open = true
  lockBody(true)
}
function closeLightbox() {
  lightbox.open = false
  lightbox.src = ''
  lightbox.id = null
  lockBody(false)
}
function onKeydown(e: KeyboardEvent) {
  if (e.key === 'Escape' && lightbox.open) closeLightbox()
}
onMounted(() => document.addEventListener('keydown', onKeydown))
onBeforeUnmount(() => {
  document.removeEventListener('keydown', onKeydown)
  lockBody(false)
})
</script>

<template>
  <div>
    <header class="single-bar">
      <NuxtLink to="/" class="single-home">← PosePlanner</NuxtLink>
    </header>

    <main class="container single">
      <div v-if="error || !post" class="status error">
        {{ error?.statusCode === 404 ? '找不到這張圖片，可能已被移除。' : '讀取失敗，請稍後再試。' }}
        <div class="single-back"><NuxtLink to="/">回首頁逛逛 →</NuxtLink></div>
      </div>

      <div v-else class="feed feed-single">
        <PostCard
          :post="post"
          @open-lightbox="openLightbox"
          @tag="(t) => navigateTo('/?tag=' + encodeURIComponent(t.replace(':', '=')))"
        />
      </div>
    </main>

    <LightBox
      v-if="lightbox.open"
      :src="lightbox.src"
      :cap="lightbox.cap"
      :id="lightbox.id"
      @close="closeLightbox"
    />
  </div>
</template>
