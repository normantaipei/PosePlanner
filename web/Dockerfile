# PosePlanner 找圖前端（Nuxt 3）— 多階段建構。
# 產出只含 Node runtime + .output 的精簡映像；後端 domain 與 token 都「不烤進映像」，
# 一律在 docker run 時用環境變數帶進來（NUXT_POSEPLANNER_BASE_URL / NUXT_POSEPLANNER_TOKEN）。

# ── build ───────────────────────────────────────────────
# 用 Debian slim（glibc）而非 alpine（musl）：避開 Nuxt 依賴的原生套件
# （oxc-parser / rollup）在 musl 上的 native binding 問題。
FROM node:22-slim AS build
WORKDIR /app
# 只帶 package.json（不帶 host 的 package-lock）：host 在 macOS 產的 lockfile 沒有
# linux 的 optional 原生套件（@oxc-parser/binding-linux-*），會觸發 npm/cli#4828。
# 讓容器自己依「實際建構平台」乾淨解析，原生 binding 才會齊。
# --ignore-scripts：跳過 postinstall 的 `nuxt prepare`（此時還沒 COPY 原始碼會失敗）；
# 接著的 `npm run build` 自己會做 prepare。
COPY package.json ./
RUN npm install --ignore-scripts --no-audit --no-fund
COPY . .
RUN npm run build

# ── runtime ─────────────────────────────────────────────
FROM node:22-slim AS runtime
WORKDIR /app
ENV NODE_ENV=production \
    NITRO_HOST=0.0.0.0 \
    NITRO_PORT=3000 \
    PORT=3000
# 只帶執行所需的產物（不含 node_modules / 原始碼）。
COPY --from=build /app/.output ./.output
EXPOSE 3000
CMD ["node", ".output/server/index.mjs"]
