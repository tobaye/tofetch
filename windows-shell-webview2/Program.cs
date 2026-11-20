// ToFetch embedded shell (Windows local package, WebView2 flavor).
//
// Same backend and same web UI as the standard flavor, but the UI
// lives inside this exe's own window (WebView2 = the embeddable
// Edge) instead of an external browser: one window, no second
// window summoned. The panel functions of the standard flavor -
// service start/stop, verified association / autostart toggles,
// download-dir picking, one-click update, tiered exits - sit in the
// toolbar above the embedded page.
//
//   ToFetch.exe <magnet-or-torrent>  -> ensure service, submit, open
//   ToFetch.exe --background         -> service + tray only (autostart)
//   ToFetch.exe                      -> window + service + tray
//   ToFetch.exe --pick-folder <token> / --apply-update <zip> <pid> <dir>
//       One-shot helper modes, identical to the standard flavor.
//
// Requires the WebView2 Evergreen runtime (built into Windows 11,
// present on most Windows 10 installs); when it is missing the
// window says so and links to Microsoft's installer instead of
// failing silently.
using System;
using System.Collections.Generic;
using System.Diagnostics;
using System.Drawing;
using System.IO;
using System.IO.Compression;
using System.Net.Http;
using System.Runtime.InteropServices;
using System.Security.Cryptography;
using System.Text;
using System.Threading;
using System.Threading.Tasks;
using System.Web.Script.Serialization;
using System.Windows.Forms;
using Microsoft.Web.WebView2.Core;
using Microsoft.Web.WebView2.WinForms;
using Microsoft.Win32;

namespace ToFetchWebView
{
    internal static class Cfg
    {
        public const string Version = "1.5.0";
        // This flavor's own release asset. The one-click updater only
        // ever downloads this name - flavors never cross-update.
        public const string AssetName = "ToFetch-Windows-WebView2.zip";
        public const string RepoLatest =
            "https://api.github.com/repos/tobaye/tofetch/releases/latest";
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
        // fail, and the shell then reports a running backend as stopped).
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
    }

    internal static class FolderDialog
    {
        // The modern Explorer-style folder dialog (IFileDialog with
        // FOS_PICKFOLDERS) - the one Firefox/Chrome show for their
        // download folder. Hand-written COM interop, no packages.
        private const uint FOS_PICKFOLDERS = 0x00000020;
        private const uint FOS_FORCEFILESYSTEM = 0x00000040;
        private const uint FOS_PATHMUSTEXIST = 0x00000800;
        private const uint SIGDN_FILESYSPATH = 0x80058000;

        /// <summary>Chosen absolute path, or null when cancelled.</summary>
        public static string Pick(IntPtr owner, string initialDir, string title)
        {
            IFileOpenDialog dlg = null;
            try
            {
                dlg = (IFileOpenDialog)new FileOpenDialogCoClass();
                dlg.SetOptions(FOS_PICKFOLDERS | FOS_FORCEFILESYSTEM
                               | FOS_PATHMUSTEXIST);
                if (!string.IsNullOrEmpty(title)) dlg.SetTitle(title);
                if (!string.IsNullOrEmpty(initialDir)
                    && Directory.Exists(initialDir))
                {
                    IShellItem folder;
                    var iid = new Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE");
                    if (SHCreateItemFromParsingName(initialDir, IntPtr.Zero,
                            ref iid, out folder) == 0 && folder != null)
                    {
                        try { dlg.SetFolder(folder); } catch { }
                        Marshal.ReleaseComObject(folder);
                    }
                }
                if (dlg.Show(owner) != 0) return null;  // cancelled
                IShellItem item;
                dlg.GetResult(out item);
                if (item == null) return null;
                try
                {
                    string path;
                    item.GetDisplayName(SIGDN_FILESYSPATH, out path);
                    return path;
                }
                finally { Marshal.ReleaseComObject(item); }
            }
            catch { return null; }
            finally
            {
                if (dlg != null) Marshal.ReleaseComObject(dlg);
            }
        }

