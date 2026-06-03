/* PosePlanner 找圖前端 — Vue 3（全域版，含模板編譯器）。只做搜尋 + 推薦 feed。
 *
 * 設定來自 config.js（由 build.py 產生）：
 *   window.POSEPLANNER_CONFIG = { baseUrl: "http://...:8000", token: "讀取token或空字串" }
 * 資料來自 server 的 GET /search?q=&tag=&limit=&offset= 與 GET /stats。
 * 圖片用 <img> 直接載 /thumbs、/images（token 走 ?t= 查詢參數）。
 */
(() => {
  "use strict";

  // Vue 還沒載到（vendored + CDN 都失敗）→ 退回最簡單的提示，別讓整頁空白。
  if (!window.Vue || window.__noVue) {
    document.getElementById("app").innerHTML =
      '<div style="max-width:560px;margin:80px auto;font-family:system-ui;text-align:center;color:#65676b">' +
      "<h2>載入不到 Vue</h2><p>vendored 的 <code>web/vendor/vue.global.prod.js</code> 不存在，且連 CDN 也失敗。" +
      "請在有網路的環境重跑 <code>python3 web/build.py</code>（會順手把 Vue 抓進 vendor/）。</p></div>";
    return;
  }

  const { createApp, ref, reactive, computed, onMounted, watch, nextTick } = window.Vue;

  const PAGE = 24;
  const MAX_POSTS = 300;   // feed 累積上限：避免圖庫大時 DOM 無限長把手機拖卡；超過提示用搜尋縮小範圍
  const cfg = window.POSEPLANNER_CONFIG || null;
  const BASE = cfg && cfg.baseUrl ? String(cfg.baseUrl).replace(/\/+$/, "") : "";
  const TOKEN = cfg && cfg.token ? String(cfg.token) : "";

  // ── URL / API 小工具 ─────────────────────────────────
  function mediaUrl(rel) {
    if (!rel) return "";
    let u = `${BASE}/${rel}`;
    if (TOKEN) u += (u.includes("?") ? "&" : "?") + "t=" + encodeURIComponent(TOKEN);
    return u;
  }
  function apiUrl(path, params) {
    const u = new URL(BASE + path);
    if (TOKEN) u.searchParams.set("t", TOKEN);
    for (const [k, v] of Object.entries(params || {})) {
      if (Array.isArray(v)) v.forEach((x) => u.searchParams.append(k, x));
      else if (v !== undefined && v !== null && v !== "") u.searchParams.set(k, v);
    }
    return u.toString();
  }
  async function apiGet(path, params) {
    const res = await fetch(apiUrl(path, params), { method: "GET" });
    if (!res.ok) {
      const body = await res.text().catch(() => "");
      throw new Error(`HTTP ${res.status}｜${body.slice(0, 160)}`);
    }
    return res.json();
  }

  // ── 把 server 的一筆結果整理成卡片要用的視圖物件 ────────
  function splitCreators(creators) {
    const photographers = [], models = [], others = [];
    for (const c of creators || []) {
      const role = (c.role || "").toLowerCase();
      if (role.includes("photo") || role === "攝影" || role === "攝影師") photographers.push(c);
      else if (role.includes("model") || role === "模特" || role === "模特兒") models.push(c);
      else others.push(c);
    }
    return { photographers, models, others };
  }
  const joinNames = (arr) => arr.map((c) => c.name).join("、");

  function normalize(item) {
    const { photographers, models, others } = splitCreators(item.creators);
    const author = photographers[0] || (item.creators && item.creators[0]) || null;
    const subParts = [];
    if (models.length) subParts.push("模特兒 " + joinNames(models));
    if (photographers.length && author && photographers[0] !== author) subParts.push("攝影 " + joinNames(photographers));
    if (others.length) subParts.push(joinNames(others));

    const badge = [];
    if (item.favorite) badge.push("★");
    if (item.rating) badge.push(item.rating + "/5");

    return {
      id: item.id,
      // 只用縮圖（1280px）：原圖端點已鎖讀寫 token，公開頁不暴露原圖網址。
      thumb: mediaUrl(item.thumbnail_path),
      desc: item.description || "",
      avatar: photographers.length ? "📷" : "👤",
      author: author ? author.name : "未署名",
      sub: subParts.join("　·　") || ("#" + item.id),
      badge: badge.join(" "),
      roleChips: (item.creators || []).map((c) => ({ role: c.role || "creator", name: c.name })),
      tagChips: (item.tags || []).map((t) => ({
        raw: t,
        val: t.includes(":") ? t.split(":").slice(1).join(":") : t,
      })),
    };
  }

  // ── Vue app ───────────────────────────────────────────
  const app = createApp({
    setup() {
      const q = ref("");
      const posts = ref([]);
      const loading = ref(false);
      const done = ref(false);
      const capped = ref(false);   // 已達 MAX_POSTS 累積上限（非真的沒資料了）
      const offset = ref(0);
      const activeTag = ref(null);
      const status = ref(null);
      const statusErr = ref(false);
      const stats = ref(null);
      const ready = ref(false);            // 設定 OK，可以查
      const lightbox = reactive({ open: false, src: "", cap: "" });
      const sentinel = ref(null);
      let debounceTimer = null;

      const feedTitle = computed(() => {
        if (activeTag.value) return "標籤：" + activeTag.value.split("=").slice(1).join("=");
        if (q.value.trim()) return "搜尋：" + q.value.trim();
        return "為你推薦";
      });
      const feedMeta = computed(() => (offset.value ? `已載入 ${offset.value} 張` : ""));
      const statsText = computed(() =>
        stats.value ? `${stats.value.poses ?? "?"} 張 · ${stats.value.creators ?? 0} 位創作者` : "");

      function setStatus(msg, isErr) { status.value = msg || null; statusErr.value = !!isErr; }

      async function loadPage() {
        if (loading.value || done.value || !ready.value) return;
        loading.value = true;
        try {
          const params = { limit: PAGE, offset: offset.value };
          if (q.value.trim()) params.q = q.value.trim();
          if (activeTag.value) params.tag = activeTag.value;
          const items = await apiGet("/search", params);
          if (!Array.isArray(items)) throw new Error("server 回傳非預期格式");

          if (offset.value === 0 && items.length === 0) {
            setStatus(q.value.trim() || activeTag.value
              ? "找不到符合的圖片，換個關鍵字試試。" : "庫裡還沒有圖片。");
          } else {
            setStatus(null);
          }
          posts.value.push(...items.map(normalize));
          offset.value += items.length;
          if (items.length < PAGE) done.value = true;
          if (posts.value.length >= MAX_POSTS) { done.value = true; capped.value = true; }
        } catch (err) {
          setStatus("連線失敗：" + err.message +
            "　（請確認 server domain / 讀取 token 是否正確，server 是否開著）", true);
          done.value = true;
        } finally {
          loading.value = false;
        }
      }

      function resetAndLoad() {
        posts.value = [];
        offset.value = 0;
        done.value = false;
        capped.value = false;
        setStatus(null);
        loadPage();
      }

      function search(tag) {
        activeTag.value = tag || null;
        if (tag) q.value = "";
        resetAndLoad();
      }
      // 搜尋結果的 tag 是 "category:name"，但 /search 的 tag 參數要 "category=name"
      function onTag(raw) { search(raw.replace(":", "=")); }
      function clearSearch() { q.value = ""; activeTag.value = null; resetAndLoad(); }

      // 打字 350ms 後自動查（清掉 tag 篩選）
      watch(q, () => {
        clearTimeout(debounceTimer);
        debounceTimer = setTimeout(() => { activeTag.value = null; resetAndLoad(); }, 350);
      });

      function openLightbox(p) {
        if (!p.thumb) return;
        lightbox.src = p.thumb; lightbox.cap = p.desc; lightbox.open = true;
        document.body.style.overflow = "hidden";
      }
      function closeLightbox() { lightbox.open = false; lightbox.src = ""; document.body.style.overflow = ""; }

      async function loadStats() {
        try { stats.value = await apiGet("/stats", {}); } catch { /* 靜默 */ }
      }

      onMounted(() => {
        if (!cfg || !BASE) {
          setStatus("找不到 config.js（或缺 baseUrl）。請先在終端機跑：python3 web/build.py" +
            "　（會問你 server domain 與『讀取』token）", true);
          return;
        }
        ready.value = true;
        document.addEventListener("keydown", (e) => { if (e.key === "Escape" && lightbox.open) closeLightbox(); });
        const io = new IntersectionObserver(
          (entries) => { if (entries.some((en) => en.isIntersecting)) loadPage(); },
          { rootMargin: "600px 0px" });
        nextTick(() => { if (sentinel.value) io.observe(sentinel.value); });
        loadStats();
        resetAndLoad();   // 進頁就先給推薦 feed（無關鍵字＝依收藏/評分/時間排序）
      });

      return {
        q, posts, loading, done, capped, status, statusErr, lightbox, sentinel,
        feedTitle, feedMeta, statsText, ready,
        search, onTag, clearSearch, openLightbox, closeLightbox,
      };
    },
    template: `
      <header class="topbar">
        <div class="bar-inner">
          <a class="brand" href="#" @click.prevent="clearSearch">
            <span class="logo">📸</span><span class="brand-name">PosePlanner</span>
          </a>
          <form class="searchbar" @submit.prevent="search(null)" autocomplete="off">
            <span class="search-ic">🔍</span>
            <input v-model="q" type="search" :disabled="!ready"
                   placeholder="搜尋姿勢、構圖、作品、創作者…例如「回眸 站姿」">
            <button v-if="q" type="button" class="clear-btn" title="清除" @click="clearSearch">✕</button>
          </form>
          <div class="stats-pill" :title="'庫狀態'">{{ statsText }}</div>
        </div>
      </header>

      <main class="container">
        <div class="feed-head">
          <h2>{{ feedTitle }}</h2>
          <span class="feed-meta">{{ feedMeta }}</span>
        </div>

        <div v-if="status" class="status" :class="{ error: statusErr }">{{ status }}</div>

        <div class="feed">
          <article v-for="p in posts" :key="p.id" class="post">
            <div class="post-head">
              <div class="avatar">{{ p.avatar }}</div>
              <div class="who">
                <div class="author">{{ p.author }}</div>
                <div class="sub">{{ p.sub }}</div>
              </div>
              <div v-if="p.badge" class="head-badge">{{ p.badge }}</div>
            </div>

            <div v-if="p.thumb" class="media" @click="openLightbox(p)">
              <img loading="lazy" :src="p.thumb" :alt="'pose #' + p.id">
            </div>
            <div v-else class="media no-img"><span>（無縮圖）</span></div>

            <div class="post-body">
              <p v-if="p.desc" class="caption">{{ p.desc }}</p>
              <div class="chips">
                <span v-for="c in p.roleChips" :key="c.role + c.name" class="chip role" :title="c.role">
                  {{ c.role }}：{{ c.name }}
                </span>
                <button v-for="t in p.tagChips" :key="t.raw" class="chip" @click="onTag(t.raw)">
                  #{{ t.val }}
                </button>
              </div>
            </div>
          </article>
        </div>

        <div v-if="loading" class="more-spin">載入中…</div>
        <div v-else-if="capped" class="more-spin">已顯示前 {{ posts.length }} 張 — 想找更多請用上方搜尋縮小範圍 🔍</div>
        <div ref="sentinel" class="sentinel"></div>
      </main>

      <div v-if="lightbox.open" class="lightbox" @click.self="closeLightbox">
        <button class="lb-close" title="關閉" @click="closeLightbox">✕</button>
        <figure class="lb-figure">
          <img :src="lightbox.src" alt="">
          <figcaption v-if="lightbox.cap">{{ lightbox.cap }}</figcaption>
        </figure>
      </div>
    `,
  });

  app.mount("#app");
})();
