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
// One-shot helper modes (no panel, no tray, exit when done):
//   ToFetch.exe --pick-folder <token>
//       Show the system folder dialog for the web UI's "browse"
//       button and post the choice back to the backend.
//   ToFetch.exe --apply-update <zip> <panel-pid> <install-dir>
//       Relay half of the one-click update: runs from a temp copy so
//       the panel's own exe can be replaced once the panel exits.
//
// .NET Framework 4.8 WinForms only - no external packages, so the exe
// stays tiny and runs on a stock Windows 10/11 install.
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
using Microsoft.Win32;

namespace ToFetchPanel
{
    internal static class Cfg
    {
        public const string Version = "1.5.0";
        // This flavor's own release asset. The one-click updater only
        // ever downloads this name - flavors never cross-update.
        public const string AssetName = "ToFetch-Windows.zip";
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

    internal sealed class BrowserChoice
    {
        public string Name = "";
        public string Exe = "";   // "" = system default browser
        public override string ToString() { return Name; }
    }

    internal static class Browsers
    {
        // Which browser opens the download UI. Chosen in the panel,
        // stored as one named value under HKCU\Software\ToFetch.
        private const string RegKey = @"Software\ToFetch";
        private const string RegVal = "Browser";

        public static string SavedExe()
        {
            try
            {
                using (var k = Registry.CurrentUser.OpenSubKey(RegKey))
                {
                    var v = k == null ? null : k.GetValue(RegVal) as string;
                    return v ?? "";
                }
            }
            catch { return ""; }
        }

        public static void Save(string exe)
        {
            try
            {
                using (var k = Registry.CurrentUser.CreateSubKey(RegKey))
                {
                    if (k != null) k.SetValue(RegVal, exe ?? "", RegistryValueKind.String);
                }
            }
            catch { }
        }

        public static List<BrowserChoice> Installed()
        {
            var list = new List<BrowserChoice>();
            var seen = new HashSet<string>(StringComparer.OrdinalIgnoreCase);
            foreach (var root in new[] { Registry.LocalMachine, Registry.CurrentUser })
            {
                try
                {
                    using (var clients = root.OpenSubKey(@"Software\Clients\StartMenuInternet"))
                    {
                        if (clients == null) continue;
                        foreach (var sub in clients.GetSubKeyNames())
                        {
                            try
                            {
                                using (var ck = clients.OpenSubKey(sub))
                                {
                                    if (ck == null) continue;
                                    var disp = ck.GetValue("") as string;
                                    string exe = null;
                                    using (var cmd = ck.OpenSubKey(@"shell\open\command"))
                                    {
                                        var c = cmd == null ? null : cmd.GetValue("") as string;
                                        exe = ExtractExe(c);
                                    }
                                    if (exe != null && File.Exists(exe) && seen.Add(exe))
                                        list.Add(new BrowserChoice { Name = string.IsNullOrEmpty(disp) ? sub : disp, Exe = exe });
                                }
                            }
                            catch { }
                        }
                    }
                }
                catch { }
            }
            return list;
        }

        private static string ExtractExe(string cmd)
        {
            if (string.IsNullOrWhiteSpace(cmd)) return null;
            cmd = cmd.Trim();
            if (cmd.StartsWith("\""))
            {
                int j = cmd.IndexOf('"', 1);
                return j > 1 ? cmd.Substring(1, j - 1) : null;
            }
            int sp = cmd.IndexOf(' ');
            return sp > 0 ? cmd.Substring(0, sp) : cmd;
        }

        public static void Open(string url)
        {
            var exe = SavedExe();
            if (!string.IsNullOrEmpty(exe) && File.Exists(exe))
            {
                try
                {
                    Process.Start(new ProcessStartInfo { FileName = exe, Arguments = "\"" + url + "\"", UseShellExecute = false });
                    return;
                }
                catch { }
            }
            try { Process.Start(url); } catch { }
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
            Browsers.Open(Cfg.Base + "/");
        }
    }

