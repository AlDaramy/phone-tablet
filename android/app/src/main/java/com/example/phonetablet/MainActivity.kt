package com.example.phonetablet

import android.content.pm.ActivityInfo
import android.graphics.Color
import android.graphics.drawable.GradientDrawable
import android.os.Bundle
import android.text.InputType
import android.view.Gravity
import android.view.View
import android.view.ViewGroup
import android.view.WindowManager
import android.widget.Button
import android.widget.EditText
import android.widget.FrameLayout
import android.widget.LinearLayout
import android.widget.ScrollView
import android.widget.TextView
import androidx.activity.OnBackPressedCallback
import androidx.appcompat.app.AppCompatActivity
import androidx.core.view.WindowCompat
import androidx.core.view.WindowInsetsCompat
import androidx.core.view.WindowInsetsControllerCompat

class MainActivity : AppCompatActivity() {
    private var session: Session? = null
    private var tabletView: TabletView? = null
    private var overlay: TextView? = null
    private var screenButton: Button? = null

    private fun dp(v: Int) = (v * resources.displayMetrics.density).toInt()

    override fun onCreate(savedInstanceState: Bundle?) {
        super.onCreate(savedInstanceState)
        window.addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON)
        showForm()

        onBackPressedDispatcher.addCallback(this, object : OnBackPressedCallback(true) {
            override fun handleOnBackPressed() {
                if (tabletView != null) {
                    leaveTablet("")
                } else {
                    finish()
                }
            }
        })
    }

    // ============================== connect form =======================================
    private fun showForm(message: String = "") {
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_UNSPECIFIED
        setSystemBars(visible = true)
        val prefs = getSharedPreferences("tablet", MODE_PRIVATE)

        val root = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(24), dp(24), dp(24), dp(24))
        }
        root.addView(TextView(this).apply {
            text = "Phone Tablet"
            textSize = 22f
            setPadding(0, 0, 0, dp(12))
        })

        val hostRow = LinearLayout(this).apply { orientation = LinearLayout.HORIZONTAL }
        val host = EditText(this).apply {
            hint = "PC address  (USB: 127.0.0.1)"
            setText(prefs.getString("host", ""))
            setSingleLine()
        }
        val scan = Button(this).apply {
            text = "Scan"
            isAllCaps = false
        }
        hostRow.addView(host, LinearLayout.LayoutParams(0, ViewGroup.LayoutParams.WRAP_CONTENT, 1f))
        hostRow.addView(scan)
        root.addView(hostRow)

        val found = LinearLayout(this).apply { orientation = LinearLayout.VERTICAL }
        root.addView(found)

        val port = EditText(this).apply {
            hint = "Port"
            setText(prefs.getString("port", Proto.DEFAULT_PORT.toString()))
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
        }
        val pin = EditText(this).apply {
            hint = "PIN shown on the PC (6 digits)"
            setText(prefs.getString("pin", ""))
            inputType = InputType.TYPE_CLASS_NUMBER
            setSingleLine()
        }
        val connect = Button(this).apply {
            text = "Connect"
            isAllCaps = false
        }
        val status = TextView(this).apply {
            text = if (message.isNotEmpty()) message else "Run the server on the PC, then tap Scan."
            setPadding(0, dp(12), 0, 0)
        }
        root.addView(port)
        root.addView(pin)
        root.addView(connect)
        root.addView(status)
        setContentView(ScrollView(this).apply { addView(root) })

        scan.setOnClickListener {
            scan.isEnabled = false
            status.text = "Searching the Wi-Fi network..."
            found.removeAllViews()
            Discovery.scan(this) { list ->
                runOnUiThread {
                    scan.isEnabled = true
                    if (list.isEmpty()) {
                        status.text = "No PC found. Is the server running and on the same Wi-Fi? " +
                            "You can also type the address."
                    } else {
                        status.text = "Tap your PC:"
                        for (s in list) {
                            found.addView(Button(this).apply {
                                text = "${s.name}  (${s.host})" + if (s.needsPin) "  - PIN" else ""
                                isAllCaps = false
                                setOnClickListener {
                                    host.setText(s.host)
                                    port.setText(s.port.toString())
                                    if (s.needsPin) pin.requestFocus()
                                }
                            })
                        }
                    }
                }
            }
        }

        connect.setOnClickListener {
            val h = host.text.toString().trim()
            val p = port.text.toString().trim().toIntOrNull() ?: Proto.DEFAULT_PORT
            val pinText = pin.text.toString().trim()
            if (h.isEmpty()) {
                status.text = "Enter the PC address or tap Scan."
                return@setOnClickListener
            }
            prefs.edit().putString("host", h).putString("port", p.toString())
                .putString("pin", pinText).apply()
            status.text = "Connecting to $h:$p ..."
            connect.isEnabled = false
            connectTo(h, p, pinText.toIntOrNull() ?: 0)
        }
    }

    private fun connectTo(host: String, port: Int, pin: Int) {
        session?.close()
        lateinit var s: Session
        s = Session(host, port, pin, object : Session.Callbacks {
            override fun onReady() {
                if (session === s) handleReady()
            }

            override fun onFrame(frame: FrameInfo) {
                if (session !== s) return
                tabletView?.let { v -> v.post { v.applyFrame(frame) } }
            }

            override fun onState(text: String) {
                if (session === s) showOverlay(text)
            }

            override fun onFailed(reason: String) {
                if (session === s) leaveTablet("Not connected: $reason")
            }
        })
        session = s
        s.start()
    }

    private fun handleReady() {
        val view = tabletView
        if (view == null) {
            showTablet()
        } else {
            overlay?.visibility = View.GONE
            view.sendInitialState()
        }
    }

    private fun leaveTablet(message: String) {
        session?.close()
        session = null
        tabletView?.release()
        tabletView = null
        overlay = null
        screenButton = null
        showForm(message)
    }

    private fun showOverlay(message: String) {
        val note = overlay ?: return
        note.text = message
        note.visibility = View.VISIBLE
    }

    // ============================== tablet screen ======================================
    private fun showTablet() {
        val s = session ?: return
        requestedOrientation = ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE
        setSystemBars(visible = false)

        val root = FrameLayout(this)
        val view = TabletView(this, s)
        root.addView(view, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.MATCH_PARENT))
        root.addView(buildToolbar(view, s), FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.MATCH_PARENT, Gravity.START))

        val note = TextView(this).apply {
            setTextColor(Color.WHITE)
            textSize = 14f
            setPadding(dp(12), dp(6), dp(12), dp(6))
            background = rounded(0xCC000000.toInt())
            visibility = View.GONE
        }
        root.addView(note, FrameLayout.LayoutParams(
            ViewGroup.LayoutParams.WRAP_CONTENT, ViewGroup.LayoutParams.WRAP_CONTENT,
            Gravity.TOP or Gravity.CENTER_HORIZONTAL))

        view.onInfo = { idx, count ->
            screenButton?.apply {
                text = "Screen $idx/$count"
                visibility = if (count > 1) View.VISIBLE else View.GONE
            }
        }
        tabletView = view
        overlay = note
        setContentView(root)
        view.sendInitialState()
    }

    private fun rounded(color: Int) = GradientDrawable().apply {
        setColor(color)
        cornerRadius = dp(8).toFloat()
    }

    private fun toolButton(label: String, onClick: (Button) -> Unit): Button =
        Button(this).apply {
            text = label
            isAllCaps = false
            textSize = 12f
            setTextColor(Color.WHITE)
            minWidth = 0
            minHeight = 0
            minimumWidth = 0
            minimumHeight = 0
            setPadding(dp(10), dp(8), dp(10), dp(8))
            background = rounded(0xCC202020.toInt())
            setOnClickListener { onClick(this) }
        }

    private fun buildToolbar(view: TabletView, s: Session): View {
        fun key(action: Int) = s.send(Proto.KEY, action.toFloat(), 0f, 0f)

        val panel = LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(4), 0, dp(4), dp(4))
        }
        fun add(b: Button) {
            panel.addView(b, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT
            ).apply { topMargin = dp(4) })
        }

        add(toolButton("Undo") { key(Proto.ACT_UNDO) })
        add(toolButton("Redo") { key(Proto.ACT_REDO) }.apply {
            // long press = Ctrl+Shift+Z (the redo shortcut in Krita / Photoshop)
            setOnLongClickListener { key(Proto.ACT_REDO_ALT); true }
        })
        add(toolButton("Eraser") { key(Proto.ACT_ERASER) })
        add(toolButton("Brush") { key(Proto.ACT_BRUSH) })
        add(toolButton("Size -") { key(Proto.ACT_BRUSH_SMALLER) })
        add(toolButton("Size +") { key(Proto.ACT_BRUSH_BIGGER) })
        add(toolButton("Save") { key(Proto.ACT_SAVE) })
        add(toolButton("Fit") { view.fitView() })
        add(toolButton("Window") { view.snapWindow() })
        val screen = toolButton("Screen") { view.nextMonitor() }.apply { visibility = View.GONE }
        screenButton = screen
        add(screen)
        add(toolButton("Finger: Draw") { b ->
            view.fingerDraws = !view.fingerDraws
            b.text = if (view.fingerDraws) "Finger: Draw" else "Finger: Pan"
        })
        add(toolButton("Exit") { leaveTablet("") })

        val scroll = ScrollView(this).apply {
            isVerticalScrollBarEnabled = false
            addView(panel)
        }
        val toggle = toolButton("Menu") { b ->
            val show = scroll.visibility != View.VISIBLE
            scroll.visibility = if (show) View.VISIBLE else View.GONE
            b.text = if (show) "Hide" else "Menu"
        }.apply { text = "Hide" }

        return LinearLayout(this).apply {
            orientation = LinearLayout.VERTICAL
            setPadding(dp(4), dp(4), 0, 0)
            addView(toggle, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.MATCH_PARENT, ViewGroup.LayoutParams.WRAP_CONTENT))
            addView(scroll, LinearLayout.LayoutParams(
                ViewGroup.LayoutParams.WRAP_CONTENT, 0, 1f))
        }
    }

    private fun setSystemBars(visible: Boolean) {
        val controller = WindowCompat.getInsetsController(window, window.decorView)
        controller.systemBarsBehavior =
            WindowInsetsControllerCompat.BEHAVIOR_SHOW_TRANSIENT_BARS_BY_SWIPE
        if (visible) controller.show(WindowInsetsCompat.Type.systemBars())
        else controller.hide(WindowInsetsCompat.Type.systemBars())
    }

    override fun onDestroy() {
        session?.close()
        super.onDestroy()
    }
}
