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
import java.io.File
import kotlin.concurrent.thread

class ToFetchService : Service() {
    private var wakeLock: PowerManager.WakeLock? = null

    override fun onBind(intent: Intent?): IBinder? = null

    override fun onCreate() {
        super.onCreate()
        Diag.installCrashLogger(this)
        Diag.log(this, "service onCreate")
        try {
            val nm = getSystemService(NotificationManager::class.java)
            nm.createNotificationChannel(
                NotificationChannel("tofetch", "ToFetch", NotificationManager.IMPORTANCE_LOW)
            )
            val openIntent = PendingIntent.getActivity(
                this, 0, Intent(this, MainActivity::class.java),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val stopIntent = PendingIntent.getService(
                this, 1, Intent(this, ToFetchService::class.java).setAction(ACTION_STOP),
                PendingIntent.FLAG_UPDATE_CURRENT or PendingIntent.FLAG_IMMUTABLE
            )
            val notif = Notification.Builder(this, "tofetch")
                .setContentTitle("ToFetch 正在运行")
                .setContentText("点按打开应用；停止请按右侧按钮")
                .setSmallIcon(android.R.drawable.stat_sys_download)
                .setContentIntent(openIntent)
                .addAction(0, "停止服务", stopIntent)
                .build()
            if (Build.VERSION.SDK_INT >= 34) {
                startForeground(1, notif, ServiceInfo.FOREGROUND_SERVICE_TYPE_DATA_SYNC)
            } else {
                startForeground(1, notif)
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
    }

    override fun onStartCommand(intent: Intent?, flags: Int, startId: Int): Int {
        if (intent?.action == ACTION_STOP) {
            Diag.log(this, "stop requested from notification")
            stopForeground(STOP_FOREGROUND_REMOVE)
            stopSelf()
        }
        return START_NOT_STICKY
    }

    override fun onDestroy() {
        wakeLock?.run { if (isHeld) release() }
        android.os.Process.killProcess(android.os.Process.myPid())
        super.onDestroy()
    }

    companion object {
        const val ACTION_STOP = "eu.org.puke.tofetch.STOP"
    }
}
