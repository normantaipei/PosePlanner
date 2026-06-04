<script setup lang="ts">
// 開發者資訊彈窗。
defineProps<{ statsText: string }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <div class="modal" @click.self="emit('close')">
    <div class="modal-card">
      <button class="modal-x" type="button" title="關閉" @click="emit('close')">✕</button>
      <h3>開發者資訊</h3>
      <p class="dev-line"><b>專案</b>　PosePlanner — Cosplay 拍攝參考庫</p>
      <p class="dev-line"><b>前端</b>　Nuxt 3（SSR，token 走 server proxy）</p>
      <p class="dev-line"><b>後端</b>　Python + sqlite-vec 語意搜尋</p>
      <p class="dev-line"><b>庫狀態</b>　{{ statsText || '—' }}</p>
      <p class="dev-foot">純雲端設計，圖片儲存於使用者的 Google Drive。</p>
    </div>
  </div>
</template>
