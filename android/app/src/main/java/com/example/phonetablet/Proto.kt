package com.example.phonetablet

import android.graphics.Bitmap
import android.graphics.BitmapFactory
import java.nio.ByteBuffer

/** Wire format shared with server/common.py (see the docstring there). */
object Proto {
    const val DOWN: Byte = 0
    const val MOVE: Byte = 1
    const val UP: Byte = 2
    const val HOVER: Byte = 3
    const val LEAVE: Byte = 4
    const val RESIZE: Byte = 10
    const val AUTH: Byte = 11
    const val KEY: Byte = 12
    const val VIEW_POS: Byte = 13
    const val VIEW_SIZE: Byte = 14
    const val MONITOR: Byte = 15
    const val SNAP_WINDOW: Byte = 16

    const val FLAG_BARREL = 0x40
    const val FLAG_ERASER = 0x80

    const val MSG_FRAME = 0
    const val MSG_TEXT = 1
    const val FRAME_HEAD_SIZE = 31

    const val DEFAULT_PORT = 5555
    const val DISCOVERY_PORT = 5556

    // shortcut action ids (server/common.py ACTIONS)
    const val ACT_UNDO = 1
    const val ACT_REDO = 2
    const val ACT_REDO_ALT = 3
    const val ACT_ERASER = 4
    const val ACT_BRUSH = 5
    const val ACT_SAVE = 6
    const val ACT_BRUSH_SMALLER = 7
    const val ACT_BRUSH_BIGGER = 8
}

/** A rectangle in monitor fractions (0..1). */
class Region(val l: Float, val t: Float, val w: Float, val h: Float)

class FrameInfo(
    val region: Region,
    val cursorX: Float,
    val cursorY: Float,
    val monitor: Int,
    val monitorCount: Int,
    val monW: Int,
    val monH: Int,
    val forced: Boolean,
    val bitmap: Bitmap?,
)

/** data[0] = message kind (already checked to be MSG_FRAME), then the 31-byte head, then JPEG. */
fun parseFrame(data: ByteArray): FrameInfo? {
    val jpegOffset = 1 + Proto.FRAME_HEAD_SIZE
    if (data.size < jpegOffset) return null
    val bb = ByteBuffer.wrap(data, 1, Proto.FRAME_HEAD_SIZE) // big-endian
    val l = bb.float
    val t = bb.float
    val w = bb.float
    val h = bb.float
    val cx = bb.float
    val cy = bb.float
    val monitor = bb.get().toInt() and 0xFF
    val count = bb.get().toInt() and 0xFF
    val monW = bb.short.toInt() and 0xFFFF
    val monH = bb.short.toInt() and 0xFFFF
    val forced = bb.get().toInt() != 0
    val jpegLen = data.size - jpegOffset
    val bmp = if (jpegLen > 0) BitmapFactory.decodeByteArray(data, jpegOffset, jpegLen) else null
    return FrameInfo(Region(l, t, w, h), cx, cy, monitor, count, monW, monH, forced, bmp)
}
