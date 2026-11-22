package com.example.phonetablet

import android.os.Handler
import android.os.Looper
import java.nio.ByteBuffer

/**
 * A logical session with the server: connects, sends the PIN, and reconnects by itself
 * when the link drops (until close() is called). Packet sending is a no-op while not ready.
 *
 * onReady / onState / onFailed are delivered on the main thread; onFrame on the reader thread.
 */
class Session(
    private val host: String,
    private val port: Int,
    private val pin: Int,
    private val callbacks: Callbacks,
) : Connection.Listener {

    interface Callbacks {
        fun onReady()
        fun onFrame(frame: FrameInfo)
        fun onState(text: String)
        fun onFailed(reason: String)
    }

    private val main = Handler(Looper.getMainLooper())
    @Volatile private var conn: Connection? = null
    @Volatile var ready = false
        private set
    @Volatile private var closedByUser = false
    @Volatile private var authFailed = false
    @Volatile private var everReady = false
    @Volatile private var attempts = 0

    fun start() = connect()

    private fun connect() {
        val c = Connection(host, port, this)
        conn = c
        c.start()
    }

    fun send(type: Byte, x: Float, y: Float, pressure: Float, flags: Int = 0) {
        if (!ready) return
        conn?.send(packet(type, x, y, pressure, flags))
    }

    fun close() {
        closedByUser = true
        ready = false
        conn?.close()
    }

    private fun packet(type: Byte, x: Float, y: Float, p: Float, flags: Int): ByteArray =
        ByteBuffer.allocate(13)
            .put((type.toInt() or flags).toByte())
            .putFloat(x).putFloat(y).putFloat(p)
            .array()

    // ---- Connection.Listener ------------------------------------------------
    override fun onConnected(c: Connection) {
        if (c !== conn) return
        c.send(packet(Proto.AUTH, pin.toFloat(), 0f, 0f, 0)) // always the first packet
    }

    override fun onMessage(c: Connection, data: ByteArray) {
        if (c !== conn || data.isEmpty()) return
        when (data[0].toInt()) {
            Proto.MSG_TEXT -> handleText(c, String(data, 1, data.size - 1, Charsets.UTF_8))
            Proto.MSG_FRAME -> if (ready) parseFrame(data)?.let { callbacks.onFrame(it) }
        }
    }

    private fun handleText(c: Connection, text: String) {
        when (text) {
            "AUTH_OK" -> {
                ready = true
                everReady = true
                attempts = 0
                main.post { callbacks.onReady() }
            }
            "AUTH_FAIL" -> {
                authFailed = true
                ready = false
                c.close()
                main.post { callbacks.onFailed("Wrong PIN") }
            }
        }
    }

    override fun onClosed(c: Connection, reason: String) {
        if (c !== conn || closedByUser || authFailed) return
        ready = false
        if (!everReady) {
            main.post { callbacks.onFailed(reason) }
            return
        }
        val n = ++attempts
        main.post { callbacks.onState("Connection lost - reconnecting ($n)...") }
        main.postDelayed({
            if (!closedByUser && c === conn) connect()
        }, 2000)
    }
}