    internal static class FolderDialog
    {
        // The modern Explorer-style folder dialog (IFileDialog with
        // FOS_PICKFOLDERS) - the one Firefox/Chrome show for their
        // download folder. The old FolderBrowserDialog tree is not
        // acceptable (user, 2026-11-19). Hand-written COM interop,
        // no external packages.
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
        // Second half of the one-click update. The panel copies itself
        // to a temp dir and launches that copy with --apply-update;
        // running from temp, this process may replace the panel's own
        // exe once the panel has exited. The install's data/ folder
        // (settings, history, sessions) is never backed up, replaced
        // or restored - it simply stays where it is throughout.
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
            // Either way the (new or restored) panel comes back up.
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
        // back, exit - no panel, no tray, no service control.
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
        private readonly Button _btnUpdate = new Button();
        private readonly Button _btnExit = new Button();
        private ReleaseInfo _pendingUpdate;
        private bool _updateBusy;
        private readonly CheckBox _chkAssoc = new CheckBox();
        private readonly CheckBox _chkAuto = new CheckBox();
        private readonly ComboBox _cmbBrowser = new ComboBox();
        private bool _syncingBrowser;
        private readonly Button _btnDir = new Button();
        private readonly Label _ucHint = new Label();
        private readonly TextBox _log = new TextBox();
        private readonly NotifyIcon _tray = new NotifyIcon();
        private readonly System.Windows.Forms.Timer _timer = new System.Windows.Forms.Timer();
        private readonly bool _startHidden;
        private bool _running;
        private bool _userStopped;
        private bool _stopArmed;
        private bool _exitArmed;
        private readonly System.Windows.Forms.Timer _armTimer = new System.Windows.Forms.Timer();
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
            ClientSize = new Size(520, 605);
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
            _btnOpen.Size = new Size(140, 30);
            _btnOpen.Click += async (s, e) => { if (await Task.Run(() => Backend.EnsureStarted())) { _userStopped = false; Backend.OpenUi(); } };
            _btnStart.Text = "启动服务";
            _btnStart.Location = new Point(162, 164);
            _btnStart.Size = new Size(104, 30);
            _btnStart.Click += async (s, e) =>
            {
                _userStopped = false;
                Log(await Task.Run(() => Backend.EnsureStarted()) ? "✓ 服务已启动" : "✗ 服务启动失败，看诊断日志排查");
                RefreshState();
            };
            _btnStop.Text = "停止服务";
            _btnStop.Location = new Point(276, 164);
            _btnStop.Size = new Size(104, 30);
            _btnStop.Click += (s, e) => StopService();
            _btnUpdate.Text = "检查更新";
            _btnUpdate.Location = new Point(390, 164);
            _btnUpdate.Size = new Size(118, 30);
            _btnUpdate.Click += async (s, e) => await CheckUpdate();
            Controls.AddRange(new Control[] { _btnOpen, _btnStart, _btnStop, _btnUpdate });

            var gLocal = new GroupBox { Text = "本机集成（状态实时回读，改动立即生效）", Location = new Point(12, 206), Size = new Size(496, 166) };
            _chkAssoc.Text = "磁力链接与 .torrent 文件关联到 ToFetch";
            _chkAssoc.AutoSize = true;
            _chkAssoc.Location = new Point(14, 24);
            _chkAssoc.CheckedChanged += async (s, e) => { if (!_syncing) await Toggle("assoc", _chkAssoc.Checked); };
            _chkAuto.Text = "开机自启（后台静默启动，不弹窗）";
            _chkAuto.AutoSize = true;
            _chkAuto.Location = new Point(14, 48);
            _chkAuto.CheckedChanged += async (s, e) => { if (!_syncing) await Toggle("autostart", _chkAuto.Checked); };
            var browserLabel = new Label { Text = "打开界面用的浏览器：", AutoSize = true, Location = new Point(14, 76) };
            _cmbBrowser.DropDownStyle = ComboBoxStyle.DropDownList;
            _cmbBrowser.Location = new Point(150, 72);
            _cmbBrowser.Size = new Size(220, 24);
            _cmbBrowser.SelectedIndexChanged += (s, e) =>
            {
                if (_syncingBrowser) return;
                var bc = _cmbBrowser.SelectedItem as BrowserChoice;
                if (bc == null) return;
                Browsers.Save(bc.Exe);
                Log("打开界面浏览器：" + bc.Name);
            };
            var dirLabel = new Label { Text = "下载目录（改后只影响新任务）：", AutoSize = true, Location = new Point(14, 106) };
            _btnDir.Text = "选择…";
            _btnDir.Location = new Point(190, 101);
            _btnDir.Size = new Size(80, 26);
            _btnDir.Click += async (s, e) => await PickDownloadDir();
            _ucHint.ForeColor = Color.DarkGoldenrod;
            _ucHint.AutoSize = false;
            _ucHint.Location = new Point(14, 134);
            _ucHint.Size = new Size(468, 28);
            gLocal.Controls.AddRange(new Control[] { _chkAssoc, _chkAuto, browserLabel, _cmbBrowser, dirLabel, _btnDir, _ucHint });
            Controls.Add(gLocal);
            InitBrowserCombo();

