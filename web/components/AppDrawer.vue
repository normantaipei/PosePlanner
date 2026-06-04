<script setup lang="ts">
// 左側漢堡抽屜：搜尋主頁、開發者資訊。open 用 v-model 綁回 page。
const open = defineModel<boolean>('open', { required: true })
const emit = defineEmits<{
  home: []
  dev: []
}>()
</script>

<template>
  <div class="drawer-root" :class="{ open }">
    <div class="drawer-scrim" @click="open = false" />
    <nav class="drawer" aria-label="主選單">
      <div class="drawer-head">
        <span class="drawer-title">PosePlanner</span>
        <button class="drawer-x" type="button" title="關閉" @click="open = false">✕</button>
      </div>
      <ul class="drawer-menu">
        <li>
          <button type="button" @click="emit('home')">搜尋主頁</button>
        </li>
        <li>
          <button type="button" @click="emit('dev')">開發者資訊</button>
        </li>
      </ul>
    </nav>
  </div>
</template>
