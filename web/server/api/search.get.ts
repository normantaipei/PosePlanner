// GET /api/search?q=&tag=&limit=&offset=
// 代理到私有 DB server 的 /search，回傳結果陣列。token 在 server 端補上。
export default defineEventHandler(async (event) => {
  const { baseUrl, token } = backendConfig(event)
  const q = getQuery(event)

  const params: Record<string, unknown> = { t: token || undefined }
  for (const k of ['q', 'tag', 'limit', 'offset'] as const) {
    if (q[k] !== undefined && q[k] !== '') params[k] = q[k]
  }
  if (!token) delete params.t

  try {
    return await $fetch(`${baseUrl}/search`, { params })
  } catch (err: any) {
    throw createError({
      statusCode: err?.statusCode || err?.response?.status || 502,
      statusMessage: `搜尋失敗（後端 DB server 連線異常）：${err?.message || err}`,
    })
  }
})
