package eu.org.puke.tofetch

import android.content.Context
import android.os.Build
import java.io.File
import java.io.PrintWriter
import java.io.StringWriter
import java.text.SimpleDateFormat
import java.util.Date
import java.util.Locale

/** Startup diagnostics: appends milestone + crash lines to startup.log in the
 * app's external files dir (Android/data/eu.org.puke.tofetch/files/), where
 * the user can read it with any file manager, and MainActivity shows its tail
 * on screen. Exists because a sideloaded app gives us no logcat access. */
object Diag {
    @Volatile private var logFile: File? = null
    @Volatile private var crashLoggerInstalled = false

    @Synchronized
    fun init(ctx: Context) {
        if (logFile != null) return
        val base = ctx.getExternalFilesDir(null) ?: ctx.filesDir
        val f = File(base, "startup.log")
        try {
            if (f.exists() && f.length() > 200_000) {
                // Cap the log: keep only the most recent ~100KB.
                f.writeText(f.readText().takeLast(100_000))
            }
        } catch (e: Exception) {
        }
        logFile = f
    }

    @Synchronized
    fun log(ctx: Context, msg: String) {
        init(ctx)
        try {
            val ts = SimpleDateFormat("HH:mm:ss.SSS", Locale.US).format(Date())
            logFile?.appendText("$ts $msg\n")
        } catch (e: Exception) {
        }
    }

    fun logError(ctx: Context, where: String, t: Throwable) {
        val sw = StringWriter()
        t.printStackTrace(PrintWriter(sw))
        log(ctx, "ERROR in $where:\n$sw")
    }

    fun read(ctx: Context): String {
        init(ctx)
        return try {
            logFile?.readText() ?: ""
        } catch (e: Exception) {
            ""
        }
    }

    fun deviceInfo(): String =
        "model=${Build.MODEL} brand=${Build.BRAND} sdk=${Build.VERSION.SDK_INT} " +
            "release=${Build.VERSION.RELEASE} abis=${Build.SUPPORTED_ABIS.joinToString()}"

    @Synchronized
    fun installCrashLogger(ctx: Context) {
        if (crashLoggerInstalled) return
        crashLoggerInstalled = true
        val appCtx = ctx.applicationContext
        val prev = Thread.getDefaultUncaughtExceptionHandler()
        Thread.setDefaultUncaughtExceptionHandler { thread, t ->
            try {
                logError(appCtx, "uncaught on thread ${thread.name}", t)
            } catch (e: Exception) {
            }
            prev?.uncaughtException(thread, t)
        }
    }
}