        [ComImport, Guid("43826D1E-E718-42EE-BC55-A1E261C37BFE"),
         InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IShellItem
        {
            void BindToHandler(IntPtr pbc, [In] ref Guid bhid,
                               [In] ref Guid riid, out IntPtr ppv);
            void GetParent(
                [MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
            void GetDisplayName(uint sigdnName,
                [MarshalAs(UnmanagedType.LPWStr)] out string ppszName);
            void GetAttributes(uint sfgaoMask, out uint psfgaoAttribs);
            void Compare(
                [MarshalAs(UnmanagedType.Interface)] IShellItem psi,
                uint hint, out int piOrder);
        }

        [StructLayout(LayoutKind.Sequential, CharSet = CharSet.Unicode)]
        private struct COMDLG_FILTERSPEC
        {
            [MarshalAs(UnmanagedType.LPWStr)] public string pszName;
            [MarshalAs(UnmanagedType.LPWStr)] public string pszSpec;
        }

        [ComImport, Guid("D57C7288-D4AD-4768-BE02-9D969532D960"),
         InterfaceType(ComInterfaceType.InterfaceIsIUnknown)]
        private interface IFileOpenDialog
        {
            // IFileDialog, in vtable order
            [PreserveSig] int Show(IntPtr hwndOwner);
            void SetFileTypes(uint cFileTypes,
                              [In] COMDLG_FILTERSPEC[] rgFilterSpec);
            void SetFileTypeIndex(uint iFileType);
            void GetFileTypeIndex(out uint piFileType);
            void Advise([MarshalAs(UnmanagedType.Interface)] object pfde,
                        out uint pdwCookie);
            void Unadvise(uint dwCookie);
            void SetOptions(uint fos);
            void GetOptions(out uint pfos);
            void SetDefaultFolder(
                [MarshalAs(UnmanagedType.Interface)] IShellItem psi);
            void SetFolder(
                [MarshalAs(UnmanagedType.Interface)] IShellItem psi);
            void GetFolder(
                [MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
            void GetCurrentSelection(
                [MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
            void SetFileName([MarshalAs(UnmanagedType.LPWStr)] string pszName);
            void GetFileName(
                [MarshalAs(UnmanagedType.LPWStr)] out string pszName);
            void SetTitle([MarshalAs(UnmanagedType.LPWStr)] string pszTitle);
            void SetOkButtonLabel(
                [MarshalAs(UnmanagedType.LPWStr)] string pszText);
            void SetFileNameLabel(
                [MarshalAs(UnmanagedType.LPWStr)] string pszLabel);
            void GetResult(
                [MarshalAs(UnmanagedType.Interface)] out IShellItem ppsi);
            void AddPlace(
                [MarshalAs(UnmanagedType.Interface)] IShellItem psi,
                uint fdap);
            void SetDefaultExtension(
                [MarshalAs(UnmanagedType.LPWStr)] string pszDefaultExtension);
            void Close(int hr);
            void SetClientGuid([In] ref Guid guid);
            void ClearClientData();
            void SetFilter([MarshalAs(UnmanagedType.Interface)] object pFilter);
            // IFileOpenDialog
            void GetResults(
                [MarshalAs(UnmanagedType.Interface)] out object ppenum);
            void GetSelectedItems(
                [MarshalAs(UnmanagedType.Interface)] out object ppsai);
        }

        [ComImport, Guid("DC1C5A9C-E88A-4DDE-A5A1-60F82A20AEF7")]
        private class FileOpenDialogCoClass { }

        [DllImport("shell32.dll", CharSet = CharSet.Unicode)]
        private static extern int SHCreateItemFromParsingName(
            string pszPath, IntPtr pbc, ref Guid riid,
            [MarshalAs(UnmanagedType.Interface)] out IShellItem ppv);
    }

    internal sealed class ReleaseInfo
    {
        public string Version = "";   // tag without the leading "v"
        public string AssetUrl = "";
        public long AssetSize;
        public string SumsUrl = "";   // "" when the release has none
    }

    internal static class Updater
    {
        // GitHub traffic may legitimately need the system proxy - the
        // loopback Api client disables it on purpose; this one doesn't.
        private static readonly HttpClient Net = new HttpClient();
        private static readonly JavaScriptSerializer Json =
            new JavaScriptSerializer();

        static Updater()
        {
            try
            {
                Net.DefaultRequestHeaders.UserAgent.ParseAdd(
                    "ToFetch/" + Cfg.Version);
                Net.Timeout = TimeSpan.FromMinutes(20);
            }
            catch { }
        }

        /// <summary>Semver-ish compare: -1 / 0 / 1. A release
        /// outranks its own prereleases (1.5.0 &gt; 1.5.0-rc.1).</summary>
        public static int CompareVersions(string a, string b)
        {
            string coreA, coreB;
            string[] preA, preB;
            SplitVersion(a, out coreA, out preA);
            SplitVersion(b, out coreB, out preB);
            var na = coreA.Split('.');
            var nb = coreB.Split('.');
            for (int i = 0; i < Math.Max(na.Length, nb.Length); i++)
            {
                int va = i < na.Length ? CoreNum(na[i]) : 0;
                int vb = i < nb.Length ? CoreNum(nb[i]) : 0;
                if (va != vb) return va < vb ? -1 : 1;
            }
            if (preA == null && preB == null) return 0;
            if (preA == null) return 1;
            if (preB == null) return -1;
            for (int i = 0; i < Math.Max(preA.Length, preB.Length); i++)
            {
                if (i >= preA.Length) return -1;
                if (i >= preB.Length) return 1;
                int ia, ib;
                bool isNumA = int.TryParse(preA[i], out ia);
                bool isNumB = int.TryParse(preB[i], out ib);
                if (isNumA && isNumB)
                {
                    if (ia != ib) return ia < ib ? -1 : 1;
                }
                else if (isNumA) return -1;  // numeric < alphanumeric
                else if (isNumB) return 1;
                else
                {
                    int c = string.CompareOrdinal(preA[i], preB[i]);
                    if (c != 0) return c < 0 ? -1 : 1;
                }
            }
            return 0;
        }

        private static void SplitVersion(string v, out string core,
                                         out string[] pre)
        {
            v = (v ?? "").Trim();
            if (v.StartsWith("v") || v.StartsWith("V")) v = v.Substring(1);
            int dash = v.IndexOf('-');
            if (dash < 0) { core = v; pre = null; }
            else
            {
                core = v.Substring(0, dash);
                pre = v.Substring(dash + 1).Split('.');
            }
        }

        private static int CoreNum(string s)
        {
            int n;
            return int.TryParse(s, out n) ? n : 0;
        }

        public static ReleaseInfo FetchLatest(out string error)
        {
            error = null;
            try
            {
                var text = Net.GetStringAsync(Cfg.RepoLatest)
                              .GetAwaiter().GetResult();
                var root = Json.Deserialize<Dictionary<string, object>>(text);
                var rel = new ReleaseInfo();
                object tag;
                if (root == null || !root.TryGetValue("tag_name", out tag)
                    || tag == null)
                {
                    error = "GitHub 返回的内容里没有版本号";
                    return null;
                }
                rel.Version = tag.ToString().TrimStart('v', 'V');
                object assetsObj;
                var assets = root.TryGetValue("assets", out assetsObj)
                    ? assetsObj as object[] : null;
                if (assets != null)
                {
                    foreach (var a in assets)
                    {
                        var d = a as Dictionary<string, object>;
                        if (d == null) continue;
                        var name = Api.Str(d, "name");
                        if (name == Cfg.AssetName)
                        {
                            rel.AssetUrl = Api.Str(d, "browser_download_url");
                            object sz;
                            if (d.TryGetValue("size", out sz) && sz != null)
                                rel.AssetSize = Convert.ToInt64(sz);
                        }
                        else if (name == "SHA256SUMS.txt")
                        {
                            rel.SumsUrl = Api.Str(d, "browser_download_url");
                        }
                    }
                }
                if (rel.AssetUrl.Length == 0)
                {
                    error = "最新版里没有 " + Cfg.AssetName + " 这个包";
                    return null;
                }
                return rel;
            }
            catch (Exception ex)
            {
                error = "连不上 GitHub（" + ex.Message + "）";
                return null;
            }
        }

        /// <summary>The sha256 recorded for an asset, or null. The
        /// sums file is the only source of truth - no line, no trust.</summary>
        public static string ExpectedHash(string sumsText, string assetName)
        {
            if (string.IsNullOrEmpty(sumsText)) return null;
            foreach (var rawLine in sumsText.Split('\n'))
            {
                var line = rawLine.Trim();
                if (line.Length == 0 || line.StartsWith("#")) continue;
                var parts = line.Split(new[] { ' ', '\t' },
                                       StringSplitOptions.RemoveEmptyEntries);
                if (parts.Length < 2) continue;
                var hash = parts[0].ToLowerInvariant();
                if (hash.Length != 64) continue;
                var name = parts[parts.Length - 1].TrimStart('*');
                int slash = name.LastIndexOf('/');
                if (slash >= 0) name = name.Substring(slash + 1);
                if (string.Equals(name, assetName, StringComparison.Ordinal))
                    return hash;
            }
            return null;
        }

        /// <summary>Fail-closed verification of a downloaded zip:
        /// no sums asset, no line for our asset, or a hash mismatch
        /// all refuse the update.</summary>
        public static bool VerifyDownloaded(string zipPath, ReleaseInfo rel,
                                            out string error)
        {
            error = null;
            if (rel.SumsUrl.Length == 0)
            {
                error = "最新版没有附 SHA256SUMS.txt 校验文件，拒绝更新";
                return false;
            }
            string sums;
            try
            {
                sums = Net.GetStringAsync(rel.SumsUrl)
                          .GetAwaiter().GetResult();
            }
            catch
            {
                error = "校验文件下载失败，拒绝更新";
                return false;
            }
            var want = ExpectedHash(sums, Cfg.AssetName);
            if (want == null)
            {
                error = "校验文件里没有 " + Cfg.AssetName + " 的记录，拒绝更新";
                return false;
            }
            string got;
            using (var fs = File.OpenRead(zipPath))
            using (var sha = SHA256.Create())
                got = BitConverter.ToString(sha.ComputeHash(fs))
                                  .Replace("-", "").ToLowerInvariant();
            if (got != want)
            {
                error = "下载的包校验不通过（SHA-256 不一致），拒绝更新";
                return false;
            }
            return true;
        }

        public static async Task Download(string url, string dest,
                                          Action<int> onProgress)
        {
            using (var resp = await Net.GetAsync(
                       url, HttpCompletionOption.ResponseHeadersRead))
            {
                resp.EnsureSuccessStatusCode();
                long total = resp.Content.Headers.ContentLength ?? -1;
                using (var src = await resp.Content.ReadAsStreamAsync())
                using (var dst = File.Create(dest))
                {
                    var buf = new byte[1 << 16];
                    long done = 0;
                    int lastPct = -1, n;
                    while ((n = await src.ReadAsync(buf, 0, buf.Length)) > 0)
                    {
                        await dst.WriteAsync(buf, 0, n);
                        done += n;
                        if (total > 0 && onProgress != null)
                        {
                            int pct = (int)(done * 100 / total);
                            if (pct != lastPct)
                            {
                                lastPct = pct;
                                onProgress(pct);
                            }
                        }
                    }
                }
            }
        }
    }

    internal static class UpdateRelay
    {
        // Second half of the one-click update. The shell copies
        // itself to a temp dir and launches that copy with
        // --apply-update; running from temp, this process may replace
        // the shell's own exe once the shell has exited. The install's
        // data/ folder (settings, history, sessions, the WebView2
        // profile) is never backed up, replaced or restored - it
        // simply stays where it is throughout.
        public static void Run(string zip, int panelPid, string home)
        {
            try
            {
                if (panelPid > 0)
                    Process.GetProcessById(panelPid).WaitForExit(30000);
            }
            catch { }
            Thread.Sleep(800);
            string parent = Directory.GetParent(home).FullName;
            string oldVer = "old";
            try
            {
                var fv = FileVersionInfo.GetVersionInfo(
                    Path.Combine(home, "ToFetch.exe")).FileVersion;
                if (!string.IsNullOrEmpty(fv))
                {
                    var p = fv.Split('.');
                    oldVer = p.Length == 4 && p[3] == "0"
                        ? string.Join(".", p, 0, 3) : fv;
                }
            }
            catch { }
            string backup = Path.Combine(parent, "ToFetch-backup-" + oldVer);
            try
            {
                if (Directory.Exists(backup)) Directory.Delete(backup, true);
                CopyTree(home, backup);
                ExtractPackage(zip, home);
                bool ok = File.Exists(Path.Combine(home, "ToFetch.exe"))
                    && File.Exists(Path.Combine(home, "server.py"))
                    && File.Exists(Path.Combine(home, "static", "index.html"));
                if (!ok)
                {
                    // The new exe is gone (antivirus quarantine of the
                    // unsigned binary - it has happened on the user's
                    // machine) or the package was incomplete: put the
                    // previous install back wholesale and say so.
                    CopyTree(backup, home);
                    MessageBox.Show(
                        "更新没能完成：新版程序文件不完整，可能被杀毒软件隔离了。已恢复到更新前的版本。\n\n" +
                        "请先把 " + home + " 整个文件夹加入杀毒软件的信任区，再重试检查更新。",
                        "ToFetch 更新", MessageBoxButtons.OK,
                        MessageBoxIcon.Warning);
                }
            }
            catch (Exception ex)
            {
                try { CopyTree(backup, home); } catch { }
                MessageBox.Show(
                    "更新没能完成（" + ex.Message + "），已恢复到更新前的版本。",
                    "ToFetch 更新", MessageBoxButtons.OK,
                    MessageBoxIcon.Warning);
            }
            // Either way the (new or restored) shell comes back up.
            try { Process.Start(Path.Combine(home, "ToFetch.exe")); }
            catch { }
        }

        private static void CopyTree(string src, string dst)
        {
            Directory.CreateDirectory(dst);
            foreach (var f in Directory.GetFiles(src))
                File.Copy(f, Path.Combine(dst, Path.GetFileName(f)), true);
            foreach (var d in Directory.GetDirectories(src))
            {
                var name = Path.GetFileName(d);
                // data/ holds settings/history/sessions: it is the
                // one folder an update must never carry away or
                // overwrite, so backups and restores skip it.
                if (string.Equals(name, "data",
                        StringComparison.OrdinalIgnoreCase))
                    continue;
                CopyTree(d, Path.Combine(dst, name));
            }
        }

        private static void ExtractPackage(string zip, string home)
        {
            using (var za = ZipFile.OpenRead(zip))
            {
                foreach (var entry in za.Entries)
                {
                    var name = entry.FullName.Replace('\\', '/');
                    if (!name.StartsWith("ToFetch/")) continue;
                    var rel = name.Substring("ToFetch/".Length);
                    if (rel.Length == 0 || rel.EndsWith("/")) continue;
                    if (rel == "data" || rel.StartsWith("data/")) continue;
                    var dest = Path.GetFullPath(Path.Combine(home, rel));
                    var root = Path.GetFullPath(home)
                        + Path.DirectorySeparatorChar;
                    if (!dest.StartsWith(root,
                            StringComparison.OrdinalIgnoreCase))
                        continue;  // zip-slip guard
                    Directory.CreateDirectory(
                        Path.GetDirectoryName(dest));
                    entry.ExtractToFile(dest, true);
                }
            }
        }
    }

    internal static class PickFolderMode
    {
        // One-shot: the backend minted a token, the web page is
        // polling for the result. Show the dialog, post the choice
        // back, exit - no window, no tray, no service control.
        public static void Run(string token)
        {
            string initial = "";
            var st = Api.Get("/api/state", 3000);
            if (st != null) initial = Api.Str(st, "download_dir");
            if (initial.Length == 0 || !Directory.Exists(initial))
                initial = Cfg.DownloadDir;
            var chosen = FolderDialog.Pick(IntPtr.Zero, initial,
                                           "选择下载目录");
            try
            {
                Api.Post("/api/win/pick-folder/result",
                    new Dictionary<string, object>
                    {
                        { "token", token },
                        { "path", chosen },
                    }, 10000);
            }
            catch { }
        }
    }

    internal static class Opener
    {
        // Registered handler path: a magnet link or .torrent file was
        // handed to us. Ensure the service and submit; the window
        // itself is opened by Main right after.
        public static void Submit(string target)
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
            catch { /* the UI opening anyway still lets the user add it */ }
        }
    }

    internal sealed class MainForm : Form
    {
        private readonly ToolStrip _bar = new ToolStrip();
        private readonly ToolStripLabel _svcLabel = new ToolStripLabel("服务：…");
        private readonly ToolStripButton _btnStart = new ToolStripButton("启动服务");
        private readonly ToolStripButton _btnStop = new ToolStripButton("停止服务");
        private readonly ToolStripButton _btnAssoc = new ToolStripButton("关联：…");
        private readonly ToolStripButton _btnAuto = new ToolStripButton("开机自启：…");
        private readonly ToolStripButton _btnDir = new ToolStripButton("下载目录…");
        private readonly ToolStripButton _btnUpdate = new ToolStripButton("检查更新");
        private readonly ToolStripButton _btnExit = new ToolStripButton("退出");
        private readonly WebView2 _web = new WebView2();
        private readonly StatusStrip _status = new StatusStrip();
        private readonly ToolStripStatusLabel _msg = new ToolStripStatusLabel();
        private readonly LinkLabel _fallback = new LinkLabel();
        private readonly NotifyIcon _tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
        private readonly System.Windows.Forms.Timer _armTimer = new System.Windows.Forms.Timer();
        private readonly bool _startHidden;
        private bool _running;
        private bool _userStopped;
        private bool _stopArmed;
        private bool _exitArmed;
        private bool _reallyExit;
        private bool _balloonShown;
        private bool _syncing;   // setting toggles from verified state
        private bool _migrated;  // old pythonw opener upgraded to the exe
        private int _tick;
        private ReleaseInfo _pendingUpdate;
        private bool _updateBusy;
        private string _lastHint = "";

        public MainForm(bool startHidden)
        {
            _startHidden = startHidden;
            Text = "ToFetch";
            Font = new Font("Microsoft YaHei UI", 9f);
            ClientSize = new Size(1120, 740);
            MinimumSize = new Size(860, 580);
            StartPosition = FormStartPosition.CenterScreen;
            try { Icon = Icon.ExtractAssociatedIcon(Application.ExecutablePath); } catch { }

            _bar.GripStyle = ToolStripGripStyle.Hidden;
            _bar.Items.Add(_svcLabel);
            _bar.Items.Add(new ToolStripSeparator());
            _bar.Items.Add(_btnStart);
            _bar.Items.Add(_btnStop);
            _bar.Items.Add(new ToolStripSeparator());
            _bar.Items.Add(_btnAssoc);
            _bar.Items.Add(_btnAuto);
            _bar.Items.Add(_btnDir);
            _bar.Items.Add(new ToolStripSeparator());
            _bar.Items.Add(_btnUpdate);
            _bar.Items.Add(_btnExit);
            Controls.Add(_bar);

            _btnStart.Click += async (s, e) =>
            {
                _userStopped = false;
                Msg(await Task.Run(() => Backend.EnsureStarted()) ? "✓ 服务已启动" : "✗ 服务启动失败，看网页里的诊断日志排查");
                RefreshState();
            };
            _btnStop.Click += (s, e) => StopService();
            _btnAssoc.Click += async (s, e) => { if (!_syncing) await Toggle("assoc"); };
            _btnAuto.Click += async (s, e) => { if (!_syncing) await Toggle("autostart"); };
            _btnDir.Click += async (s, e) => await PickDownloadDir();
            _btnUpdate.Click += async (s, e) => await CheckUpdate();
            _btnExit.Click += (s, e) => FullExit();

            _web.Dock = DockStyle.Fill;
            Controls.Add(_web);
            _web.BringToFront();

            _fallback.Visible = false;
            _fallback.Dock = DockStyle.Fill;
            _fallback.TextAlign = ContentAlignment.MiddleCenter;
            _fallback.Font = new Font(Font.FontFamily, 11f);
            _fallback.Text = "这台电脑缺少 WebView2 运行时，界面打不开。\n点这里去微软官网下载安装（免费），装好后重新打开 ToFetch。";
            _fallback.LinkClicked += (s, e) =>
            {
                try { Process.Start("https://go.microsoft.com/fwlink/p/?LinkId=2124703"); } catch { }
            };
            Controls.Add(_fallback);

            _msg.Text = "ToFetch " + Cfg.Version;
            _status.Items.Add(_msg);
            Controls.Add(_status);

            // tray
            _tray.Icon = Icon;
            _tray.Text = "ToFetch";
            _tray.Visible = true;
            _tray.DoubleClick += (s, e) => ShowPanel();
            var menu = new ContextMenuStrip();
            menu.Items.Add("打开面板", null, (s, e) => ShowPanel());
            menu.Items.Add("暂停全部", null, (s, e) =>
            {
                var r = Api.Post("/api/rpc", new Dictionary<string, object>
                    { { "method", "pauseAll" }, { "params", new object[0] } }, 5000);
                Msg(r != null ? "✓ 已暂停全部任务" : "✗ 暂停全部失败（服务未运行或设了密码）");
            });
            menu.Items.Add(new ToolStripSeparator());
            menu.Items.Add("退出 ToFetch", null, (s, e) => FullExitFromTray());
            _tray.ContextMenuStrip = menu;

            _timer.Interval = 2000;
            _timer.Tick += (s, e) => RefreshState();
            _timer.Start();
            _armTimer.Interval = 4000;
            _armTimer.Tick += (s, e) => DisarmConfirms();

            Shown += async (s, e) =>
            {
                if (_startHidden) Hide();
                if (!_userStopped) await Task.Run(() => Backend.EnsureStarted());
                RefreshState();
                CleanupUpdateBackup();
                await InitWeb();
            };
            FormClosing += OnClosing;
        }

        private async Task InitWeb()
        {
            try
            {
                // The browser profile lives in data/ on purpose: an
                // update never replaces data/, so logins and page
                // state survive one-click updates.
                var udf = Path.Combine(Cfg.Home, "data", "webview2");
                Directory.CreateDirectory(udf);
                var env = await CoreWebView2Environment.CreateAsync(null, udf);
                await _web.EnsureCoreWebView2Async(env);
                _web.CoreWebView2.NewWindowRequested += (s, e) =>
                {
                    e.Handled = true;
                    _web.CoreWebView2.Navigate(e.Uri);
                };
                _web.CoreWebView2.DownloadStarting += OnDownloadStarting;
                _web.Source = new Uri(Cfg.Base + "/");
            }
            catch (Exception ex)
            {
                _web.Visible = false;
                _fallback.Visible = true;
                _fallback.BringToFront();
                Msg("✗ WebView2 运行时不可用：" + ex.Message);
            }
        }

        private void OnDownloadStarting(object sender,
                                        CoreWebView2DownloadStartingEventArgs e)
        {
            // Page-initiated downloads (diagnostics log, settings and
            // task exports) land in the user's Downloads folder under
            // their suggested name; completion is reported in the
            // status bar since there is no browser download shelf.
            try
            {
                var dir = Path.Combine(
                    Environment.GetFolderPath(Environment.SpecialFolder.UserProfile),
                    "Downloads");
                Directory.CreateDirectory(dir);
                var name = "tofetch-download";
                if (!string.IsNullOrEmpty(e.ResultFilePath))
                    name = Path.GetFileName(e.ResultFilePath);
                var dest = Path.Combine(dir, name);
                int i = 1;
                while (File.Exists(dest))
                {
                    dest = Path.Combine(dir,
                        Path.GetFileNameWithoutExtension(name) + " (" + i + ")"
                        + Path.GetExtension(name));
                    i++;
                }
                e.ResultFilePath = dest;
                var op = e.DownloadOperation;
                op.StateChanged += (s2, e2) =>
                {
                    if (op.State == CoreWebView2DownloadState.Completed)
                        BeginInvoke(new Action(() => Msg("✓ 已保存到 " + dest)));
                    else if (op.State == CoreWebView2DownloadState.Interrupted)
                        BeginInvoke(new Action(() =>
                            Msg("✗ 导出下载中断：" + op.InterruptReason)));
                };
            }
            catch { }
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
                Msg("关窗口只是最小化到托盘，后台继续下载；要全停请用「退出」");
            }
        }

        private void Msg(string msg)
        {
            if (InvokeRequired) { BeginInvoke(new Action(() => Msg(msg))); return; }
            _msg.Text = "[" + DateTime.Now.ToString("HH:mm:ss") + "] " + msg;
        }

        private void RefreshState()
        {
            var st = Api.Get("/api/state", 1500);
            _running = st != null;
            _svcLabel.Text = "服务：" + (_running
                ? (Api.Truthy(st, "engine") ? "运行中（引擎就绪）" : "运行中（引擎未就绪）")
                : "已停止");
            _btnStart.Enabled = !_running;
            _btnStop.Enabled = _running;
            _btnAssoc.Enabled = _running;
            _btnAuto.Enabled = _running;
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
            _btnAssoc.Checked = Api.Truthy(ws, "magnet") && Api.Truthy(ws, "torrent");
            _btnAuto.Checked = Api.Truthy(ws, "autostart");
            _btnAssoc.Text = "关联：" + (_btnAssoc.Checked ? "开" : "关");
            _btnAuto.Text = "开机自启：" + (_btnAuto.Checked ? "开" : "关");
            _syncing = false;
            // Migration: a v1.3.0 association points at pythonw +
            // tofetch-open.py. Rewrite it once to this exe so protocol
            // prompts say "ToFetch" and the old helper can retire.
            if (!_migrated && Api.Str(ws, "opener") == "py" && Api.Truthy(ws, "magnet"))
            {
                _migrated = true;
                _ = Toggle("assoc");
            }
            var hints = new List<string>();
            var ucm = Api.Str(ws, "userchoice_magnet");
            if (ucm.Length > 0 && !ucm.ToLowerInvariant().StartsWith("tofetch"))
                hints.Add("MAGNET 被 " + ucm + " 在系统默认应用里占用，去 设置>应用>默认应用 改选 ToFetch");
            var uct = Api.Str(ws, "userchoice_torrent");
            if (uct.Length > 0 && !uct.ToLowerInvariant().StartsWith("tofetch"))
                hints.Add(".torrent 被 " + uct + " 在系统默认应用里占用，去 设置>应用>默认应用 改选 ToFetch");
            var hint = hints.Count > 0 ? "提醒：" + string.Join("；", hints) : "";
            if (hint.Length > 0 && hint != _lastHint) Msg(hint);
            _lastHint = hint;
        }

        private async Task Toggle(string kind)
        {
            bool on;
            if (kind == "assoc") { _btnAssoc.Checked = !_btnAssoc.Checked; on = _btnAssoc.Checked; }
            else { _btnAuto.Checked = !_btnAuto.Checked; on = _btnAuto.Checked; }
            var path = kind == "assoc" ? "/api/win/assoc" : "/api/win/autostart";
            var ws = await Task.Run(() => Api.Post(path, new Dictionary<string, object> { { "on", on } }, 10000));
            if (ws == null)
            {
                Msg("✗ " + (kind == "assoc" ? "关联" : "自启") + "设置失败：连不上服务");
                RefreshState();
                return;
            }
            ApplyWinState(ws);
            bool ok = Api.Truthy(ws, "ok");
            var what = kind == "assoc" ? "关联" : "开机自启";
            Msg(ok ? "✓ " + what + (on ? "已打开（已回读确认）" : "已关闭（已回读确认）")
                   : "✗ " + what + "没有完全生效：" + Api.Str(ws, "error") + "（开关已按实际状态回读）");
        }

        private async Task PickDownloadDir()
        {
            if (!await Task.Run(() => Backend.EnsureStarted()))
            {
                Msg("✗ 服务没在运行，改不了下载目录");
                return;
            }
            var st = await Task.Run(() => Api.Get("/api/state", 3000));
            var cur = st == null ? "" : Api.Str(st, "download_dir");
            // The modern system dialog, on the UI thread (modal COM
            // dialog; blocks like ShowDialog did - that is fine).
            var chosen = FolderDialog.Pick(Handle, cur,
                "选择下载目录（只影响之后新建的任务）");
            if (chosen == null) return;
            var body = new Dictionary<string, object>
            {
                { "global", new Dictionary<string, object> { { "dir", chosen } } },
            };
            var r = await Task.Run(() => Api.Post("/api/settings", body, 10000));
            if (r == null)
            {
                Msg("✗ 下载目录没改成：连不上服务");
                return;
            }
            var st2 = await Task.Run(() => Api.Get("/api/state", 3000));
            var now = st2 == null ? "" : Api.Str(st2, "download_dir");
            if (now.Length > 0 && string.Equals(now.TrimEnd('\\'), chosen.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                Msg("✓ 下载目录已改为 " + now + "（已回读确认）");
            else
                Msg("✗ 下载目录没有生效，现在仍是 " + (now.Length > 0 ? now : "未知"));
            RefreshState();
        }

        private void CleanupUpdateBackup()
        {
            // After a one-click update the relay leaves the previous
            // install in a sibling ToFetch-backup-<ver> folder. Once
            // the backend reports this shell's own version, the new
            // build is proven good and the backup can go.
            try
            {
                var parent = Directory.GetParent(Cfg.Home);
                if (parent == null) return;
                var st = Api.Get("/api/state", 2000);
                if (st == null || Api.Str(st, "version") != Cfg.Version)
                    return;
                foreach (var dir in Directory.GetDirectories(
                             parent.FullName, "ToFetch-backup-*"))
                {
                    try
                    {
                        Directory.Delete(dir, true);
                        Msg("✓ 更新完成，旧版备份已清理");
                    }
                    catch { }
                }
            }
            catch { }
        }

        private async Task CheckUpdate()
        {
            if (_updateBusy) return;
            if (_pendingUpdate != null)
            {
                await ApplyPendingUpdate();
                return;
            }
            _updateBusy = true;
            _btnUpdate.Enabled = false;
            Msg("正在检查更新…");
            string error = null;
            ReleaseInfo rel = null;
            try
            {
                rel = await Task.Run(() => Updater.FetchLatest(out error));
            }
            catch { }
            _updateBusy = false;
            _btnUpdate.Enabled = true;
            if (rel == null)
            {
                Msg("✗ 检查更新失败：" + (error ?? "连不上 GitHub"));
                return;
            }
            if (Updater.CompareVersions(rel.Version, Cfg.Version) <= 0)
            {
                Msg("✓ 已是最新版本（v" + Cfg.Version + "）");
                return;
            }
            _pendingUpdate = rel;
            _btnUpdate.Text = "更新到 v" + rel.Version;
            Msg("发现新版 v" + rel.Version + "（约 "
                + (rel.AssetSize / 1048576.0).ToString("0.0")
                + " MB）。再点一次「更新到 v" + rel.Version
                + "」就下载并更新：更新会自动重启，下载目录、设置和任务都保留。");
        }

        private async Task ApplyPendingUpdate()
        {
            var rel = _pendingUpdate;
            if (rel == null || _updateBusy) return;
            _updateBusy = true;
            _btnUpdate.Enabled = false;
            try
            {
                var dir = Path.Combine(Path.GetTempPath(),
                    "ToFetch-update", "v" + rel.Version);
                Directory.CreateDirectory(dir);
                var zip = Path.Combine(dir, Cfg.AssetName);
                Msg("正在下载 v" + rel.Version + "…");
                int lastLog = -1;
                await Updater.Download(rel.AssetUrl, zip, pct =>
                {
                    if (pct % 25 == 0 && pct != lastLog)
                    {
                        lastLog = pct;
                        Msg("下载中 " + pct + "%");
                    }
                });
                Msg("下载完成，正在校验…");
                string error = null;
                bool verified = await Task.Run(
                    () => Updater.VerifyDownloaded(zip, rel, out error));
                if (!verified)
                {
                    Msg("✗ " + error + "，已放弃更新，旧版照常运行");
                    return;
                }
                Msg("✓ 校验通过，正在应用更新（会重启）…");
                // Stop the service first so nothing holds files, then
                // hand over to a temp copy of this exe: a running
                // shell cannot overwrite its own binary.
                await Task.Run(() =>
                {
                    Api.Post("/api/exit", new Dictionary<string, object>
                        { { "mode", "stop" } }, 5000);
                    for (int i = 0; i < 20 && Backend.IsUp(); i++)
                        Thread.Sleep(500);
                });
                var relay = Path.Combine(dir, "ToFetch-relay.exe");
                File.Copy(Application.ExecutablePath, relay, true);
                Process.Start(new ProcessStartInfo
                {
                    FileName = relay,
                    Arguments = "--apply-update \"" + zip + "\" "
                        + Process.GetCurrentProcess().Id
                        + " \"" + Cfg.Home + "\"",
                    UseShellExecute = false,
                    WindowStyle = ProcessWindowStyle.Hidden,
                });
                _reallyExit = true;
                _tray.Visible = false;
                Application.Exit();
            }
            catch (Exception ex)
            {
                Msg("✗ 更新失败：" + ex.Message + "（旧版照常运行）");
            }
            finally
            {
                _updateBusy = false;
                _btnUpdate.Enabled = true;
                _pendingUpdate = null;
                _btnUpdate.Text = "检查更新";
            }
        }

        // Two-step confirms on the buttons themselves (a modal dialog
        // can be dismissed by the very click that opened it - field
        // lesson from the standard flavor). First click arms and
        // explains in the status bar; a second click executes.
        private void DisarmConfirms()
        {
            _armTimer.Stop();
            _stopArmed = false;
            _exitArmed = false;
            _btnStop.Text = "停止服务";
            _btnExit.Text = "退出";
        }

        private void ArmConfirm(bool stop)
        {
            if (stop) { _stopArmed = true; _btnStop.Text = "确定停止？"; }
            else { _exitArmed = true; _btnExit.Text = "确定退出？"; }
            _armTimer.Stop();
            _armTimer.Start();
        }

        private void StopService()
        {
            if (!_stopArmed)
            {
                ArmConfirm(true);
                Msg("提醒：停止服务后，所有下载都会停摆，重新打开 ToFetch 才恢复。再点一次「确定停止？」执行");
                return;
            }
            DisarmConfirms();
            Api.Post("/api/exit", new Dictionary<string, object> { { "mode", "stop" } }, 5000);
            _userStopped = true;
            Msg("✓ 服务已停止（关联还在，点磁力仍可唤起）");
            RefreshState();
        }

        private void FullExit()
        {
            if (!_exitArmed)
            {
                ArmConfirm(false);
                Msg("提醒：退出将停止服务、注销磁力与 .torrent 关联、取消开机自启，全部还给系统。再点一次「确定退出？」执行");
                return;
            }
            FullExitNow();
        }

        private void FullExitFromTray()
        {
            ShowPanel();
            if (MessageBox.Show(this,
                    "退出 ToFetch 将：停止服务、注销磁力与 .torrent 关联、取消开机自启。\n\n退出后无法用 ToFetch 下载，点磁力也不会再唤起 ToFetch；要再用，重新打开 ToFetch、并在工具栏里重新打开关联即可。\n\n确定退出？",
                    "退出 ToFetch", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK)
                return;
            FullExitNow();
        }

        private async void FullExitNow()
        {
            DisarmConfirms();
            if (!Backend.IsUp()) await Task.Run(() => Backend.EnsureStarted());
            Api.Post("/api/exit", new Dictionary<string, object> { { "mode", "full" } }, 8000);
            Msg("✓ 已退出，关联与自启已还给系统");
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
            // One-shot helper modes first: they must never reach the
            // window mutex, the tray or the opener parsing below.
            if (args.Length >= 4 && args[0] == "--apply-update")
            {
                int pid;
                int.TryParse(args[2], out pid);
                UpdateRelay.Run(args[1], pid, args[3]);
                return;
            }
            if (args.Length >= 2 && args[0] == "--pick-folder")
            {
                Application.EnableVisualStyles();
                Application.SetCompatibleTextRenderingDefault(false);
                PickFolderMode.Run(args[1]);
                return;
            }
            string target = null;
            bool background = false;
            foreach (var a in args)
            {
                if (a == "--background") background = true;
                else if (target == null) target = a;
            }
            if (target != null)
            {
                // Submit in the background; the window opening below
                // is the "open the UI" half of the opener contract.
                var t = target;
                var th = new Thread(() => Opener.Submit(t));
                th.IsBackground = true;
                th.Start();
            }

            bool first;
            using (var mutex = new Mutex(true, "ToFetchWebViewPanelMutex", out first))
            {
                if (!first)
                {
                    // A window is already running: ask it to come forward.
                    try
                    {
                        using (var ev = EventWaitHandle.OpenExisting("ToFetchWebViewPanelShow"))
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
                    using (var ev = new EventWaitHandle(false, EventResetMode.AutoReset, "ToFetchWebViewPanelShow"))
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
