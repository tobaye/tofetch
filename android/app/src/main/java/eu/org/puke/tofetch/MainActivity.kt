package eu.org.puke.tofetch

import android.app.Activity
import android.content.Intent
import android.net.Uri
import android.os.Bundle
import android.os.Handler
import android.os.Looper
import android.webkit.WebView
import android.widget.Button
import android.widget.LinearLayout
import android.widget.Toast
import java.io.File
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
        val root = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        val bar = LinearLayout(this)
        bar.addView(Button(this).apply {
            text = "在浏览器打开"
            setOnClickListener {
                startActivity(Intent(Intent.ACTION_VIEW, Uri.parse("http://127.0.0.1:8077/")))
            }
        })
        bar.addView(Button(this).apply {
            text = "停止服务"
            setOnClickListener {
                stopService(Intent(this@MainActivity, ToFetchService::class.java))
                finish()
            }
        })
        bar.addView(Button(this).apply {
            text = "导出日志"
            setOnClickListener { exportLogs() }
        })
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
        }
        root.addView(bar)
        root.addView(web, LinearLayout.LayoutParams(-1, -1))
        setContentView(root)

        try {
            startForegroundService(Intent(this, ToFetchService::class.java))
            Diag.log(this, "startForegroundService ok")
        } catch (t: Throwable) {
            Diag.logError(this, "startForegroundService", t)
        }
        waitForServer(0)
    }

    /** Merge the shell startup log and the server's own diagnostics into one
     * file in the download dir (fixed name, overwritten each time so exports
     * never pile up), ready to be picked up with any file manager. */
    private fun exportLogs() {
        Toast.makeText(this, "正在导出日志…", Toast.LENGTH_SHORT).show()
        thread {
            val sb = StringBuilder()
            sb.append("===== startup.log =====\n").append(Diag.read(this))
            sb.append("\n===== server diagnostics =====\n")
            try {
                val c = URL("http://127.0.0.1:8077/api/log").openConnection() as HttpURLConnection
                c.connectTimeout = 3000
                c.readTimeout = 3000
                sb.append(c.inputStream.bufferedReader().readText())
                c.disconnect()
            } catch (e: Exception) {
                sb.append("(服务未运行，未能读取服务日志：${e.message})")
            }
            val extBase = getExternalFilesDir(null) ?: filesDir
            val out = File(File(extBase, "ToFetch").apply { mkdirs() }, "tofetch-diagnostics.txt")
            val msg = try {
                out.writeText(sb.toString())
                "日志已导出到下载目录：tofetch-diagnostics.txt"
            } catch (e: Exception) {
                "导出失败：${e.message}"
            }
            handler.post { Toast.makeText(this, msg, Toast.LENGTH_LONG).show() }
        }
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
