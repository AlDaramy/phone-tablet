package com.example.phonetablet

import android.content.Context
import android.net.wifi.WifiManager
import java.net.DatagramPacket
import java.net.DatagramSocket
import java.net.InetAddress
import java.net.SocketTimeoutException

/** A Windows server found on the local network. */
class Found(val name: String, val host: String, val port: Int, val needsPin: Boolean)

/** Finds servers by UDP broadcast: sends "PHONETABLET?" and collects "PHONETABLET!name|port|pin". */
object Discovery {
    private const val QUERY = "PHONETABLET?"
    private const val REPLY = "PHONETABLET!"

    /** Runs in a background thread; onResult is called from that thread. */
    fun scan(context: Context, onResult: (List<Found>) -> Unit) {
        val appContext = context.applicationContext
        Thread {
            val found = LinkedHashMap<String, Found>()
            var socket: DatagramSocket? = null
            try {
                socket = DatagramSocket().apply {
                    broadcast = true
                    soTimeout = 300
                }
                val query = QUERY.toByteArray()
                val targets = broadcastAddresses(appContext)
                val buf = ByteArray(512)
                val deadline = System.currentTimeMillis() + 2000
                var lastSend = 0L
                while (System.currentTimeMillis() < deadline) {
                    if (System.currentTimeMillis() - lastSend > 600) {
                        for (t in targets) {
                            try {
                                socket.send(DatagramPacket(query, query.size, t, Proto.DISCOVERY_PORT))
                            } catch (_: Exception) {
                            }
                        }
                        lastSend = System.currentTimeMillis()
                    }
                    try {
                        val p = DatagramPacket(buf, buf.size)
                        socket.receive(p)
                        parseReply(p)?.let { found[it.host] = it }
                    } catch (_: SocketTimeoutException) {
                    }
                }
            } catch (_: Exception) {
            } finally {
                try { socket?.close() } catch (_: Exception) {}
            }
            onResult(found.values.toList())
        }.apply { isDaemon = true }.start()
    }

    private fun parseReply(p: DatagramPacket): Found? {
        val text = String(p.data, 0, p.length, Charsets.UTF_8)
        if (!text.startsWith(REPLY)) return null
        val parts = text.removePrefix(REPLY).split("|")
        if (parts.size < 3) return null
        val port = parts[1].toIntOrNull() ?: return null
        val host = p.address?.hostAddress ?: return null
        return Found(parts[0], host, port, parts[2] == "1")
    }

    private fun broadcastAddresses(context: Context): List<InetAddress> {
        val list = ArrayList<InetAddress>()
        try {
            list.add(InetAddress.getByName("255.255.255.255"))
        } catch (_: Exception) {
        }
        try {
            @Suppress("DEPRECATION")
            val dhcp = (context.getSystemService(Context.WIFI_SERVICE) as WifiManager).dhcpInfo
            if (dhcp != null && dhcp.netmask != 0) {
                val b = (dhcp.ipAddress and dhcp.netmask) or dhcp.netmask.inv()
                val bytes = byteArrayOf(
                    (b and 0xFF).toByte(),
                    ((b shr 8) and 0xFF).toByte(),
                    ((b shr 16) and 0xFF).toByte(),
                    ((b shr 24) and 0xFF).toByte(),
                )
                list.add(InetAddress.getByAddress(bytes))
            }
        } catch (_: Exception) {
        }
        return list
    }
}
