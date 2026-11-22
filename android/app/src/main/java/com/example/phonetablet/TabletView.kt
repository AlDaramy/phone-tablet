package com.example.phonetablet

import android.content.Context
import android.graphics.Bitmap
import android.graphics.Canvas
import android.graphics.Color
import android.graphics.Paint
import android.graphics.RectF
import android.os.Handler
import android.os.Looper
import android.view.MotionEvent
import android.view.View
import android.view.ViewConfiguration
import kotlin.math.hypot
import kotlin.math.max
import kotlin.math.min

/**
 * Shows the laptop screen and turns touch / stylus input into pen packets.
 *
 * The visible area is a "viewport" (vl, vt, vw, vh) in monitor fractions. The server crops that
 * area, so zooming stays sharp. All coordinates sent to the server are monitor fractions.
 *  - stylus: draws (barrel button / eraser end are forwarded)
 *  - finger: draws or pans (toggle), two fingers always pinch-zoom and pan
 */
class TabletView(context: Context, private val session: Session) : View(context) {

    companion object {
        private const val MIN_FRAC = 0.02f
        private const val PRESS_DELAY_MS = 70L
        private const val VIEW_SEND_MS = 40L
    }

    /** Called (on the UI thread) when the server reports which screen is shown. */
    var onInfo: ((monitor: Int, count: Int) -> Unit)? = null

    /** true: one finger draws; false: one finger pans. */
    var fingerDraws = true

    // ---- what the laptop sent us ------------------------------------------------
    private var frame: Bitmap? = null
    private var frameRegion = Region(0f, 0f, 1f, 1f)
    private var monW = 16
    private var monH = 9
    private var cursorX = -1f
    private var cursorY = -1f
    private var monitorIndex = 0
    private var monitorCount = 1

    // ---- viewport in monitor fractions -------------------------------------------
    private var vl = 0f
    private var vt = 0f
    private var vw = 1f
    private var vh = 1f

    private val dst = RectF()
    private val tmp = RectF()
    private val handler = Handler(Looper.getMainLooper())
    private val slop = ViewConfiguration.get(context).scaledTouchSlop.toFloat()
    private val density = context.resources.displayMetrics.density

