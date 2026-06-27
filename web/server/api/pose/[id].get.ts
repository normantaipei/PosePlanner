// GET /api/pose/:id
// 代理到私有 DB server 的 /poses/{id}，回傳單張 pose。token 在 server 端補上。
// 給「單圖分享頁」/p/<id> 用：別人開分享連結時只會看到這一張的縮圖資訊。
export default defineEventHandler(async (event) => {
  const { baseUrl, token } = backendConfig(event)
  const raw = getRouterParam(event, 'id') || ''
  // 只接受純數字 id，擋掉路徑穿越/亂打。
  if (!/^\d+$/.test(raw)) {
    throw createError({ statusCode: 400, statusMessage: '不合法的圖片 id' })
  }

  try {
    // 轉發真實 client IP，後端限流才能 per-user。
    return await $fetch(`${baseUrl}/poses/${raw}`, {
      params: token ? { t: token } : {},
      headers: { 'x-forwarded-for': clientIp(event) },
    })
  } catch (err: any) {
    const status = err?.statusCode || err?.response?.status || 502
    if (status === 404) {
      throw createError({ statusCode: 404, statusMessage: '找不到這張圖片' })
    }
    // 其餘後端錯誤只記在 server，不把含內網 base_url 的原文回給瀏覽器。
    console.error('[api/pose] 後端連線異常：', err?.message || err)
    throw createError({ statusCode: status, statusMessage: '讀取失敗，請稍後再試' })
  }
})
