<script setup lang="ts">
// 頂部列：漢堡選單、品牌、搜尋框、庫狀態。搜尋字串用 v-model 雙向綁回 page。
defineProps<{ statsText: string }>()
const q = defineModel<string>('q', { required: true })
const emit = defineEmits<{
  openDrawer: []
  submit: []
  clear: []
}>()
</script>

<template>
  <header class="topbar">
    <div class="bar-inner">
      <button
        class="hamburger"
        type="button"
        title="選單"
        aria-label="開啟選單"
        @click="emit('openDrawer')"
      >
        <span /><span /><span />
      </button>
      <a class="brand" href="#" @click.prevent="emit('clear')">
        <span class="brand-name">PosePlanner</span>
      </a>
      <form class="searchbar" autocomplete="off" @submit.prevent="emit('submit')">
        <input
          v-model="q"
          type="search"
          placeholder="搜尋姿勢、構圖、作品、創作者…例如「回眸 站姿」"
        />
        <button v-if="q" type="button" class="clear-btn" title="清除" @click="emit('clear')">
          ✕
        </button>
      </form>
      <div class="stats-pill" title="庫狀態">{{ statsText }}</div>
    </div>
  </header>
</template>
