// 後端代理共用工具：從 runtimeConfig 取私有 DB server 的 baseUrl + token，
// 組出帶 token 的目標網址。token 只在 server 端使用，不會回傳給瀏覽器。
import type { H3Event } from 'h3'

export function backendConfig(event: H3Event) {
  const cfg = useRuntimeConfig(event)
  const baseUrl = String(cfg.poseplannerBaseUrl || '').replace(/\/+$/, '')
  const token = String(cfg.poseplannerToken || '')
  if (!baseUrl) {
    throw createError({
      statusCode: 500,
      statusMessage:
        '尚未設定 NUXT_POSEPLANNER_BASE_URL（你的私有 DB server domain）。請看 web/.env.example。',
    })
  }
  return { baseUrl, token }
}

// 把 token 併進 query：後端用 ?t= 查詢參數驗證讀取權限。
export function withToken(
  params: Record<string, unknown> | undefined,
  token: string,
): Record<string, unknown> {
  const out: Record<string, unknown> = { ...(params || {}) }
  if (token) out.t = token
  return out
}
