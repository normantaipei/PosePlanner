// GET /api/stats → 代理私有 DB server 的 /stats（{ poses, creators } 等）。
export default defineEventHandler(async (event) => {
  const { baseUrl, token } = backendConfig(event)
  try {
    return await $fetch(`${baseUrl}/stats`, {
      params: token ? { t: token } : {},
    })
  } catch (err: any) {
    throw createError({
      statusCode: err?.statusCode || 502,
      statusMessage: `讀取庫狀態失敗：${err?.message || err}`,
    })
  }
})
