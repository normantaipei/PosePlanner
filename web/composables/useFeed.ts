// 找圖 feed 的狀態與邏輯：搜尋、標籤篩選、無限捲動分頁、庫狀態。
// 資料一律走 Nuxt 代理（/api/search、/api/stats），token 不出現在前端。
import { normalize, type PostView, type RawPose } from '~/utils/pose'

const PAGE = 24
const MAX_POSTS = 300 // feed 累積上限：避免圖庫大時 DOM 無限長把手機拖卡

export interface Stats {
  poses?: number
  creators?: number
}

export function useFeed() {
  const q = ref('')
  const posts = ref<PostView[]>([])
  const loading = ref(false)
  const done = ref(false)
  const capped = ref(false) // 已達 MAX_POSTS 累積上限（非真的沒資料了）
  const offset = ref(0)
  const activeTag = ref<string | null>(null)
  const status = ref<string | null>(null)
  const statusErr = ref(false)
  const stats = ref<Stats | null>(null)

  let debounceTimer: ReturnType<typeof setTimeout> | null = null

  const feedTitle = computed(() => {
    if (activeTag.value) return '標籤：' + activeTag.value.split('=').slice(1).join('=')
    if (q.value.trim()) return '搜尋：' + q.value.trim()
    return '為你推薦'
  })
  const feedMeta = computed(() => (offset.value ? `已載入 ${offset.value} 張` : ''))
  const statsText = computed(() =>
    stats.value ? `${stats.value.poses ?? '?'} 張 · ${stats.value.creators ?? 0} 位創作者` : '',
  )

  function setStatus(msg: string | null, isErr = false) {
    status.value = msg || null
    statusErr.value = !!isErr
  }

  async function loadPage() {
    if (loading.value || done.value) return
    loading.value = true
    try {
      const params: Record<string, string | number> = { limit: PAGE, offset: offset.value }
      if (q.value.trim()) params.q = q.value.trim()
      if (activeTag.value) params.tag = activeTag.value
      const items = await $fetch<RawPose[]>('/api/search', { params })
      if (!Array.isArray(items)) throw new Error('server 回傳非預期格式')

      if (offset.value === 0 && items.length === 0) {
        setStatus(
          q.value.trim() || activeTag.value
            ? '找不到符合的圖片，換個關鍵字試試。'
            : '庫裡還沒有圖片。',
        )
      } else {
        setStatus(null)
      }
      posts.value.push(...items.map(normalize))
      offset.value += items.length
      if (items.length < PAGE) done.value = true
      if (posts.value.length >= MAX_POSTS) {
        done.value = true
        capped.value = true
      }
    } catch (err: any) {
      setStatus('連線失敗：' + (err?.message || err) + '　（請確認後端 DB server 是否開著）', true)
      done.value = true
    } finally {
      loading.value = false
    }
  }

  function resetAndLoad() {
    posts.value = []
    offset.value = 0
    done.value = false
    capped.value = false
    setStatus(null)
    loadPage()
  }

  function search(tag?: string | null) {
    activeTag.value = tag || null
    if (tag) q.value = ''
    resetAndLoad()
  }
  // 搜尋結果的 tag 是 "category:name"，但 /search 的 tag 參數要 "category=name"
  function onTag(raw: string) {
    search(raw.replace(':', '='))
  }
  function clearSearch() {
    q.value = ''
    activeTag.value = null
    resetAndLoad()
  }

  // 打字 350ms 後自動查（清掉 tag 篩選）
  watch(q, () => {
    if (debounceTimer) clearTimeout(debounceTimer)
    debounceTimer = setTimeout(() => {
      activeTag.value = null
      resetAndLoad()
    }, 350)
  })

  async function loadStats() {
    try {
      stats.value = await $fetch<Stats>('/api/stats')
    } catch {
      /* 靜默 */
    }
  }

  return {
    q,
    posts,
    loading,
    done,
    capped,
    status,
    statusErr,
    stats,
    feedTitle,
    feedMeta,
    statsText,
    loadPage,
    resetAndLoad,
    search,
    onTag,
    clearSearch,
    loadStats,
  }
}