            _btnExit.Text = "退出 ToFetch";
            _btnExit.Location = new Point(378, 382);
            _btnExit.Size = new Size(130, 30);
            _btnExit.Click += (s, e) => FullExit();
            Controls.Add(_btnExit);
            var exitHint = new Label
            {
                Text = "退出 = 停服务＋注销关联＋取消自启，全部还给系统。",
                ForeColor = Color.Gray,
                AutoSize = true,
                Location = new Point(12, 390),
            };
            Controls.Add(exitHint);

            var gLog = new GroupBox { Text = "记录", Location = new Point(12, 422), Size = new Size(496, 170) };
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
                Log("面板已打开，服务在后台运行");
                // Never block the UI thread on service startup: while the
                // backend is still coming up the panel must stay clickable.
                if (!_userStopped) await Task.Run(() => Backend.EnsureStarted());
                RefreshState();
                CleanupUpdateBackup();
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

        private async Task PickDownloadDir()
        {
            if (!await Task.Run(() => Backend.EnsureStarted()))
            {
                Log("✗ 服务没在运行，改不了下载目录");
                return;
            }
            var st = await Task.Run(() => Api.Get("/api/state", 3000));
            var cur = st == null ? "" : Api.Str(st, "download_dir");
            // The modern system dialog, on the UI thread (it is a
            // modal COM dialog; the old FolderBrowserDialog tree is
            // gone). It blocks like ShowDialog did - that is fine.
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
                Log("✗ 下载目录没改成：连不上服务");
                return;
            }
            var st2 = await Task.Run(() => Api.Get("/api/state", 3000));
            var now = st2 == null ? "" : Api.Str(st2, "download_dir");
            if (now.Length > 0 && string.Equals(now.TrimEnd('\\'), chosen.TrimEnd('\\'), StringComparison.OrdinalIgnoreCase))
                Log("✓ 下载目录已改为 " + now + "（已回读确认）");
            else
                Log("✗ 下载目录没有生效，现在仍是 " + (now.Length > 0 ? now : "未知"));
            RefreshState();
        }

