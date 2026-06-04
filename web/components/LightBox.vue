<script setup lang="ts">
// 點圖放大檢視。
defineProps<{ src: string; cap?: string }>()
const emit = defineEmits<{ close: [] }>()
</script>

<template>
  <div class="lightbox" @click.self="emit('close')">
    <button class="lb-close" title="關閉" @click="emit('close')">✕</button>
    <figure class="lb-figure">
      <img :src="src" alt="" />
      <figcaption v-if="cap">{{ cap }}</figcaption>
    </figure>
  </div>
</template>
