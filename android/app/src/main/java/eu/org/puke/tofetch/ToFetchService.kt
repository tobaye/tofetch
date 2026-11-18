package eu.org.puke.tofetch

import android.app.Notification
import android.app.NotificationChannel
import android.app.NotificationManager
import android.app.PendingIntent
import android.app.Service
import android.content.Intent
import android.content.pm.ServiceInfo
import android.os.Build
import android.os.IBinder
import android.os.PowerManager
import com.chaquo.python.Python
import com.chaquo.python.android.AndroidPlatform
import org.json.JSONArray
import org.json.JSONObject
import java.io.File
import java.net.HttpURLConnection
import java.net.URL
import kotlin.concurrent.thread

class ToFetchService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null
    private lateinit var nm: NotificationManager
    private lateinit var openIntent: PendingIntent
    private lateinit var stopIntent: PendingIntent
    private lateinit var pauseIntent: PendingIntent

    @Volatile
    private var polling = false
    private var knownDone: Set<String> = emptySet()
    private var seededDone = false

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Diag.installCrashLogger(this)
        Diag.log(this, "service onCreate")
        try {
            nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel("tofetch", "ToFetch", NotificationManager.IMPORTANCE_LOW)
            )
            nm.createNotificationChannel(
                NotificationChannel("tofetch-done", "下载完成", NotificationManager.IMPORTANCE_DEFAULT)
            )
            openIntent = PendingIntent.getActivity(
                this, 0, Intent(this, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            stopIntent = PendingIntent.getService(
                this, 1, Intent(this, ToFetchService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            pauseIntent = PendingIntent.getService(
                this, 2, Intent(this, ToFetchService::class.java).setAction(ACTION_PAUSE_ALL),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(1, buildStatusNotification("正在运行"),
                    ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            } else {
                startForeground(1, buildStatusNotification("正在运行"))
            }
            Diag.log(this, "startForeground ok")
            val pm = getSystemService(PowerManager::class.java)
            wakeLock = pm.newWakeLock(PowerManager.PARTIAL_WAKE_LOCK, "ToFetch:dl").apply { acquire() }
            Diag.log(this, "wakeLock acquired")
        } catch (t: Throwable) {
            Diag.logError(this, "service onCreate", t)
            throw t
        }

        thread(name = "tofetch-server") {
            try {
                Diag.log(this, "server thread: python starting")
                if (!Python.isStarted()) Python.start(AndroidPlatform(this))
                Diag.log(this, "server thread: python started")
                val py = Python.getInstance()
                val extBase = getExternalFilesDir(null) ?: filesDir
                val downloadDir = File(extBase, "ToFetch").apply { mkdirs() }
                val dataDir = File(filesDir, "data").apply { mkdirs() }
                val aria2 = File(applicationInfo.nativeLibraryDir, "libaria2c.so").absolutePath
                Diag.log(this, "server thread: launching server.py, aria2=$aria2")
                py.getModule("launcher").callAttr(
                    "start",
                    filesDir.absolutePath,
                    dataDir.absolutePath,
                    downloadDir.absolutePath,
                    aria2,
                    File(extBase, "startup.log").absolutePath
                )
                Diag.log(this, "server thread: launcher returned (server exited)")
            } catch (t: Throwable) {
                Diag.logError(this, "server thread", t)
            }
        }
        startStatusPoller()
    }

    private fun buildStatusNotification(text: String): Notification =
        Notification.Builder(this, "tofetch")
            .setContentTitle("ToFetch 正在运行")
            .setContentText(text)
            .setSmallIcon(android.R.drawable.stat_sys_download)
            .setContentIntent(openIntent)
            .addAction(0, "全部暂停", pauseIntent)
            .addAction(0, "停止服务", stopIntent)
            .build()

    // Poll the local API every few seconds: keep the foreground
    // notification's text at the live total speed / active count, and
    // post a separate notification when a task newly completes.
    private fun startStatusPoller() {
        polling = true
        thread(name = "tofetch-status") {
            while (polling) {
                try {
                    val stat = rpc("getGlobalStat", JSONArray()) as? JSONObject
                    if (stat != null) {
                        val down = stat.optLong("downloadSpeed")
                        val na = stat.optInt("numActive")
                        val nw = stat.optInt("numWaiting")
                        val text = if (na > 0) {
                            "↓ " + fmtSpeed(down) + " · 进行中 " + na +
                                (if (nw > 0) " · 等待 " + nw else "")
                        } else {
                            "运行中 · 无活动任务"
                        }
                        nm.notify(1, buildStatusNotification(text))
                        checkCompletions()
                    }
                } catch (e: Exception) {
                }
                try {
                    Thread.sleep(3000)
                } catch (e: InterruptedException) {
                    break
                }
            }
        }
    }

    private fun checkCompletions() {
        val keys = JSONArray().put("gid").put("status").put("files").put("bittorrent")
        val params = JSONArray().put(0).put(50).put(keys)
        val stopped = rpc("tellStopped", params) as? JSONArray ?: return
        val nowDone = mutableSetOf<String>()
        for (i in 0 until stopped.length()) {
            val t = stopped.optJSONObject(i) ?: continue
            if (t.optString("status") != "complete") continue
            val gid = t.optString("gid")
            if (gid.isEmpty()) continue
            nowDone.add(gid)
            if (seededDone && gid !in knownDone) notifyDone(t)
        }
        knownDone = nowDone
        seededDone = true
    }

    private fun notifyDone(t: JSONObject) {
        val name = taskName(t)
        val notif = Notification.Builder(this, "tofetch-done")
            .setContentTitle("下载完成")
            .setContentText(name)
            .setSmallIcon(android.R.drawable.stat_sys_download_done)
            .setContentIntent(openIntent)
            .setAutoCancel(true)
            .build()
        nm.notify(t.optString("gid").hashCode(), notif)
    }

    private fun taskName(t: JSONObject): String {
        val info = t.optJSONObject("bittorrent")?.optJSONObject("info")
        if (info != null && info.optString("name").isNotEmpty()) return info.optString("name")
        val f = t.optJSONArray("files")?.optJSONObject(0)
        val path = f?.optString("path") ?: ""
        if (path.isNotEmpty()) return path.substringAfterLast('/')
        val uri = f?.optJSONArray("uris")?.optJSONObject(0)?.optString("uri") ?: ""
        return uri.ifEmpty { "文件" }
    }

    private fun rpc(method: String, params: JSONArray): Any? {
        val body = JSONObject().put("method", method).put("params", params).toString()
        val c = URL("http://127.0.0.1:8077/api/rpc").openConnection() as HttpURLConnection
        c.requestMethod = "POST"
        c.connectTimeout = 2000
        c.readTimeout = 2000
        c.doOutput = true
        c.setRequestProperty("Content-Type", "application/json")
        c.outputStream.use { it.write(body.toByteArray()) }
        val text = c.inputStream.bufferedReader().readText()
        c.disconnect()
        return JSONObject(text).opt("result")
    }

    private fun fmtSpeed(n: Long): String {
        var v = n.toDouble()
        val units = arrayOf("B/s", "KB/s", "MB/s", "GB/s")
        var i = 0
        while (v >= 1024 && i < units.size - 1) {
            v /= 1024
            i++
        }
        return (if (v >= 100 || i == 0) v.toInt().toString() else String.format("%.1f", v)) + " " + units[i]
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        when (intent?.action) {
            ACTION_STOP -> {
                Diag.log(this, "stop requested from notification")
                stopForeground(STOP_FOREGROUND_REMOVE)
                stopSelf()
            }
            ACTION_PAUSE_ALL -> {
                Diag.log(this, "pause-all requested from notification")
                thread {
                    try {
                        rpc("pauseAll", JSONArray())
                    } catch (e: Exception) {
                    }
                }
            }
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        polling = false
        wakeLock?.run { if (isHeld) release() }
        android.os.Process.killProcess(android.os.Process.myPid())
        super.onDestroy()
    }

    companion object {
        const val ACTION_STOP = "eu.org.puke.tofetch.STOP"
        const val ACTION_PAUSE_ALL = "eu.org.puke.tofetch.PAUSE_ALL"
    }
}
