package eu.org.puke.tofetch

import android.app.Activity
import android.app.DownloadManager
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Environment
import android.os.Handler
import android.os.Looper
import android.webkit.URLUtil
import android.webkit.WebView
import android.widget.Toast
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        Diag.installCrashLogger(this)
        Diag.log(this, "=== activity onCreate; ${Diag.deviceInfo()} ===")
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
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

        try {
            startForegroundService(Intent(this, ToFetchService::class.java))
            Diag.log(this, "startForegroundService ok")
        } catch (t: Throwable) {
            Diag.logError(this, "startForegroundService", t)
        }
        waitForServer(0)
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
