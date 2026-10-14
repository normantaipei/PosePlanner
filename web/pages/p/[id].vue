<script setup lang="ts">
// 單圖分享頁 /p/<id>：別人開分享連結時直接 SSR 出這一張圖（縮圖 + 敘述/標籤）。
// 與 feed 共用 PostCard / LightBox；找不到就給友善訊息 + 回首頁。
// SSR 同時輸出 Open Graph 標籤，聊天軟體貼上連結就會顯示這張圖的縮圖預覽。
import { normalize, shareMeta, type PostView, type RawPose } from '~/utils/pose'

const route = useRoute()
const id = computed(() => String(route.params.id || ''))

// 私有頁，不要被搜尋引擎收錄（與首頁一致）。聊天軟體的預覽爬蟲不受影響。
useHead({
  meta: [{ name: 'robots', content: 'noindex, nofollow' }],
})

const { data, error } = await useFetch<RawPose>(() => `/api/pose/${id.value}`)
const post = computed<PostView | null>(() => (data.value ? normalize(data.value) : null))

// og:image / og:url 必須是絕對網址。有設 NUXT_PUBLIC_SITE_URL 就用它（正式對外網域），
// 否則用這次請求的網址（反向代理後面則讀 X-Forwarded-Host / -Proto）。
const config = useRuntimeConfig()
const requestUrl = useRequestURL({ xForwardedHost: true, xForwardedProto: true })
const origin = String(config.public.siteUrl || '').replace(/\/+$/, '') || requestUrl.origin
const meta = computed(() => (data.value ? shareMeta(data.value) : null))
const ogImage = computed(() => (post.value?.thumb ? origin + post.value.thumb : undefined))

useSeoMeta({
  title: () => meta.value?.title || `PosePlanner — 圖片 #${id.value}`,
  description: () => meta.value?.description,
  ogType: 'website',
  ogSiteName: 'PosePlanner',
  ogUrl: () => `${origin}/p/${id.value}`,
  ogTitle: () => meta.value?.title || 'PosePlanner',
  ogDescription: () => meta.value?.description || '找不到這張圖片，可能已被移除。',
  ogImage: () => ogImage.value,
  ogImageWidth: () => (ogImage.value && data.value?.thumb_w) || undefined,
  ogImageHeight: () => (ogImage.value && data.value?.thumb_h) || undefined,
  ogImageAlt: () => meta.value?.description,
  // 有圖就用大圖卡（Discord / Slack 會顯示大縮圖，而不是右側小方塊）。
  twitterCard: () => (ogImage.value ? 'summary_large_image' : 'summary'),
})

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
