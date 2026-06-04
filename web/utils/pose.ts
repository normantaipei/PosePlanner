// 把 server /search 回傳的一筆原始資料整理成卡片視圖物件。純函式、好測。
// 圖片改走 Nuxt 代理：/api/media/<相對路徑>（token 由 server 端補，前端不碰）。

export interface Creator {
  role?: string
  name: string
}

export interface RawPose {
  id: number
  thumbnail_path?: string
  description?: string
  favorite?: boolean
  rating?: number
  creators?: Creator[]
  tags?: string[]
}

export interface PostView {
  id: number
  thumb: string
  desc: string
  avatar: string
  author: string
  sub: string
  badge: string
  roleChips: { role: string; name: string }[]
  tagChips: { raw: string; val: string }[]
}

export function mediaUrl(rel?: string): string {
  if (!rel) return ''
  return `/api/media/${String(rel).replace(/^\/+/, '')}`
}

function splitCreators(creators?: Creator[]) {
  const photographers: Creator[] = []
  const models: Creator[] = []
  const others: Creator[] = []
  for (const c of creators || []) {
    const role = (c.role || '').toLowerCase()
    if (role.includes('photo') || role === '攝影' || role === '攝影師') photographers.push(c)
    else if (role.includes('model') || role === '模特' || role === '模特兒') models.push(c)
    else others.push(c)
  }
  return { photographers, models, others }
}

const joinNames = (arr: Creator[]) => arr.map((c) => c.name).join('、')

export function normalize(item: RawPose): PostView {
  const { photographers, models, others } = splitCreators(item.creators)
  const author = photographers[0] || (item.creators && item.creators[0]) || null

  const subParts: string[] = []
  if (models.length) subParts.push('模特兒 ' + joinNames(models))
  if (photographers.length && author && photographers[0] !== author)
    subParts.push('攝影 ' + joinNames(photographers))
  if (others.length) subParts.push(joinNames(others))

  const badge: string[] = []
  if (item.favorite) badge.push('★')
  if (item.rating) badge.push(item.rating + '/5')

  return {
    id: item.id,
    // 只用縮圖（後端鎖 token 的原圖端點不對外暴露）。
    thumb: mediaUrl(item.thumbnail_path),
    desc: item.description || '',
    avatar: photographers.length ? '📷' : '👤',
    author: author ? author.name : '未署名',
    sub: subParts.join('　·　') || '#' + item.id,
    badge: badge.join(' '),
    roleChips: (item.creators || []).map((c) => ({ role: c.role || 'creator', name: c.name })),
    tagChips: (item.tags || []).map((t) => ({
      raw: t,
      val: t.includes(':') ? t.split(':').slice(1).join(':') : t,
    })),
  }
}
