/* UI-only locale state. Server facts, paths and code remain verbatim. */
const messages = {
  zh: {
    lang: "EN", skip: "跳至工作区", brand: "工程安全台", workspace: "工作空间", tasks: "开发任务", newTask: "创建任务", files: "文件审阅", boundary: "隔离候选模式", boundaryText: "先确认范围，再生成副本。\n正式发布与数据库写入关闭。", connected: "后端已连接", connecting: "连接服务中", disconnected: "后端未连接 / 状态未知", localDesk: "本机工作台 / 审计优先", slogan: "提议 ≠ 批准 ≠ 执行 ≠ 验证", footer: "仅展示服务端事实 · 未接入的能力不会标记完成", loading: "正在读取工作区…", english: "切换到 English", chinese: "切换到中文"
  },
  en: {
    lang: "中", skip: "Skip to workspace", brand: "Engineering Control", workspace: "Workspace", tasks: "Development tasks", newTask: "Create task", files: "File review", boundary: "Isolated candidate mode", boundaryText: "Confirm scope before creating a copy.\nProduction release and database writes are disabled.", connected: "Backend connected", connecting: "Connecting", disconnected: "Backend unavailable / status unknown", localDesk: "Local workbench / audit first", slogan: "Proposal ≠ approval ≠ execution ≠ verification", footer: "Server facts only · unavailable capabilities are never marked complete", loading: "Loading workspace…", english: "Switch to English", chinese: "切换到中文"
  }
};
let locale = "zh";
try { locale = localStorage.getItem("longflow.locale") === "en" ? "en" : "zh"; } catch {}
export function currentLocale() { return locale; }
export function t(key) { return messages[locale][key] ?? messages.zh[key] ?? key; }
export function setLocale(next) { locale = next === "en" ? "en" : "zh"; try { localStorage.setItem("longflow.locale", locale); } catch {} document.documentElement.lang = locale === "en" ? "en" : "zh-CN"; window.dispatchEvent(new CustomEvent("longflow-locale", { detail: locale })); }
export function toggleLocale() { setLocale(locale === "zh" ? "en" : "zh"); }
export function localeLabel() { return locale === "zh" ? messages.zh.lang : messages.en.lang; }
export function localeTitle() { return locale === "zh" ? messages.zh.english : messages.en.chinese; }
const dynamic = { "刷新": "Refresh", "新建任务": "New task", "返回任务": "Back to tasks", "开发任务": "Development task", "工程任务控制台": "Engineering task console", "任务队列": "Task queue", "暂无任务": "No tasks yet", "任务加载失败": "Failed to load tasks", "查看任务": "Open task", "选择本机文件夹": "Choose local folder", "刷新工作区": "Refresh workspace", "添加文件": "Add file", "生成候选": "Generate candidate", "批准并应用": "Approve and apply", "精确候选差异": "Exact candidate diff", "版本历史": "Version history", "比较版本": "Compare versions", "查看文件版本": "View file version", "恢复预览": "Preview restore", "未选择文件": "No file selected", "未生成候选差异": "No candidate diff generated yet", "暂无暂存变更": "No staged changes", "后端未连接 / 状态未知": "Backend unavailable / status unknown", "连接服务中": "Connecting to backend" };
export function translateDynamic(root = document) {
  if (locale === "zh" || !root) return;
  const walk = node => {
    if (node.nodeType === 3 && node.parentElement && !["TEXTAREA", "PRE", "CODE"].includes(node.parentElement.tagName)) { let value = node.nodeValue; for (const [from, to] of Object.entries(dynamic)) value = value.split(from).join(to); node.nodeValue = value; } else node.childNodes?.forEach(walk);
  };
  walk(root);
}
