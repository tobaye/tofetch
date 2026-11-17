# ToFetch 路线图

来自 v1.0/v1.1 的实测反馈与竞品调研（Motrix v2、Rayburst、AriaNg、Persepolis、
Gopeed、AB Download Manager、FDM、IDM），按批次推进。原则不变：单文件后端、
零依赖、零 CDN；新行为类功能一律做成开关且默认关。

## 推进方式（2026-11-18 定）
- **四版协调**：服务器版（woiden 部署）＋安卓/Windows/Linux 三个本地包共用
  同一套 server.py / UI，是同一个产品的四个发行形态。优化必须四版协调推进：
  同一批、同一次发布、同一个版本号，不允许某一版单独冒进或长期掉队。
- **攒批优化**：优化项先累计进本文件，过段时间一次性做一批，不零敲碎打。
- **按版适配**：协调不等于一刀切。每项优化先判断适合哪些版：核心层的
  （会话保存、限速、图表）四版共享；平台专属的只进适合的版——如绑
  127.0.0.1 只进本地三包（服务器版的用途就是远程访问，不绑）、wake-lock
  只进安卓、systemd 示例只进 Linux/服务器。不适合的版不硬塞。
- **一批的完整动作**：改核心 → tests/run_tests.py 全套回归 → 三包重打＋
  服务器版同步升级 → 一次交付。平台专属项（安卓 install.sh、Windows 启动器、
  Linux systemd/aarch64 等）只改各自包，但搭同一批车发布。
- 每批做完用 tests/run_tests.py 全套回归（S1 软失败项在会话保存修复后转正）。

## 已完成（v1.2.0，可靠性批）
- 每次 addUri/addTorrent 成功后立即 saveSession；--save-session-interval
  30→5s（「加任务后立即强杀丢任务」已修，回归 T7 转正，8/8 全绿）
- 三个本地包启动脚本默认 TOFETCH_HOST=127.0.0.1（仅本地包；服务器版维持
  0.0.0.0＋密码）
- Linux 包双架构引擎（x86_64＋aarch64，官方校验和验过）＋启动脚本按
  uname -m 选引擎、不可用时自动改用系统 aria2
- Linux 包附 systemd user unit 示例
- README 与各包说明写明已知限制：强杀后进度回滚（实测 26MB→8MB），
  任务不丢、文件最终正确性不受影响

## 已完成（v1.1.0）
- 引擎启动失败在 UI 显眼报错并给出原因（不再静默 engine:false）
- 诊断日志落盘（data/tofetch.log + aria2.log），UI 可导出
- install.sh：镜像失败自动换源、按架构选引擎、install.log、结尾真自检
- 双开友好提示；thunder:// 自动解码；重复添加提醒（只提醒不拦截）
- 轮询降频（后台/无活动任务时）；任务速度为 0 持续 3 分钟的卡住提示
- Termux 启动时 wake-lock

## 下一批·功能（易做、收益高）
- 限速档位（可保存的几档配置）＋按时段自动切换限速
- 定时启停（到点 pauseAll / unpauseAll）
- 速度曲线图（采样 getGlobalStat）＋任务分片进度图（解析 bitfield）
- 任务列表搜索、排序；失败任务一键重试（重新 addUri）
- 批量添加：通配符展开（file[01-10].zip）、逐项勾选确认
- Tracker 列表定时更新（changeGlobalOption 热更新，失败回退旧列表）
- 已完成历史落盘（JSON 存数据目录，重启不丢，带清理入口）

## 再下一批
- BT 添加后先取元数据、文件树勾选再下载（select-file）
- 按扩展名自动分类子目录（开关，默认关）
- 添加任务时可选填 MD5/SHA-256，完成后自动校验比对
- 完成/失败通知（浏览器 Notification；Termux 可选 termux-notification，默认关）
- Webhook 回调（完成/失败 POST 到用户配置的 URL，默认关）
- 暗色/浅色切换（默认浅色——注：当前 UI 为深色，改默认浅色时一并处理）

## 远期 / 待评估
- 浏览器扩展接管下载（链接 + Cookie/Referer 直送 ToFetch）
- 多队列与各自定时（需在 server.py 维护队列状态，属新增子系统）
- 引擎可替换余量：已支持 ARIA2_BIN；将来可换 Aria2 Next 获取 HLS/DASH、ED2K

## 明确不做
- IDM 式动态分段（引擎内部调度，aria2 做不了也不必做）
- 云端离线下载/秒传/P2SP（依赖运营方基础设施，与自托管定位冲突）
- 插件系统/站点解析器（需 JS 沙箱，违背零依赖）
- 原生安卓 App（Termux + Web UI 形态已成立）
- 全局选项持久化：全局限速等全局选项存进 data 目录、启动时自动恢复（2026-11-18 安卓复测建议，先记下，功能批再做）
