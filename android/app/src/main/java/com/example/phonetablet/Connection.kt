package com.example.phonetablet

import java.io.DataInputStream
import java.io.IOException
import java.net.InetSocketAddress
import java.net.Socket
import java.util.concurrent.LinkedBlockingQueue
import java.util.concurrent.atomic.AtomicBoolean

/**
 * One TCP connection attempt to the Windows server.
 * Writes queued packets on one thread and reads length-prefixed messages on another.
 * Messages are delivered as [kind byte][body]. onClosed is called at most once, and never
 * after close() was called by us.
 */
class Connection(
    private val host: String,
    private val port: Int,
    private val listener: Listener,
) {
    interface Listener {
        fun onConnected(c: Connection)
        fun onMessage(c: Connection, data: ByteArray)
        fun onClosed(c: Connection, reason: String)
    }

    private val queue = LinkedBlockingQueue<ByteArray>()
    @Volatile private var running = true
    @Volatile private var socket: Socket? = null
    private val notified = AtomicBoolean(false)

    fun start() {
        Thread {
            try {
                val s = Socket()
                s.tcpNoDelay = true
                s.keepAlive = true
                s.connect(InetSocketAddress(host, port), 4000)
                socket = s
                if (!running) {
                    s.close()
                    return@Thread
                }
                listener.onConnected(this)
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
                if (len <= 0 || len > 20_000_000) throw IOException("Bad message size")
                val data = ByteArray(len)
                input.readFully(data)
                listener.onMessage(this, data)
            }
        } catch (e: Exception) {
            fail(e.message ?: "Server closed the connection")
        }
    }

    fun send(packet: ByteArray) {
        if (running) queue.offer(packet)
    }

    private fun fail(reason: String) {
        running = false
        try { socket?.close() } catch (_: Exception) {}
        queue.offer(ByteArray(0)) // wake the writer thread
        if (notified.compareAndSet(false, true)) listener.onClosed(this, reason)
    }

    /** Close on purpose: no onClosed callback. */
    fun close() {
        notified.set(true)
        running = false
        try { socket?.close() } catch (_: Exception) {}
        queue.offer(ByteArray(0))
    }
}
