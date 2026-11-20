package com.example.phonetablet

import android.content.Context
import android.content.pm.ActivityInfo
import android.graphics.Bitmap
import android.graphics.BitmapFactory
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.os.Bundle
import android.view.MotionEvent
import android.view.View
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.LinearLayout
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat
import java.io.DataInputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.nio.ByteBuffer
import java.util.concurrent.LinkedBlockingQueue

private const val DOWN: Byte = 0
private const val MOVE: Byte = 1
private const val UP: Byte = 2
private const val HOVER: Byte = 3
private const val LEAVE: Byte = 4
private const val RESIZE: Byte = 10

/**
 * One TCP connection, both directions:
 *  - sends 13-byte packets (type, x, y, pressure) to the Windows server
 *  - receives screen frames: [4-byte length][JPEG] and hands them to onFrame
 */
class Connection(
    private val host: String,
    private val port: Int,
    private val onConnected: () -> Unit,
    private val onFrame: (Bitmap) -> Unit,
    private val onClosed: (String) -> Unit,
) {
    private val queue = LinkedBlockingQueue<ByteArray>()
    @Volatile private var running = true
    @Volatile private var socket: Socket? = null

    fun start() {
        Thread {
            try {
                val s = Socket()
                s.tcpNoDelay = true
                s.connect(InetSocketAddress(host, port), 4000)
                socket = s
                onConnected()
                Thread { readLoop(s) }.apply { isDaemon = true }.start()
                writeLoop(s)
            } catch (e: Exception) {
                fail(e.message ?: "Connection failed")
            }
        }.apply { isDaemon = true }.start()
    }

    private fun writeLoop(s: Socket) {
        val out = s.getOutputStream().buffered()
        while (running) {
            val packet = queue.take()
            if (!running) break
            out.write(packet)
            if (queue.isEmpty()) out.flush()
        }
    }

    private fun readLoop(s: Socket) {
        try {
            val input = DataInputStream(s.getInputStream().buffered(1 shl 16))
            while (running) {
                val len = input.readInt()
                if (len <= 0 || len > 20_000_000) throw IOException("Bad frame size")
                val data = ByteArray(len)
                input.readFully(data)
                val bmp = BitmapFactory.decodeByteArray(data, 0, len) ?: continue
                onFrame(bmp)
            }
        } catch (e: Exception) {
            fail(e.message ?: "Server closed the connection")
        }
    }

    @Synchronized
    private fun fail(reason: String) {
        if (!running) return
        running = false
        try { socket?.close() } catch (_: Exception) {}
        queue.offer(ByteArray(0))
        onClosed(reason)
    }

    fun send(type: Byte, x: Float, y: Float, pressure: Float) {
        if (!running) return
        queue.offer(
            ByteBuffer.allocate(13).put(type).putFloat(x).putFloat(y).putFloat(pressure).array()
        )
    }

    fun close() {
        running = false
        try { socket?.close() } catch (_: Exception) {}
        queue.offer(ByteArray(0)) // wake the writer thread
    }
}

/**
 * Shows the laptop screen (letterboxed) and turns touch / stylus input into
 * pen packets, with coordinates relative to the displayed screen image.
 */
class TabletView(context: Context, private val conn: Connection) : View(context) {
    @Volatile private var frame: Bitmap? = null
    private val paint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val dst = RectF()
    private var activeId = -1

    init {
        setBackgroundColor(Color.BLACK)
    }

    fun setFrame(b: Bitmap) {
        frame = b
        postInvalidateOnAnimation()
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        // Tell the server how big the phone screen is so it can downscale frames.
        conn.send(RESIZE, w.toFloat(), h.toFloat(), 0f)
    }

    /** Fit the frame inside the view, centered, keeping its aspect ratio. */
    private fun updateRect(f: Bitmap) {
        val scale = minOf(width.toFloat() / f.width, height.toFloat() / f.height)
        val w = f.width * scale
        val h = f.height * scale
        val left = (width - w) / 2f
        val top = (height - h) / 2f
        dst.set(left, top, left + w, top + h)
    }

    override fun onDraw(canvas: Canvas) {
        val f = frame ?: return
        updateRect(f)
        canvas.drawBitmap(f, null, dst, paint)
    }

    private fun nx(v: Float) = ((v - dst.left) / dst.width()).coerceIn(0f, 1f)
    private fun ny(v: Float) = ((v - dst.top) / dst.height()).coerceIn(0f, 1f)

    private fun pressureOf(e: MotionEvent, idx: Int, raw: Float): Float =
        if (e.getToolType(idx) == MotionEvent.TOOL_TYPE_STYLUS) raw.coerceIn(0f, 1f) else 0.5f

