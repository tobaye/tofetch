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
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class MainActivity : Activity() {
    private lateinit var web: WebView
    private val handler = Handler(Looper.getMainLooper())

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
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
        web = WebView(this).apply {
            settings.javaScriptEnabled = true
            settings.domStorageEnabled = true
        }
        root.addView(bar)
        root.addView(web, LinearLayout.LayoutParams(-1, -1))
        setContentView(root)

        startForegroundService(Intent(this, ToFetchService::class.java))
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
                    ok -> web.loadUrl("http://127.0.0.1:8077/")
                    tries < 60 -> handler.postDelayed({ waitForServer(tries + 1) }, 500)
                    else -> web.loadData(
                        "<h3>ToFetch 服务启动失败，请退出应用后重试</h3>",
                        "text/html", "utf-8"
                    )
                }
            }
        }
    }
}
