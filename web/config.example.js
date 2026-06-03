// config.js 範本。實際的 config.js 由 web/build.py 產生（內含你的 server domain 與
// 『讀取』token），且被 .gitignore 排除、不進版控。手動建立時複製本檔為 config.js：
window.POSEPLANNER_CONFIG = {
  baseUrl: "http://192.168.1.50:8000",   // 你的私有 DB server domain
  token: ""                               // server 的 POSEPLANNER_READ_TOKEN；沒設就留空
};
