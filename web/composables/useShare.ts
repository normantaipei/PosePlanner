// 分享單張圖的連結：手機優先用系統分享面板（navigator.share），
// 否則退回複製到剪貼簿並回報，呼叫端可據此閃一下「已複製」。
export function useShare() {
  const justCopied = ref(false)
  let timer: ReturnType<typeof setTimeout> | null = null

  // 由 pose id 組出可分享的絕對網址（client 端才有 location）。
  function shareUrl(id: number | string): string {
    if (!import.meta.client) return ''
    return `${location.origin}/p/${id}`
  }

  async function share(url: string, title = 'PosePlanner') {
    if (!import.meta.client || !url) return
    if (navigator.share) {
      try {
        await navigator.share({ title, url })
        return
      } catch (err: any) {
        // 使用者自己取消分享面板時不要再退回複製。
        if (err?.name === 'AbortError') return
      }
    }
    try {
      await navigator.clipboard.writeText(url)
      justCopied.value = true
      if (timer) clearTimeout(timer)
      timer = setTimeout(() => (justCopied.value = false), 1600)
    } catch {
      /* 沒有剪貼簿權限就靜默 */
    }
  }

  return { share, shareUrl, justCopied }
}