        private void CleanupUpdateBackup()
        {
            // After a one-click update the relay leaves the previous
            // install in a sibling ToFetch-backup-<ver> folder. Once
            // the backend reports this panel's own version, the new
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
                        Log("✓ 更新完成，旧版备份已清理");
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
            Log("正在检查更新…");
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
                Log("✗ 检查更新失败：" + (error ?? "连不上 GitHub"));
                return;
            }
            if (Updater.CompareVersions(rel.Version, Cfg.Version) <= 0)
            {
                Log("✓ 已是最新版本（v" + Cfg.Version + "）");
                return;
            }
            _pendingUpdate = rel;
            _btnUpdate.Text = "更新到 v" + rel.Version;
            Log("发现新版 v" + rel.Version + "（约 "
                + (rel.AssetSize / 1048576.0).ToString("0.0")
                + " MB）。再点一次「更新到 v" + rel.Version
                + "」就下载并更新：更新会自动重启面板，下载目录、设置和任务都保留。");
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
                Log("正在下载 v" + rel.Version + "…");
                int lastLog = -1;
                await Updater.Download(rel.AssetUrl, zip, pct =>
                {
                    if (pct % 25 == 0 && pct != lastLog)
                    {
                        lastLog = pct;
                        Log("下载中 " + pct + "%");
                    }
                });
                Log("下载完成，正在校验…");
                string error = null;
                bool verified = await Task.Run(
                    () => Updater.VerifyDownloaded(zip, rel, out error));
                if (!verified)
                {
                    Log("✗ " + error + "，已放弃更新，旧版照常运行");
                    return;
                }
                Log("✓ 校验通过，正在应用更新（面板会重启）…");
                // Stop the service first so nothing holds files, then
                // hand over to a temp copy of this exe: a running
                // panel cannot overwrite its own binary.
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
                Log("✗ 更新失败：" + ex.Message + "（旧版照常运行）");
            }
            finally
            {
                _updateBusy = false;
                _btnUpdate.Enabled = true;
                _pendingUpdate = null;
                _btnUpdate.Text = "检查更新";
            }
        }

        private void InitBrowserCombo()
        {
            _syncingBrowser = true;
            _cmbBrowser.Items.Clear();
            _cmbBrowser.Items.Add(new BrowserChoice { Name = "系统默认", Exe = "" });
            foreach (var b in Browsers.Installed()) _cmbBrowser.Items.Add(b);
            var saved = Browsers.SavedExe();
            int sel = 0;
            for (int i = 0; i < _cmbBrowser.Items.Count; i++)
            {
                var bc = _cmbBrowser.Items[i] as BrowserChoice;
                if (bc != null && string.Equals(bc.Exe, saved, StringComparison.OrdinalIgnoreCase)) { sel = i; break; }
            }
            _cmbBrowser.SelectedIndex = sel;
            _syncingBrowser = false;
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

        // Two-step confirms on the buttons themselves: a modal dialog
        // can land its buttons exactly under the click that opened it
        // and be dismissed by that same click's release (on the
        // reference machine the stop dialog just "flashed" and never
        // acted). First click arms the button and explains in the log;
        // a second click within a few seconds executes.
        private void DisarmConfirms()
        {
            _armTimer.Stop();
            _stopArmed = false;
            _exitArmed = false;
            _btnStop.Text = "停止服务";
            _btnExit.Text = "退出 ToFetch";
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
                Log("提醒：停止服务后，所有下载都会停摆，重新打开 ToFetch 才恢复。再点一次「确定停止？」执行");
                return;
            }
            DisarmConfirms();
            Api.Post("/api/exit", new Dictionary<string, object> { { "mode", "stop" } }, 5000);
            _userStopped = true;
            Log("✓ 服务已停止");
            Log("提醒：停止后无法用 ToFetch 下载；关联还在，点磁力仍可唤起");
            RefreshState();
        }

        private void FullExit()
        {
            if (!_exitArmed)
            {
                ArmConfirm(false);
                Log("提醒：退出将停止服务、注销磁力与 .torrent 关联、取消开机自启，全部还给系统。再点一次「确定退出？」执行");
                return;
            }
            FullExitNow();
        }

        private void FullExitFromTray()
        {
            ShowPanel();
            if (MessageBox.Show(this,
                    "退出 ToFetch 将：停止服务、注销磁力与 .torrent 关联、取消开机自启。\n\n退出后无法用 ToFetch 下载，点磁力也不会再唤起 ToFetch；要再用，重新打开 ToFetch、并在面板里重新打开关联即可。\n\n确定退出？",
                    "退出 ToFetch", MessageBoxButtons.OKCancel, MessageBoxIcon.Warning) != DialogResult.OK)
                return;
            FullExitNow();
        }

        private async void FullExitNow()
        {
            DisarmConfirms();
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
            // One-shot helper modes first: they must never reach the
            // panel mutex, the tray or the opener parsing below.
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
