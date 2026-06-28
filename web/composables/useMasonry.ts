// 把 feed 拆成「固定欄」瀑布流：每張卡片永久屬於某一欄，位置只受同欄前面的卡片影響。
// 相對於 CSS multicol（會依高度自動平衡、捲動量到真高就全欄重排 → issue #8），
// 這裡的指派只看資料（ratio 估高貪婪塞最矮欄），與視窗/捲動無關 → 不會跳版。
// 又因為是依序處理、post[i] 的歸欄只取決於 posts[0..i]，翻頁 append 不會動到既有卡片。
import type { Ref } from 'vue'
import type { PostView } from '~/utils/pose'

// 相對高度（欄寬視為 1）：圖片高 ≈ h/w，再加卡頭/敘述等固定 chrome 的估值。
function estHeight(p: PostView): number {
  let imgH = 1.2 // 缺尺寸舊資料的保守估值
  if (p.ratio) {
    const [w, h] = p.ratio.split('/').map(Number)
    if (w > 0 && h > 0) imgH = h / w
  }
  return imgH + 0.5
}

export function useMasonry(posts: Ref<PostView[]>) {
  const cols = ref(1)

  function calcCols(): number {
    if (!import.meta.client) return 1
    const w = window.innerWidth
    if (w >= 1000) return 3
    if (w >= 640) return 2
    return 1
  }
  function onResize() {
    cols.value = calcCols()
  }

  onMounted(() => {
    cols.value = calcCols()
    window.addEventListener('resize', onResize, { passive: true })
  })
  onBeforeUnmount(() => window.removeEventListener('resize', onResize))

  const columns = computed<PostView[][]>(() => {
    const n = cols.value
    const buckets: PostView[][] = Array.from({ length: n }, () => [])
    const heights = new Array(n).fill(0)
    for (const p of posts.value) {
      let m = 0 // 最矮欄
      for (let i = 1; i < n; i++) if (heights[i] < heights[m]) m = i
      buckets[m].push(p)
      heights[m] += estHeight(p)
    }
    return buckets
  })

  return { columns }
}