    private val bitmapPaint = Paint(Paint.FILTER_BITMAP_FLAG)
    private val ringDark = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.BLACK
        strokeWidth = 5f
    }
    private val ringLight = Paint(Paint.ANTI_ALIAS_FLAG).apply {
        style = Paint.Style.STROKE
        color = Color.WHITE
        strokeWidth = 2.5f
    }

    init {
        setBackgroundColor(Color.BLACK)
    }

    // ============================== frames from the server ==============================
    fun applyFrame(f: FrameInfo) {
        monW = max(1, f.monW)
        monH = max(1, f.monH)
        f.bitmap?.let {
            frame = it
            frameRegion = f.region
        }
        cursorX = f.cursorX
        cursorY = f.cursorY
        if (f.forced) setViewport(f.region.l, f.region.t, f.region.w, f.region.h, send = false)
        if (f.monitor != monitorIndex || f.monitorCount != monitorCount) {
            monitorIndex = f.monitor
            monitorCount = f.monitorCount
            onInfo?.invoke(monitorIndex, monitorCount)
        }
        invalidate()
    }

    // ============================== viewport handling ===================================
    private fun layoutDst() {
        if (width == 0 || height == 0) return
        val aspect = (vw * monW) / (vh * monH)
        var dw = width.toFloat()
        var dh = dw / aspect
        if (dh > height) {
            dh = height.toFloat()
            dw = dh * aspect
        }
        val left = (width - dw) / 2f
        val top = (height - dh) / 2f
        dst.set(left, top, left + dw, top + dh)
    }

    private fun toMonX(px: Float) = (vl + (px - dst.left) / dst.width() * vw).coerceIn(0f, 1f)
    private fun toMonY(py: Float) = (vt + (py - dst.top) / dst.height() * vh).coerceIn(0f, 1f)

    private fun setViewport(l: Float, t: Float, w: Float, h: Float, send: Boolean = true) {
        vw = w.coerceIn(MIN_FRAC, 1f)
        vh = h.coerceIn(MIN_FRAC, 1f)
        vl = l.coerceIn(0f, 1f - vw)
        vt = t.coerceIn(0f, 1f - vh)
        invalidate()
        if (send) scheduleViewSend()
    }

    private var viewSendPending = false
    private val sendViewRunnable = Runnable {
        viewSendPending = false
        sendViewNow()
    }

    private fun scheduleViewSend() {
        if (viewSendPending) return
        viewSendPending = true
        handler.postDelayed(sendViewRunnable, VIEW_SEND_MS)
    }

    private fun sendViewNow() {
        session.send(Proto.VIEW_POS, vl, vt, 0f)
        session.send(Proto.VIEW_SIZE, vw, vh, 0f)
    }

    /** Called after (re)connecting: tell the server our size, screen and zoom again. */
    fun sendInitialState() {
        if (width > 0) session.send(Proto.RESIZE, width.toFloat(), height.toFloat(), 0f)
        if (monitorIndex > 0) session.send(Proto.MONITOR, monitorIndex.toFloat(), 0f, 0f)
        if (vw < 1f || vh < 1f) sendViewNow()
    }

    fun fitView() = setViewport(0f, 0f, 1f, 1f)

    fun snapWindow() = session.send(Proto.SNAP_WINDOW, 0f, 0f, 0f)

    fun nextMonitor() {
        if (monitorCount > 1) {
            val next = (monitorIndex % monitorCount) + 1
            session.send(Proto.MONITOR, next.toFloat(), 0f, 0f)
        }
    }

    fun release() {
        handler.removeCallbacksAndMessages(null)
    }

    override fun onSizeChanged(w: Int, h: Int, oldw: Int, oldh: Int) {
        super.onSizeChanged(w, h, oldw, oldh)
        layoutDst()
        session.send(Proto.RESIZE, w.toFloat(), h.toFloat(), 0f)
    }

    // ============================== drawing ============================================
    override fun onDraw(canvas: Canvas) {
        layoutDst()
        if (dst.width() <= 0f) return
        val sx = dst.width() / vw
        val sy = dst.height() / vh
        canvas.save()
        canvas.clipRect(dst)
        canvas.drawColor(0xFF111111.toInt())
        val f = frame
        if (f != null) {
            val r = frameRegion
            val left = dst.left + (r.l - vl) * sx
            val top = dst.top + (r.t - vt) * sy
            tmp.set(left, top, left + r.w * sx, top + r.h * sy)
            canvas.drawBitmap(f, null, tmp, bitmapPaint)
        }
        if (cursorX >= 0f && cursorY >= 0f) {
            val px = dst.left + (cursorX - vl) * sx
            val py = dst.top + (cursorY - vt) * sy
            val radius = 9f * density
            canvas.drawCircle(px, py, radius, ringDark)
            canvas.drawCircle(px, py, radius, ringLight)
        }
        canvas.restore()
    }

    // ============================== touch handling =====================================
    private var drawingId = -1
    private var drawingPen = false
    private var lastX = 0f
    private var lastY = 0f
    private var lastPressure = 0f
    private var lastFlags = 0

    private val fingers = mutableSetOf<Int>()
    private var blocked = false // after a pinch, ignore the remaining finger until all are up

    private var pendingId = -1
    private var pendingX = 0f
    private var pendingY = 0f
    private val flushRunnable = Runnable { flushPending() }

    private var panning = false
    private var panX = 0f
    private var panY = 0f

    private var gesture = false
    private var g1 = -1
    private var g2 = -1
    private var prevMidX = 0f
    private var prevMidY = 0f
    private var prevDist = 0f

    private fun isPenTool(t: Int) =
        t == MotionEvent.TOOL_TYPE_STYLUS || t == MotionEvent.TOOL_TYPE_ERASER

    private fun pressureOf(e: MotionEvent, idx: Int, raw: Float): Float =
        if (isPenTool(e.getToolType(idx))) raw.coerceIn(0f, 1f) else 0.5f

    private fun flagsOf(e: MotionEvent, idx: Int): Int {
        var f = 0
        if (e.getToolType(idx) == MotionEvent.TOOL_TYPE_ERASER) f = f or Proto.FLAG_ERASER
        if (e.buttonState and MotionEvent.BUTTON_STYLUS_PRIMARY != 0) f = f or Proto.FLAG_BARREL
        return f
    }

    private fun sendPoint(type: Byte, px: Float, py: Float, pressure: Float, flags: Int) {
        lastX = px
        lastY = py
        lastPressure = pressure
        lastFlags = flags
        session.send(type, toMonX(px), toMonY(py), pressure, flags)
    }

    override fun onTouchEvent(e: MotionEvent): Boolean {
        layoutDst()
        if (dst.width() <= 0f) return true
        when (e.actionMasked) {
            MotionEvent.ACTION_DOWN, MotionEvent.ACTION_POINTER_DOWN -> pointerDown(e, e.actionIndex)
            MotionEvent.ACTION_MOVE -> pointersMove(e)
            MotionEvent.ACTION_UP, MotionEvent.ACTION_POINTER_UP -> pointerUp(e, e.actionIndex)
            MotionEvent.ACTION_CANCEL -> cancelAll()
        }
        return true
    }

    private fun pointerDown(e: MotionEvent, idx: Int) {
        val id = e.getPointerId(idx)
        if (isPenTool(e.getToolType(idx))) {
            cancelFingerActions()
            if (drawingId < 0) {
                drawingId = id
                drawingPen = true
                sendPoint(
                    Proto.DOWN, e.getX(idx), e.getY(idx),
                    pressureOf(e, idx, e.getPressure(idx)), flagsOf(e, idx),
                )
            }
            return
        }
        if (drawingPen) return // a palm or finger while the pen is drawing
        fingers.add(id)
        if (fingers.size == 1) {
            if (fingerDraws) {
                pendingId = id
                pendingX = e.getX(idx)
                pendingY = e.getY(idx)
                handler.postDelayed(flushRunnable, PRESS_DELAY_MS)
            } else {
                panning = true
                panX = e.getX(idx)
                panY = e.getY(idx)
            }
        } else if (fingers.size == 2) {
            cancelFingerStroke()
            panning = false
            startGesture(e)
        }
    }

    /** A finger stayed down long enough (or moved): it really is a stroke, not the start of a pinch. */
    private fun flushPending() {
        if (pendingId < 0) return
        handler.removeCallbacks(flushRunnable)
        drawingId = pendingId
        drawingPen = false
        pendingId = -1
        sendPoint(Proto.DOWN, pendingX, pendingY, 0.5f, 0)
    }

    private fun pointersMove(e: MotionEvent) {
        if (pendingId >= 0) {
            val i = e.findPointerIndex(pendingId)
            if (i < 0) return
            if (hypot(e.getX(i) - pendingX, e.getY(i) - pendingY) > slop) flushPending() else return
        }
        if (drawingId >= 0) {
            val i = e.findPointerIndex(drawingId)
            if (i < 0) return
            val flags = flagsOf(e, i)
            for (h in 0 until e.historySize) {
                sendPoint(
                    Proto.MOVE, e.getHistoricalX(i, h), e.getHistoricalY(i, h),
                    pressureOf(e, i, e.getHistoricalPressure(i, h)), flags,
                )
            }
            sendPoint(Proto.MOVE, e.getX(i), e.getY(i), pressureOf(e, i, e.getPressure(i)), flags)
            return
        }
        if (gesture) {
            updateGesture(e)
            return
        }
        if (panning && !blocked && fingers.size == 1) {
            val i = e.findPointerIndex(fingers.first())
            if (i < 0) return
            val dx = e.getX(i) - panX
            val dy = e.getY(i) - panY
            panX = e.getX(i)
            panY = e.getY(i)
            setViewport(vl - dx / dst.width() * vw, vt - dy / dst.height() * vh, vw, vh)
        }
    }

    private fun pointerUp(e: MotionEvent, idx: Int) {
        val id = e.getPointerId(idx)
        if (id == pendingId) flushPending() // a quick tap still draws a dot
        if (id == drawingId) {
            sendPoint(Proto.UP, e.getX(idx), e.getY(idx), 0f, lastFlags)
            drawingId = -1
            drawingPen = false
        }
        fingers.remove(id)
        if (gesture && fingers.size < 2) {
            gesture = false
            blocked = fingers.isNotEmpty()
        }
        if (fingers.isEmpty()) {
            blocked = false
            panning = false
        }
    }

    private fun cancelFingerStroke() {
        if (pendingId >= 0) {
            handler.removeCallbacks(flushRunnable)
            pendingId = -1
        } else if (drawingId >= 0 && !drawingPen) {
            sendPoint(Proto.UP, lastX, lastY, 0f, lastFlags)
            drawingId = -1
        }
    }

    private fun cancelFingerActions() {
        cancelFingerStroke()
        gesture = false
        panning = false
        fingers.clear()
        blocked = false
    }

    private fun cancelAll() {
        cancelFingerStroke()
        if (drawingId >= 0) {
            sendPoint(Proto.UP, lastX, lastY, 0f, lastFlags)
            drawingId = -1
            drawingPen = false
        }
        gesture = false
        panning = false
        blocked = false
        fingers.clear()
    }

    // ---- two-finger pinch zoom + pan ------------------------------------------------
    private fun startGesture(e: MotionEvent) {
        val ids = fingers.toList()
        g1 = ids[0]
        g2 = ids[1]
        val i1 = e.findPointerIndex(g1)
        val i2 = e.findPointerIndex(g2)
        if (i1 < 0 || i2 < 0) return
        prevMidX = (e.getX(i1) + e.getX(i2)) / 2f
        prevMidY = (e.getY(i1) + e.getY(i2)) / 2f
        prevDist = hypot(e.getX(i1) - e.getX(i2), e.getY(i1) - e.getY(i2))
        gesture = true
    }

    private fun updateGesture(e: MotionEvent) {
        val i1 = e.findPointerIndex(g1)
        val i2 = e.findPointerIndex(g2)
        if (i1 < 0 || i2 < 0) return
        val midX = (e.getX(i1) + e.getX(i2)) / 2f
        val midY = (e.getY(i1) + e.getY(i2)) / 2f
        val dist = hypot(e.getX(i1) - e.getX(i2), e.getY(i1) - e.getY(i2))
        if (prevDist > 8f && dist > 8f) {
            // the monitor point that was under the previous midpoint must end up under the new one
            val fx = vl + (prevMidX - dst.left) / dst.width() * vw
            val fy = vt + (prevMidY - dst.top) / dst.height() * vh
            val lo = max(MIN_FRAC / vw, MIN_FRAC / vh)
            val hi = min(1f / vw, 1f / vh)
            val factor = (prevDist / dist).coerceIn(lo, max(lo, hi))
            val nw = vw * factor
            val nh = vh * factor
            val nl = fx - (midX - dst.left) / dst.width() * nw
            val nt = fy - (midY - dst.top) / dst.height() * nh
            setViewport(nl, nt, nw, nh)
        }
        prevMidX = midX
        prevMidY = midY
        prevDist = dist
    }

    // ---- stylus hover (devices that report it) ---------------------------------------
    override fun onHoverEvent(e: MotionEvent): Boolean {
        layoutDst()
        if (dst.width() <= 0f || drawingId >= 0 || gesture) return true
        val flags = flagsOf(e, 0)
        when (e.actionMasked) {
            MotionEvent.ACTION_HOVER_ENTER, MotionEvent.ACTION_HOVER_MOVE ->
                session.send(Proto.HOVER, toMonX(e.x), toMonY(e.y), 0f, flags)
            MotionEvent.ACTION_HOVER_EXIT ->
                session.send(Proto.LEAVE, toMonX(e.x), toMonY(e.y), 0f, flags)
        }
        return true
    }
}
