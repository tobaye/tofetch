// ToFetch control panel (Windows local package).
//
// A small native companion window in the foxkit style: it supervises
// the background service (server.py on pythonw, no console window),
// shows verified state, hosts the association / autostart toggles and
// the tiered exits - while the actual downloading UI stays in the
// browser at http://127.0.0.1:8077/.
//
// The same exe is also the registered magnet / .torrent opener:
//   ToFetch.exe <magnet-or-torrent>  -> ensure service, submit, open UI
//   ToFetch.exe --background         -> service + tray only (autostart)
//   ToFetch.exe                      -> panel + service + tray
//
// .NET Framework 4.8 WinForms only - no external packages, so the exe
// stays tiny and runs on a stock Windows 10/11 install.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.Net.Http;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;

namespace ToFetchPanel
{
    internal static class Cfg
    {
        public const string Version = "1.4.0";
        public const string Base = "http://127.0.0.1:8077";
        public static string Home
        {
            get { return AppDomain.CurrentDomain.BaseDirectory.TrimEnd('\\'); }
        }
        public static string DownloadDir
        {
            get
            {
                return Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    "Downloads", "ToFetch");
            }
        }
    }

    internal static class Api
    {
        // The backend is always on this machine: never route through the
        // system proxy (a leftover proxy setting makes 127.0.0.1 requests
        // fail, and the panel then reports a running backend as stopped).
        private static readonly HttpClient Http =
            new HttpClient(new HttpClientHandler { UseProxy = false });
        private static readonly JavaScriptSerializer Json = new JavaScriptSerializer();

        public static Dictionary<string, object> Get(string path, int timeoutMs)
        {
            return Send("GET", path, null, timeoutMs);
        }

        public static Dictionary<string, object> Post(string path, object body, int timeoutMs)
        {
            return Send("POST", path, body, timeoutMs);
        }

        private static Dictionary<string, object> Send(string method, string path, object body, int timeoutMs)
        {
            try
            {
                using (var cts = new CancellationTokenSource(timeoutMs))
                using (var req = new HttpRequestMessage(new HttpMethod(method), Cfg.Base + path))
                {
                    if (body != null)
                        req.Content = new StringContent(Json.Serialize(body), Encoding.UTF8, "application/json");
                    using (var resp = Http.SendAsync(req, cts.Token).GetAwaiter().GetResult())
                    {
                        if (!resp.IsSuccessStatusCode) return null;
                        var text = resp.Content.ReadAsStringAsync().GetAwaiter().GetResult();
                        return Json.Deserialize<Dictionary<string, object>>(text);
                    }
                }
            }
            catch
            {
                return null;
            }
        }

        public static bool Truthy(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) && v is bool && (bool)v;
        }

        public static string Str(Dictionary<string, object> d, string key)
        {
            object v;
            return d != null && d.TryGetValue(key, out v) && v != null ? v.ToString() : "";
        }
    }

    internal static class Backend
    {
        public static bool IsUp()
        {
            return Api.Get("/api/state", 1500) != null;
        }

        public static bool EnsureStarted()
        {
            if (IsUp()) return true;
            try
            {
                Directory.CreateDirectory(Cfg.DownloadDir);
                var psi = new ProcessStartInfo
                {
                    FileName = Path.Combine(Cfg.Home, "python", "pythonw.exe"),
                    Arguments = "\"" + Path.Combine(Cfg.Home, "server.py") + "\"",
                    WorkingDirectory = Cfg.Home,
                    UseShellExecute = false,
                    CreateNoWindow = true,
                    WindowStyle = ProcessWindowStyle.Hidden,
                };
                psi.EnvironmentVariables["PYTHONUTF8"] = "1";
                psi.EnvironmentVariables["ARIA2_BIN"] = Path.Combine(Cfg.Home, "aria2c.exe");
                psi.EnvironmentVariables["TOFETCH_PORT"] = "8077";
                psi.EnvironmentVariables["TOFETCH_HOST"] = "127.0.0.1";
                psi.EnvironmentVariables["TOFETCH_PLATFORM"] = "windows";
                psi.EnvironmentVariables["TOFETCH_DOWNLOAD_DIR"] = Cfg.DownloadDir;
                Process.Start(psi);
            }
            catch
            {
                return false;
            }
            for (int i = 0; i < 40; i++)
            {
                if (IsUp()) return true;
                Thread.Sleep(500);
            }
            return false;
        }

        public static void OpenUi()
        {
            try { Process.Start(Cfg.Base + "/"); } catch { }
        }
    }

    internal static class Opener
    {
        // Registered handler path: a magnet link or .torrent file was
        // handed to us. Ensure the service, submit, open the UI, exit.
        public static void Run(string target)
        {
            if (!Backend.EnsureStarted()) return;
            try
            {
                if (target.EndsWith(".torrent", StringComparison.OrdinalIgnoreCase) && File.Exists(target))
                {
                    var data = Convert.ToBase64String(File.ReadAllBytes(target));
                    Api.Post("/api/torrent", new Dictionary<string, object> { { "data", data } }, 15000);
                }
                else
                {
                    Api.Post("/api/rpc", new Dictionary<string, object>
                    {
                        { "method", "addUri" },
                        { "params", new object[] { new object[] { target }, new Dictionary<string, object>() } },
                    }, 15000);
                }
            }
            catch { /* the UI opening below still lets the user add it */ }
            Backend.OpenUi();
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly Label _statusDot = new Label();
        private readonly Label _statusText = new Label();
        private readonly Label _engineText = new Label();
        private readonly Label _dirText = new Label();
        private readonly Label _verText = new Label();
        private readonly Button _btnOpen = new Button();
        private readonly Button _btnStart = new Button();
        private readonly Button _btnStop = new Button();
        private readonly Button _btnExit = new Button();
        private readonly CheckBox _chkAssoc = new CheckBox();
        private readonly CheckBox _chkAuto = new CheckBox();
        private readonly Label _ucHint = new Label();
        private readonly TextBox _log = new TextBox();
        private readonly NotifyIcon _tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
        private readonly bool _startHidden;
        private bool _running;
        private bool _userStopped;
        private bool _reallyExit;
        private bool _balloonShown;
        private bool _syncing;   // setting toggles from verified state
        private bool _migrated;  // old pythonw opener upgraded to the exe
        private int _tick;

        public MainForm(bool startHidden)
        {
            _startHidden = startHidden;
            Text = "ToFetch 控制面板";
            Font = new Font("Microsoft YaHei UI", 9f);
            ClientSize = new Size(520, 545);
            FormBorderStyle = FormBorderStyle.FixedSingle;
            MaximizeBox = false;
            StartPosition = FormStartPosition.CenterScreen;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            var title = new Label
            {
                Text = "ToFetch 下载管理器",
                Font = new Font(Font, FontStyle.Bold),
                AutoSize = true,
                Location = new Point(14, 10),
            };
            Controls.Add(title);
            var sub = new Label
            {
                Text = "服务在后台运行，下载界面在浏览器里打开。",
                ForeColor = Color.Gray,
                AutoSize = true,
                Location = new Point(14, 32),
            };
            Controls.Add(sub);

            var gStatus = new GroupBox { Text = "现在的状态", Location = new Point(12, 58), Size = new Size(496, 96) };
            _statusDot.Text = "●";
            _statusDot.Font = new Font(Font.FontFamily, 13f);
            _statusDot.ForeColor = Color.Gray;
            _statusDot.AutoSize = true;
            _statusDot.Location = new Point(12, 20);
            _statusText.Text = "正在检测…";
            _statusText.AutoSize = true;
            _statusText.Location = new Point(36, 24);
            _engineText.Text = "引擎：—";
            _engineText.AutoSize = true;
            _engineText.Location = new Point(36, 46);
            _dirText.Text = "下载目录：—";
            _dirText.AutoSize = true;
            _dirText.Location = new Point(36, 66);
            _verText.Text = "版本：" + Cfg.Version;
            _verText.ForeColor = Color.Gray;
            _verText.AutoSize = true;
            _verText.Location = new Point(380, 66);
            gStatus.Controls.AddRange(new Control[] { _statusDot, _statusText, _engineText, _dirText, _verText });
            Controls.Add(gStatus);

            _btnOpen.Text = "打开下载界面";
            _btnOpen.Location = new Point(12, 164);
            _btnOpen.Size = new Size(150, 30);
            _btnOpen.Click += async (s, e) => { if (await Task.Run(() => Backend.EnsureStarted())) { _userStopped = false; Backend.OpenUi(); } };
            _btnStart.Text = "启动服务";
            _btnStart.Location = new Point(172, 164);
            _btnStart.Size = new Size(110, 30);
            _btnStart.Click += async (s, e) =>
            {
                _userStopped = false;
                Log(await Task.Run(() => Backend.EnsureStarted()) ? "✓ 服务已启动" : "✗ 服务启动失败，看诊断日志排查");
                RefreshState();
            };
            _btnStop.Text = "停止服务";
            _btnStop.Location = new Point(292, 164);
            _btnStop.Size = new Size(110, 30);
            _btnStop.Click += (s, e) => StopService();
            Controls.AddRange(new Control[] { _btnOpen, _btnStart, _btnStop });

            var gLocal = new GroupBox { Text = "本机集成（状态实时回读，改动立即生效）", Location = new Point(12, 206), Size = new Size(496, 104) };
            _chkAssoc.Text = "磁力链接与 .torrent 文件关联到 ToFetch";
            _chkAssoc.AutoSize = true;
            _chkAssoc.Location = new Point(14, 24);
            _chkAssoc.CheckedChanged += async (s, e) => { if (!_syncing) await Toggle("assoc", _chkAssoc.Checked); };
            _chkAuto.Text = "开机自启（后台静默启动，不弹窗）";
            _chkAuto.AutoSize = true;
            _chkAuto.Location = new Point(14, 48);
            _chkAuto.CheckedChanged += async (s, e) => { if (!_syncing) await Toggle("autostart", _chkAuto.Checked); };
            _ucHint.ForeColor = Color.DarkGoldenrod;
            _ucHint.AutoSize = false;
            _ucHint.Location = new Point(14, 72);
            _ucHint.Size = new Size(468, 28);
            gLocal.Controls.AddRange(new Control[] { _chkAssoc, _chkAuto, _ucHint });
            Controls.Add(gLocal);

            _btnExit.Text = "退出 ToFetch";
            _btnExit.Location = new Point(378, 322);
            _btnExit.Size = new Size(130, 30);
            _btnExit.Click += (s, e) => FullExit();
            Controls.Add(_btnExit);
            var exitHint = new Label
            {
                Text = "退出 = 停服务＋注销关联＋取消自启，全部还给系统。",
                ForeColor = Color.Gray,
                AutoSize = true,
                Location = new Point(12, 330),
            };
            Controls.Add(exitHint);

            var gLog = new GroupBox { Text = "记录", Location = new Point(12, 362), Size = new Size(496, 170) };
            _log.Multiline = true;
            _log.ReadOnly = true;
            _log.ScrollBars = ScrollBars.Vertical;
            _log.Location = new Point(8, 20);
            _log.Size = new Size(480, 142);
            _log.BackColor = Color.White;
            gLog.Controls.Add(_log);
            Controls.Add(gLog);

            // tray
            _tray.Icon = Icon;
            _tray.Text = "ToFetch";
            _tray.Visible = true;
            _tray.DoubleClick += (s, e) => ShowPanel();
            var menu = new ContextMenuStrip();
            menu.Items.Add("打开面板", null, (s, e) => ShowPanel());
            menu.Items.Add("打开下载界面", null, async (s, e) => { if (await Task.Run(() => Backend.EnsureStarted())) Backend.OpenUi(); });
            menu.Items.Add("暂停全部", null, (s, e) =>
            {
                var r = Api.Post("/api/rpc", new Dictionary<string, object>
                    { { "method", "pauseAll" }, { "params", new object[0] } }, 5000);
                Log(r != null ? "✓ 已暂停全部任务" : "✗ 暂停全部失败（服务未运行或设了密码）");
            });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出 ToFetch", null, (s, e) => FullExit());
            _tray.ContextMenuStrip = menu;

            _timer.Interval = 2000;
            _timer.Tick += (s, e) => RefreshState();
            _timer.Start();

            Shown += async (s, e) =>
            {
                if (_startHidden) Hide();
                Log("面板已打开，服务在后台运行");
                // Never block the UI thread on service startup: while the
                // backend is still coming up the panel must stay clickable.
                if (!_userStopped) await Task.Run(() => Backend.EnsureStarted());
                RefreshState();
            };
            FormClosing += OnClosing;
        }

        private void ShowPanel()
        {
            Show();
            WindowState = FormWindowState.Normal;
            Activate();
        }

        private void OnClosing(object sender, FormClosingEventArgs e)
        {
            if (_reallyExit) { _tray.Visible = false; return; }
            if (e.CloseReason == CloseReason.UserClosing)
            {
                e.Cancel = true;
                Hide();
                if (!_balloonShown)
                {
                    _balloonShown = true;
                    _tray.ShowBalloonTip(2500, "ToFetch", "已最小化到托盘，后台继续下载。", ToolTipIcon.Info);
                }
                Log("提醒：关窗口只是最小化到托盘，后台继续下载；要全停请用「退出 ToFetch」");
            }
        }

        private void Log(string msg)
        {
            if (InvokeRequired) { BeginInvoke(new Action(() => Log(msg))); return; }
            _log.AppendText("[" + DateTime.Now.ToString("HH:mm:ss") + "] " + msg + Environment.NewLine);
        }

        private void RefreshState()
        {
            var st = Api.Get("/api/state", 1500);
            _running = st != null;
            _statusDot.ForeColor = _running ? Color.Green : Color.Gray;
            _statusText.Text = _running ? "运行中" : "已停止";
            _engineText.Text = "引擎：" + (!_running ? "—" : (Api.Truthy(st, "engine") ? "就绪" : "未就绪（看界面里的提示）"));
            if (_running) _dirText.Text = "下载目录：" + Api.Str(st, "download_dir");
            _btnStart.Enabled = !_running;
            _btnStop.Enabled = _running;
            _chkAssoc.Enabled = _running;
            _chkAuto.Enabled = _running;
            _tick++;
            if (_running && _tick % 5 == 1)
            {
                var ws = Api.Get("/api/win/state", 2000);
                if (ws != null) ApplyWinState(ws);
            }
        }

        private void ApplyWinState(Dictionary<string, object> ws)
        {
            _syncing = true;
            _chkAssoc.Checked = Api.Truthy(ws, "magnet") && Api.Truthy(ws, "torrent");
            _chkAuto.Checked = Api.Truthy(ws, "autostart");
            _syncing = false;
            // Migration: a v1.3.0 association points at pythonw +
            // tofetch-open.py. Rewrite it once to this exe so protocol
            // prompts say "ToFetch" and the old helper can retire.
            if (!_migrated && Api.Str(ws, "opener") == "py" && Api.Truthy(ws, "magnet"))
            {
                _migrated = true;
                Log("关联打开器从旧版升级为 ToFetch.exe");
                _ = Toggle("assoc", true);
            }
            var hints = new List<string>();
            var ucm = Api.Str(ws, "userchoice_magnet");
            if (ucm.Length > 0 && !ucm.ToLowerInvariant().StartsWith("tofetch"))
                hints.Add("MAGNET 被 " + ucm + " 在系统默认应用里占用，去 设置>应用>默认应用 改选 ToFetch");
            var uct = Api.Str(ws, "userchoice_torrent");
            if (uct.Length > 0 && !uct.ToLowerInvariant().StartsWith("tofetch"))
                hints.Add(".torrent 被 " + uct + " 在系统默认应用里占用，去 设置>应用>默认应用 改选 ToFetch");
            _ucHint.Text = hints.Count > 0 ? "提醒：" + string.Join("；", hints) : "";
        }

        private async Task Toggle(string kind, bool on)
        {
            var path = kind == "assoc" ? "/api/win/assoc" : "/api/win/autostart";
            var ws = await Task.Run(() => Api.Post(path, new Dictionary<string, object> { { "on", on } }, 10000));
            if (ws == null)
            {
                Log("✗ " + (kind == "assoc" ? "关联" : "自启") + "设置失败：连不上服务");
                RefreshState();
                return;
            }
            ApplyWinState(ws);
            bool ok = Api.Truthy(ws, "ok");
            var what = kind == "assoc" ? "关联" : "开机自启";
            Log(ok ? "✓ " + what + (on ? "已打开（已回读确认）" : "已关闭（已回读确认）")
                   : "✗ " + what + "没有完全生效：" + Api.Str(ws, "error") + "（开关已按实际状态回读）");
        }

        private void StopService()
        {
            if (MessageBox.Show(this,
                    "停止服务后，所有下载都会停摆，重新打开 ToFetch 才恢复。\n\n确定停止服务？",
                    "停止服务", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK)
                return;
            Api.Post("/api/exit", new Dictionary<string, object> { { "mode", "stop" } }, 5000);
            _userStopped = true;
            Log("✓ 服务已停止");
            Log("提醒：停止后无法用 ToFetch 下载；关联还在，点磁力仍可唤起");
            RefreshState();
        }

        private async void FullExit()
        {
            if (MessageBox.Show(this,
                    "退出 ToFetch 将：停止服务、注销磁力与 .torrent 关联、取消开机自启。\n\n退出后无法用 ToFetch 下载，点磁力也不会再唤起 ToFetch；要再用，重新打开 ToFetch、并在面板里重新打开关联即可。\n\n确定退出？",
                    "退出 ToFetch", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK)
                return;
            if (!Backend.IsUp()) await Task.Run(() => Backend.EnsureStarted());
            Api.Post("/api/exit", new Dictionary<string, object> { { "mode", "full" } }, 8000);
            Log("✓ 已退出，关联与自启已还给系统");
            _reallyExit = true;
            _tray.Visible = false;
            Application.Exit();
        }
    }

    internal static class Program
    {
        [STAThread]
        private static void Main(string[] args)
        {
            string target = null;
            bool background = false;
            foreach (var a in args)
            {
                if (a == "--background") background = true;
                else if (target == null) target = a;
            }
            if (target != null)
            {
                Opener.Run(target);
                return;
            }

            bool first;
            using (var mutex = new Mutex(true, "ToFetchPanelMutex", out first))
            {
                if (!first)
                {
                    // A panel is already running: ask it to come forward.
                    try
                    {
                        using (var ev = EventWaitHandle.OpenExisting("ToFetchPanelShow"))
                            ev.Set();
                    }
                    catch { }
                    return;
                }
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                var form = new MainForm(background);
                // Listen for "show yourself" signals from later launches.
                var listener = new Thread(() =>
                {
                    using (var ev = new EventWaitHandle(false, EventResetMode.AutoReset, "ToFetchPanelShow"))
                    {
                        while (true)
                        {
                            ev.WaitOne();
                            try { form.BeginInvoke(new Action(() =>
                            {
                                form.Show();
                                form.WindowState = FormWindowState.Normal;
                                form.Activate();
                            })); } catch { return; }
                        }
                    }
                });
                listener.IsBackground = true;
                listener.Start();
                Application.Run(form);
                GC.KeepAlive(mutex);
            }
        }
    }
}
