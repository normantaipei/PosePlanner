<script setup lang="ts">
// 點圖放大檢視。開著時網址列即為這張圖的分享連結 /p/<id>（FB 式）。
const props = defineProps<{ src: string; cap?: string; id?: number | null }>()
const emit = defineEmits<{ close: [] }>()

const { share, shareUrl, justCopied } = useShare()
function onShare() {
  // 有 id 就用 /p/<id>，否則退回目前網址列（lightbox 開著時本就是分享連結）。
  const url = props.id != null ? shareUrl(props.id) : import.meta.client ? location.href : ''
  share(url, props.id != null ? `PosePlanner #${props.id}` : 'PosePlanner')
}
</script>

<template>
  <div class="lightbox" @click.self="emit('close')">
    <button class="lb-close" title="關閉" @click="emit('close')">✕</button>
    <button
      class="lb-share"
      :title="justCopied ? '已複製連結' : '分享這張圖'"
      :aria-label="'分享' + (id != null ? ' pose #' + id : '')"
      @click="onShare"
    >
      <span v-if="justCopied" class="lb-share-done">已複製</span>
      <span v-else aria-hidden="true">↗</span>
    </button>
    <figure class="lb-figure">
      <img :src="src" alt="" />
      <figcaption v-if="cap">{{ cap }}</figcaption>
    </figure>
  </div>
</template>
