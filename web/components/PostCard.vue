<script setup lang="ts">
// 單張卡片：頭像/作者、縮圖、IG 風收束敘述（超過 2 行才給「更多」）、角色與標籤 chips。
import type { PostView } from '~/utils/pose'

const props = defineProps<{ post: PostView }>()
const emit = defineEmits<{
  openLightbox: [post: PostView]
  tag: [raw: string]
}>()

const expanded = ref(false)
const tagsOpen = ref(false)
const overflowing = ref(false)
const capEl = ref<HTMLElement | null>(null)

// 分享：複製/喚起系統分享面板，分享這張圖的連結 /p/<id>。
const { share, shareUrl, justCopied } = useShare()
function onShare() {
  share(shareUrl(props.post.id), `PosePlanner #${props.post.id}`)
}

// 量測敘述在收束狀態下是否溢出，決定要不要顯示「更多」。
// 量測要在收束時做（展開後 clamp 移除就量不到），故展開時跳過。
function measure() {
  const el = capEl.value
  if (!el || expanded.value) return
  requestAnimationFrame(() => {
    if (!expanded.value && capEl.value) {
      overflowing.value = capEl.value.scrollHeight - capEl.value.clientHeight > 2
    }
  })
}
onMounted(measure)
</script>

<template>
  <article class="post">
    <div class="post-head">
      <div class="avatar">{{ post.avatar }}</div>
      <div class="who">
        <div class="author">
          <span class="author-name">{{ post.author }}</span>
          <span class="pid">#{{ post.id }}</span>
        </div>
        <div class="sub">{{ post.sub }}</div>
      </div>
      <div v-if="post.badge" class="head-badge">{{ post.badge }}</div>
      <button
        type="button"
        class="share-btn"
        :class="{ 'head-badge-gap': post.badge }"
        :title="justCopied ? '已複製連結' : '分享這張圖'"
        :aria-label="'分享 pose #' + post.id"
        @click="onShare"
      >
        <span v-if="justCopied" class="share-done">已複製</span>
        <span v-else aria-hidden="true">↗</span>
      </button>
    </div>

    <div
      v-if="post.thumb"
      class="media"
      :style="post.ratio ? { aspectRatio: post.ratio } : undefined"
      @click="emit('openLightbox', post)"
    >
      <img loading="lazy" :src="post.thumb" :alt="'pose #' + post.id" />
    </div>
    <div v-else class="media no-img"><span>（無縮圖）</span></div>

    <div class="post-body">
      <div v-if="post.desc" class="caption-wrap">
        <p ref="capEl" class="caption" :class="{ clamped: !expanded }">{{ post.desc }}</p>
        <button
          v-if="overflowing"
          type="button"
          class="more-link"
          @click="expanded = !expanded"
        >
          {{ expanded ? '收起' : '… 更多' }}
        </button>
      </div>
      <div v-if="post.roleChips.length" class="chips">
        <span
          v-for="c in post.roleChips"
          :key="c.role + c.name"
          class="chip role"
          :title="c.role"
        >
          {{ c.role }}：{{ c.name }}
        </span>
      </div>

      <div v-if="post.tagChips.length" class="tags">
        <button
          type="button"
          class="tags-toggle"
          :aria-expanded="tagsOpen"
          @click="tagsOpen = !tagsOpen"
        >
          <span class="caret" :class="{ open: tagsOpen }">▸</span>
          標籤 {{ post.tagChips.length }}
        </button>
        <div v-show="tagsOpen" class="chips tags-list">
          <button
            v-for="t in post.tagChips"
            :key="t.raw"
            class="chip"
            @click="emit('tag', t.raw)"
          >
            #{{ t.val }}
          </button>
        </div>
      </div>
    </div>
  </article>
</template>
