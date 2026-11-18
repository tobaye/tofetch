package eu.org.puke.tofetch

import android.app.Activity
import android.app.DownloadManager
import android.content.ClipboardManager
import android.content.Intent
import android.net.Uri
import android.os.Build
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.webkit.URLUtil
import android.webkit.WebView
import android.webkit.WebViewClient
import android.widget.Toast
import org.json.JSONObject
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())
    private var pageLoaded = false
    private var pendingShare: String? = null

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Diag.installCrashLogger(this)
        Diag.log(this, "=== activity onCreate; ${Diag.deviceInfo()} ===")
        captureShare(intent)
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
            webViewClient = object : WebViewClient() {
                override fun onPageFinished(view: WebView?, url: String?) {
                    pageLoaded = true
                    deliverShare()
                }
            }
            // Downloads started from the page (retrieve a finished file,
            // export diagnostics) go through the system DownloadManager
            // into the public Downloads folder, like a normal app. Stopping
            // the service lives on the foreground-service notification.
            setDownloadListener { url, _, contentDisposition, mimeType, _ ->
                val name = URLUtil.guessFileName(url, contentDisposition, mimeType)
                try {
                    val req = DownloadManager.Request(Uri.parse(url))
                        .setTitle(name)
                        .setMimeType(mimeType)
                        .setNotificationVisibility(DownloadManager.Request.VISIBILITY_VISIBLE_NOTIFY_COMPLETED)
                        .setDestinationInExternalPublicDir(Environment.DIRECTORY_DOWNLOADS, name)
                    getSystemService(DownloadManager::class.java).enqueue(req)
                    Toast.makeText(
                        this@MainActivity,
                        "已开始下载：$name（系统下载目录）",
                        Toast.LENGTH_LONG
                    ).show()
                } catch (e: Exception) {
                    Toast.makeText(this@MainActivity, "下载失败：${e.message}", Toast.LENGTH_LONG).show()
                }
            }
        }
        setContentView(web)

        if (Build.VERSION.SDK_INT >= 33) {
            try {
                requestPermissions(arrayOf("android.permission.POST_NOTIFICATIONS"), 1)
            } catch (e: Exception) {
            }
        }
        try {
            startForegroundService(Intent(this, ToFetchService::class.java))
            Diag.log(this, "startForegroundService ok")
        } catch (t: Throwable) {
            Diag.logError(this, "startForegroundService", t)
        }
        waitForServer(0)
    }

    override fun onNewIntent(intent: Intent) {
        super.onNewIntent(intent)
        setIntent(intent)
        captureShare(intent)
        deliverShare()
    }

    // System share (ACTION_SEND text) and magnet:/thunder: VIEW intents
    // land here; the text goes to the page's __tofetchShare hook, which
    // opens the add dialog and runs the usual link extraction.
    private fun captureShare(intent: Intent?) {
        if (intent?.getBooleanExtra(EXTRA_ADD_CLIPBOARD, false) == true) {
            // Notification "添加下载" action: seed the add dialog with
            // whatever is on the clipboard (or open it empty).
            try {
                val cm = getSystemService(ClipboardManager::class.java)
                val clip = cm.primaryClip
                val text = if (clip != null && clip.itemCount > 0)
                    clip.getItemAt(0)?.coerceToText(this)?.toString() else null
                pendingShare = if (text.isNullOrBlank()) "" else text
                Diag.log(this, "clipboard-add captured (${pendingShare?.length ?: 0} chars)")
            } catch (e: Exception) {
                Diag.logError(this, "clipboard read", e)
                pendingShare = ""
            }
            return
        }
        val text = when (intent?.action) {
            Intent.ACTION_SEND -> intent.getStringExtra(Intent.EXTRA_TEXT)
            Intent.ACTION_VIEW -> intent.dataString
            else -> null
        }
        if (!text.isNullOrBlank()) {
            pendingShare = text
            Diag.log(this, "share captured (${text.length} chars)")
        }
    }

    private fun deliverShare() {
        val text = pendingShare ?: return
        if (!pageLoaded) return
        pendingShare = null
        web.evaluateJavascript(
            "window.__tofetchShare && window.__tofetchShare(" + JSONObject.quote(text) + ")",
            null
        )
        Toast.makeText(this, "已填入分享内容", Toast.LENGTH_SHORT).show()
    }

    companion object {
        const val EXTRA_ADD_CLIPBOARD = "add_clipboard"
    }

    private fun waitForServer(tries: Int) {
        thread {
            var ok = false
            try {
                val c = URL("http://127.0.0.1:8077/").openConnection() as HttpURLConnection
                c.connectTimeout = 1500
                c.readTimeout = 1500
                ok = c.responseCode in 200..499
                c.disconnect()
            } catch (e: Exception) {
            }
            handler.post {
                when {
                    ok -> {
                        Diag.log(this, "server answered, loading UI")
                        web.loadUrl("http://127.0.0.1:8077/")
                    }
                    tries < 60 -> handler.postDelayed({ waitForServer(tries + 1) }, 500)
                    else -> {
                        Diag.log(this, "server never answered after 30s")
                        web.loadData(
                            "<h3>ToFetch 服务启动失败，请退出应用后重试</h3>",
                            "text/html", "utf-8"
                        )
                    }
                }
            }
        }
    }
}