    override fun onTouchEvent(e: MotionEvent): Boolean {
        val f = frame ?: return true // nothing to draw on yet
        updateRect(f)
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN -> {
                activeId = e.getPointerId(0)
                conn.send(DOWN, nx(e.getX(0)), ny(e.getY(0)), pressureOf(e, 0, e.getPressure(0)))
            }
            MotionEvent.ACTION_MOVE -> {
                val idx = e.findPointerIndex(activeId)
                if (idx < 0) return true
                for (h in 0 until e.historySize) {
                    conn.send(
                        MOVE,
                        nx(e.getHistoricalX(idx, h)),
                        ny(e.getHistoricalY(idx, h)),
                        pressureOf(e, idx, e.getHistoricalPressure(idx, h)),
                    )
                }
                conn.send(MOVE, nx(e.getX(idx)), ny(e.getY(idx)), pressureOf(e, idx, e.getPressure(idx)))
            }
            MotionEvent.ACTION_POINTER_UP -> {
                // Extra fingers (palm, etc.) are ignored unless it is the drawing pointer.
                if (e.getPointerId(e.actionIndex) == activeId) {
                    val i = e.actionIndex
                    conn.send(UP, nx(e.getX(i)), ny(e.getY(i)), 0f)
                    activeId = -1
                }
            }
            MotionEvent.ACTION_UP, MotionEvent.ACTION_CANCEL -> {
                if (activeId != -1) {
                    conn.send(UP, nx(e.getX(0)), ny(e.getY(0)), 0f)
                    activeId = -1
                }
            }
        }
        return true
    }

    // Stylus hover (devices that support it)
    override fun onHoverEvent(e: MotionEvent): Boolean {
        val f = frame ?: return true
        updateRect(f)
        when (e.actionMasked) {
            MotionEvent.ACTION_HOVER_ENTER, MotionEvent.ACTION_HOVER_MOVE ->
                conn.send(HOVER, nx(e.x), ny(e.y), 0f)
            MotionEvent.ACTION_HOVER_EXIT ->
                conn.send(LEAVE, nx(e.x), ny(e.y), 0f)
        }
        return true
    }
}

class MainActivity : AppCompatActivity() {
    private var conn: Connection? = null
    private var tabletShown = false
    @Volatile private var frameSink: ((Bitmap) -> Unit)? = null
    private lateinit var status: TextView

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        showForm()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (tabletShown) {
                    conn?.close()
                    conn = null
                    showForm()
                } else {
                    finish()
                }
            }
        })
    }

    private fun showForm() {
        tabletShown = false
        frameSink = null
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        setSystemBars(visible = true)
        val prefs = getSharedPreferences("tablet", MODE_PRIVATE)

        val pad = (24 * resources.displayMetrics.density).toInt()
        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(pad, pad, pad, pad)
        }
        val host = EditText(this).apply {
            hint = "Server IP  (USB: 127.0.0.1)"
            setText(prefs.getString("host", "127.0.0.1"))
            setSingleLine()
        }
        val port = EditText(this).apply {
            hint = "Port"
            setText(prefs.getString("port", "5555"))
            inputType = android.text.InputType.TYPE_CLASS_NUMBER
            setSingleLine()
        }
        val button = Button(this).apply { text = "Connect" }
        status = TextView(this).apply {
            text = "Enter the address shown by the Windows server."
            setPadding(0, pad / 2, 0, 0)
        }
        root.addView(host)
        root.addView(port)
        root.addView(button)
        root.addView(status)
        setContentView(root)

        button.setOnClickListener {
            val h = host.text.toString().trim()
            val p = port.text.toString().trim().toIntOrNull() ?: 5555
            prefs.edit().putString("host", h).putString("port", p.toString()).apply()
            status.text = "Connecting to $h:$p ..."
            button.isEnabled = false

            lateinit var c: Connection
            c = Connection(
                h, p,
                onConnected = { runOnUiThread { conn = c; showTablet(c) } },
                onFrame = { bmp -> frameSink?.invoke(bmp) },
                onClosed = { reason ->
                    runOnUiThread {
                        if (conn === c || conn == null) {
                            conn = null
                            showForm()
                            status.text = "Not connected: $reason"
                        }
                    }
                },
            )
            c.start()
        }
    }

    private fun showTablet(c: Connection) {
        tabletShown = true
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        setSystemBars(visible = false)
        val view = TabletView(this, c)
        frameSink = { bmp -> view.setFrame(bmp) }
        setContentView(view)
    }

    private fun setSystemBars(visible: Boolean) {
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.systemBarsBehavior = WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        if (visible) controller.show(WindowInsetsCompat.Type.systemBars())
        else controller.hide(WindowInsetsCompat.Type.systemBars())
    }

    override fun onDestroy() {
        conn?.close()
        super.onDestroy()
    }
}
