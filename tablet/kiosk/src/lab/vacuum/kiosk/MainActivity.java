package lab.vacuum.kiosk;

import android.app.Activity;
import android.app.AlertDialog;
import android.content.Intent;
import android.content.pm.ActivityInfo;
import android.content.pm.PackageInfo;
import android.graphics.Color;
import android.graphics.Rect;
import android.graphics.Typeface;
import android.net.LinkAddress;
import android.os.Bundle;
import android.os.Handler;
import android.os.HandlerThread;
import android.os.Looper;
import android.os.SystemClock;
import android.provider.Settings;
import android.text.InputType;
import android.util.Log;
import android.util.TypedValue;
import android.view.Gravity;
import android.view.InputDevice;
import android.view.MotionEvent;
import android.view.View;
import android.view.ViewGroup;
import android.view.Window;
import android.view.WindowManager;
import android.view.inputmethod.EditorInfo;
import android.webkit.ConsoleMessage;
import android.webkit.RenderProcessGoneDetail;
import android.webkit.WebChromeClient;
import android.webkit.WebResourceError;
import android.webkit.WebResourceRequest;
import android.webkit.WebResourceResponse;
import android.webkit.WebSettings;
import android.webkit.WebView;
import android.webkit.WebViewClient;
import android.widget.EditText;
import android.widget.FrameLayout;
import android.widget.LinearLayout;
import android.widget.TextView;
import android.widget.Toast;

import java.text.SimpleDateFormat;
import java.util.Date;
import java.util.List;
import java.util.Locale;

/**
 * The whole kiosk: a full-screen WebView showing the control panel, and a
 * native cover over it whenever the panel is not reachable.
 *
 * Finding the server and watching it run on one worker thread. Every search
 * or connection bumps {@link #generation}, and work queued for an older
 * generation drops itself, so a slow probe can never undo a newer decision.
 */
public final class MainActivity extends Activity {

    static final String EXTRA_RECONFIGURE = "lab.vacuum.kiosk.RECONFIGURE";

    private static final String TAG = Kiosk.TAG;

    private static final long SEARCH_EVERY_MS = 2000;
    private static final long LAN_SWEEP_EVERY_MS = 20000;
    private static final long WATCH_EVERY_MS = 5000;
    /** Missed pings before the panel counts as gone: 15 s, longer than a service restart. */
    private static final int LOST_AFTER = 3;
    /** While on a fallback, look for a preferred server every this many pings (30 s). */
    private static final int PREFER_EVERY = 6;
    private static final int CORNER_TAPS = 5;
    private static final long CORNER_WINDOW_MS = 3000;

    private Config config;
    private FrameLayout root;
    private WebView web;
    private LinearLayout cover;
    private TextView coverStatus;
    private TextView coverDetail;
    private View customView;
    private WebChromeClient.CustomViewCallback customCallback;

    private final Handler ui = new Handler(Looper.getMainLooper());
    private HandlerThread workerThread;
    private Handler worker;

    /** Written on the UI thread only; read by the worker to drop stale work. */
    private volatile int generation;
    /** The server being shown, or null while searching. UI thread. */
    private String current;
    private boolean pageFailed;

    /** Worker thread only. */
    /** When each Wi-Fi search last swept its subnet (elapsed ms), by candidate. */
    private final java.util.Map<String, Long> lastSweeps = new java.util.HashMap<>();
    private int attempts;

    private int cornerTaps;
    private long cornerStart;

    // --- lifecycle --------------------------------------------------------------

    @Override
    protected void onCreate(Bundle state) {
        super.onCreate(state);
        config = new Config(this);

        getWindow().addFlags(WindowManager.LayoutParams.FLAG_KEEP_SCREEN_ON);
        setShowWhenLocked(true);
        setTurnScreenOn(true);
        // chrome://inspect from a computer over adb: the only way to see the
        // panel's console on the tablet, and adb access is trusted already.
        WebView.setWebContentsDebuggingEnabled(true);

        root = new FrameLayout(this);
        root.setBackgroundColor(Color.BLACK);
        web = createWebView();
        root.addView(web, match());
        cover = createCover();
        root.addView(cover, match());
        setContentView(root);
        keepAboveKeyboard();

        workerThread = new HandlerThread("finder");
        workerThread.start();
        worker = new Handler(workerThread.getLooper());

        applyOrientation();
        startSearch("Starting up");
    }

    @Override
    protected void onResume() {
        super.onResume();
        Kiosk.applyPolicies(this);
        Kiosk.lock(this);
        hideSystemUi();
    }

    @Override
    public void onWindowFocusChanged(boolean hasFocus) {
        super.onWindowFocusChanged(hasFocus);
        if (hasFocus) hideSystemUi();
    }

    @Override
    protected void onNewIntent(Intent intent) {
        super.onNewIntent(intent);
        if (intent.getBooleanExtra(EXTRA_RECONFIGURE, false)) {
            web.getSettings().setTextZoom(config.textZoom());
            applyOrientation();
            startSearch("Reconfigured over adb");
        }
    }

    @Override
    public void onBackPressed() {
        // Never leave: back only steps back inside the panel.
        if (customView != null) {
            hideCustomView();
        } else if (current != null && web.canGoBack()) {
            web.goBack();
        }
    }

    @Override
    protected void onDestroy() {
        generation++;
        worker.removeCallbacksAndMessages(null);
        workerThread.quitSafely();
        web.destroy();
        super.onDestroy();
    }

    // --- finding the panel -------------------------------------------------------

    /** Cover the panel and look for a server, from the top of the list. */
    private void startSearch(String reason) {
        Log.i(TAG, "search: " + reason);
        final int gen = ++generation;
        current = null;
        hideCustomView();
        showCover("Looking for the control panel…", reason);
        worker.removeCallbacksAndMessages(null);
        worker.post(() -> searchOnce(gen));
    }

    /** Worker: one pass over the candidates; repeats itself until one answers. */
    private void searchOnce(int gen) {
        if (gen != generation) return;
        attempts++;
        String path = config.probePath();
        String marker = config.probeMarker();
        StringBuilder lines = new StringBuilder();

        for (String candidate : config.candidates()) {
            String found = null;
            if (Config.isLan(candidate)) {
                LinkAddress self = Finder.lanAddress(this);
                String subnet = Config.lanSubnet(candidate);
                if (self == null) {
                    lines.append("Wi-Fi search: the tablet is not on Wi-Fi\n");
                    continue;
                }
                // `lan` sweeps the tablet's own /24; `lan:a.b.c` the one named.
                String prefix = subnet != null ? subnet : Finder.prefix(self);
                String range = prefix + ".0/24";
                String last = config.lastLan();
                long now = SystemClock.elapsedRealtime();
                Long previous = lastSweeps.get(candidate);
                if (!last.isEmpty() && Finder.probe(last, path, marker, 1500)) {
                    found = last;
                } else if (previous == null || now - previous >= LAN_SWEEP_EVERY_MS) {
                    lastSweeps.put(candidate, now);
                    found = Finder.scanLan(prefix, self.getAddress().getHostAddress(),
                            config.lanPort(), path, marker);
                    if (found != null) config.setLastLan(found);
                    else lines.append("Wi-Fi search of ").append(range)
                            .append(": no panel on port ").append(config.lanPort()).append('\n');
                } else {
                    long wait = (LAN_SWEEP_EVERY_MS - (now - previous)) / 1000;
                    lines.append("Wi-Fi search of ").append(range)
                            .append(": next sweep in ").append(Math.max(wait, 1)).append(" s\n");
                }
            } else if (Finder.probe(candidate, path, marker, 1500)) {
                found = candidate;
            } else {
                lines.append(Finder.label(candidate)).append(": no answer\n");
            }
            if (found != null) {
                final String base = found;
                ui.post(() -> { if (gen == generation) connect(base); });
                return;
            }
        }

        String stamp = new SimpleDateFormat("HH:mm:ss", Locale.ROOT).format(new Date());
        lines.append("\nAttempt ").append(attempts).append(" at ").append(stamp);
        final String detail = lines.toString();
        ui.post(() -> { if (gen == generation) coverDetail.setText(detail); });
        worker.postDelayed(() -> searchOnce(gen), SEARCH_EVERY_MS);
    }

    /** Load the panel from base, and start watching it. */
    private void connect(String base) {
        Log.i(TAG, "connect: " + base);
        final int gen = ++generation;
        current = base;
        pageFailed = false;
        showCover("Connecting…", Finder.label(base));
        web.loadUrl(base + "/");
        worker.removeCallbacksAndMessages(null);
        worker.postDelayed(() -> watch(gen, base, 0, 0), WATCH_EVERY_MS);
    }

    /** Worker: ping the server being shown; give up on it after LOST_AFTER misses. */
    private void watch(int gen, String base, int misses, int ticks) {
        if (gen != generation) return;
        long serverTime = Finder.ping(base, config.probePath(), config.probeMarker(), 2500);
        boolean ok = serverTime >= 0;
        if (serverTime > 0) Kiosk.syncClock(this, serverTime);
        int missed = ok ? 0 : misses + 1;
        if (missed >= LOST_AFTER) {
            ui.post(() -> {
                if (gen == generation) startSearch(Finder.label(base) + " stopped answering");
            });
            return;
        }
        int tick = ticks + 1;
        if (ok && tick % PREFER_EVERY == 0) {
            String better = preferredOver(base);
            if (better != null) {
                ui.post(() -> { if (gen == generation) connect(better); });
                return;
            }
        }
        worker.postDelayed(() -> watch(gen, base, missed, tick), WATCH_EVERY_MS);
    }

    /**
     * Worker: a fixed server listed before the one in use that answers now,
     * e.g. the USB link coming back while the panel is shown over Wi-Fi.
     */
    private String preferredOver(String base) {
        List<String> candidates = config.candidates();
        int rank = candidates.indexOf(base);
        if (rank < 0) {                                         // base came from a sweep
            for (int i = 0; i < candidates.size(); i++) {
                if (Config.isLan(candidates.get(i))) { rank = i; break; }
            }
        }
        for (int i = 0; i < rank; i++) {
            String candidate = candidates.get(i);
            if (Config.isLan(candidate)) continue;
            if (Finder.probe(candidate, config.probePath(), config.probeMarker(), 1500)) return candidate;
        }
        return null;
    }

    // --- the web view -------------------------------------------------------------

    private WebView createWebView() {
        WebView view = new WebView(this);
        WebSettings s = view.getSettings();
        s.setJavaScriptEnabled(true);
        s.setDomStorageEnabled(true);          // the panel keeps its settings and token here
        s.setDatabaseEnabled(true);
        s.setMediaPlaybackRequiresUserGesture(false);   // alarms must be able to sound
        // Always fetch the panel fresh: after a deploy on the lab PC the next
        // load must not mix cached old modules with new ones.
        s.setCacheMode(WebSettings.LOAD_NO_CACHE);
        s.setTextZoom(config.textZoom());
        s.setSupportZoom(false);
        s.setBuiltInZoomControls(false);
        s.setDisplayZoomControls(false);
        s.setAllowFileAccess(false);
        s.setAllowContentAccess(false);
        s.setSupportMultipleWindows(false);
        // The lab is offline: Safe Browsing cannot reach Google, it only delays loads.
        s.setSafeBrowsingEnabled(false);

        view.setBackgroundColor(Color.BLACK);
        view.setOverScrollMode(View.OVER_SCROLL_NEVER);
        // No text-selection handles or context menu from a long press on a
        // touch panel; the page still gets every touch event.
        view.setLongClickable(false);
        view.setOnLongClickListener(v -> true);
        view.setHapticFeedbackEnabled(false);
        view.setRendererPriorityPolicy(WebView.RENDERER_PRIORITY_IMPORTANT, false);
        view.setWebViewClient(new Client());
        view.setWebChromeClient(new Chrome());
        view.setDownloadListener((url, agent, disposition, mime, length) ->
                Toast.makeText(this, "Exports cannot be saved on the tablet: download them from a computer.",
                        Toast.LENGTH_LONG).show());
        return view;
    }

    private final class Client extends WebViewClient {
        @Override
        public boolean shouldOverrideUrlLoading(WebView view, WebResourceRequest request) {
            String url = request.getUrl().toString();
            if (current != null && url.startsWith(current)) return false;
            Toast.makeText(MainActivity.this, "Only the control panel opens on this tablet.",
                    Toast.LENGTH_SHORT).show();
            return true;
        }

        @Override
        public void onPageFinished(WebView view, String url) {
            if (pageFailed || current == null || url == null || !url.startsWith(current)) return;
            hideCover();
            if (config.wakeAudio()) ui.postDelayed(MainActivity.this::wakeAudio, 1500);
        }

        @Override
        public void onReceivedError(WebView view, WebResourceRequest request, WebResourceError error) {
            if (!request.isForMainFrame()) return;
            pageFailed = true;
            String why = String.valueOf(error.getDescription());
            ui.post(() -> startSearch("Loading the panel failed: " + why));
        }

        @Override
        public void onReceivedHttpError(WebView view, WebResourceRequest request, WebResourceResponse response) {
            if (!request.isForMainFrame()) return;
            pageFailed = true;
            int status = response.getStatusCode();
            ui.post(() -> startSearch("The panel answered HTTP " + status));
        }

        /** The renderer died (on 1 GB it can be killed for memory): start a fresh one. */
        @Override
        public boolean onRenderProcessGone(WebView view, RenderProcessGoneDetail detail) {
            Log.w(TAG, "renderer gone, crashed=" + detail.didCrash());
            hideCustomView();
            root.removeView(web);
            web.destroy();
            web = createWebView();
            root.addView(web, 0, match());
            startSearch(detail.didCrash() ? "The page crashed" : "The page was stopped to free memory");
            return true;
        }
    }

    private final class Chrome extends WebChromeClient {
        /** Element full screen (the dashboard's wall mode). */
        @Override
        public void onShowCustomView(View view, CustomViewCallback callback) {
            if (customView != null) {
                callback.onCustomViewHidden();
                return;
            }
            customView = view;
            customCallback = callback;
            root.addView(view, match());
            hideSystemUi();
        }

        @Override
        public void onHideCustomView() {
            hideCustomView();
        }

        @Override
        public boolean onConsoleMessage(ConsoleMessage message) {
            Log.d(TAG, "console " + message.messageLevel() + ": " + message.message()
                    + " (" + message.sourceId() + ":" + message.lineNumber() + ")");
            return true;
        }
    }

    private void hideCustomView() {
        if (customView == null) return;
        root.removeView(customView);
        customView = null;
        if (customCallback != null) customCallback.onCustomViewHidden();
        customCallback = null;
    }

    /**
     * One touch, cancelled at once, in the panel's top-left corner. The panel
     * creates its alarm AudioContext on the first pointerdown; with no gesture
     * required for media in this WebView, that context then plays at once.
     * Cancelled, it is no tap, so nothing on the page is clicked.
     */
    private void wakeAudio() {
        if (current == null) return;
        long now = SystemClock.uptimeMillis();
        MotionEvent down = MotionEvent.obtain(now, now, MotionEvent.ACTION_DOWN, 2, 2, 0);
        MotionEvent cancel = MotionEvent.obtain(now, now + 30, MotionEvent.ACTION_CANCEL, 2, 2, 0);
        down.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        cancel.setSource(InputDevice.SOURCE_TOUCHSCREEN);
        web.dispatchTouchEvent(down);
        web.dispatchTouchEvent(cancel);
        down.recycle();
        cancel.recycle();
    }

    // --- the cover ------------------------------------------------------------------

    private LinearLayout createCover() {
        LinearLayout box = new LinearLayout(this);
        box.setOrientation(LinearLayout.VERTICAL);
        box.setGravity(Gravity.CENTER);
        box.setBackgroundColor(Color.rgb(16, 20, 24));
        int pad = dp(32);
        box.setPadding(pad, pad, pad, pad);
        // Swallow touches so nothing reaches a stale page underneath.
        box.setClickable(true);

        TextView title = text(30, Color.WHITE, Typeface.DEFAULT_BOLD);
        title.setText("Vacuum Lab");
        box.addView(title);

        coverStatus = text(20, Color.rgb(255, 200, 80), Typeface.DEFAULT);
        coverStatus.setPadding(0, dp(12), 0, dp(4));
        box.addView(coverStatus);

        coverDetail = text(14, Color.rgb(170, 180, 190), Typeface.MONOSPACE);
        coverDetail.setPadding(0, dp(12), 0, dp(24));
        box.addView(coverDetail);

        TextView hint = text(13, Color.rgb(120, 130, 140), Typeface.DEFAULT);
        hint.setText("USB: plug the tablet into the lab PC, which forwards the panel to it.\n"
                + "Wi-Fi: join the lab network from the admin menu "
                + "(tap the top-left corner " + CORNER_TAPS + " times).");
        box.addView(hint);
        return box;
    }

    private void showCover(String status, String detail) {
        coverStatus.setText(status);
        coverDetail.setText(detail);
        cover.setVisibility(View.VISIBLE);
    }

    private void hideCover() {
        cover.setVisibility(View.GONE);
    }

    // --- admin menu -----------------------------------------------------------------

    @Override
    public boolean dispatchTouchEvent(MotionEvent event) {
        if (event.getActionMasked() == MotionEvent.ACTION_DOWN
                && event.getX() < dp(72) && event.getY() < dp(72)) {
            long now = SystemClock.uptimeMillis();
            if (now - cornerStart > CORNER_WINDOW_MS) {
                cornerStart = now;
                cornerTaps = 0;
            }
            if (++cornerTaps >= CORNER_TAPS) {
                cornerTaps = 0;
                askPin();
            }
        }
        return super.dispatchTouchEvent(event);
    }

    private void askPin() {
        String pin = config.pin();
        if (pin.isEmpty()) {
            showAdminMenu();
            return;
        }
        EditText field = new EditText(this);
        field.setInputType(InputType.TYPE_CLASS_NUMBER | InputType.TYPE_NUMBER_VARIATION_PASSWORD);
        field.setHint("PIN");
        field.setImeOptions(EditorInfo.IME_ACTION_DONE);
        FrameLayout box = new FrameLayout(this);
        box.setPadding(dp(24), dp(8), dp(24), 0);
        box.addView(field);
        AlertDialog dialog = new AlertDialog.Builder(this)
                .setTitle("Kiosk admin")
                .setView(box)
                .setPositiveButton("Open", (d, w) -> {
                    if (pin.equals(field.getText().toString())) showAdminMenu();
                    else Toast.makeText(this, "Wrong PIN", Toast.LENGTH_SHORT).show();
                })
                .setNegativeButton("Cancel", null)
                .create();
        // The number pad covers the buttons on a 600 px tall screen: its
        // done key opens instead.
        field.setOnEditorActionListener((view, action, event) -> {
            dialog.getButton(AlertDialog.BUTTON_POSITIVE).performClick();
            return true;
        });
        showImmersive(dialog);
    }

    private void showAdminMenu() {
        String[] items = {
                "Reload the panel",
                "Search for the server again",
                "Wi-Fi settings",
                "Android settings",
                "Connection details",
        };
        showImmersive(new AlertDialog.Builder(this)
                .setTitle("Kiosk admin")
                .setItems(items, (d, which) -> {
                    switch (which) {
                        case 0:
                            if (current != null) connect(current);
                            else startSearch("Reload from the admin menu");
                            break;
                        case 1:
                            startSearch("Search restarted from the admin menu");
                            break;
                        case 2:
                            leaveFor(new Intent(Settings.ACTION_WIFI_SETTINGS));
                            break;
                        case 3:
                            leaveFor(new Intent(Settings.ACTION_SETTINGS));
                            break;
                        default:
                            showDetails();
                    }
                })
                .setNegativeButton("Close", null)
                .create());
    }

    /** Step out of lock task for maintenance; coming back home locks again (onResume). */
    private void leaveFor(Intent intent) {
        Kiosk.unlock(this);
        startActivity(intent.addFlags(Intent.FLAG_ACTIVITY_NEW_TASK));
    }

    private void showDetails() {
        StringBuilder out = new StringBuilder();
        try {
            PackageInfo self = getPackageManager().getPackageInfo(getPackageName(), 0);
            out.append("Kiosk ").append(self.versionName).append('\n');
        } catch (Exception ignored) {
            // Our own package is always there.
        }
        PackageInfo engine = WebView.getCurrentWebViewPackage();
        if (engine != null) out.append("Web engine: ").append(engine.packageName)
                .append(' ').append(engine.versionName).append('\n');
        out.append("Device owner: ").append(Kiosk.isOwner(this) ? "yes" : "no")
                .append(", locked: ").append(Kiosk.inLockTask(this) ? "yes" : "no").append("\n\n");
        out.append("Showing: ").append(current == null ? "nothing, searching" : current).append('\n');
        LinkAddress lan = Finder.lanAddress(this);
        out.append("Tablet on Wi-Fi: ").append(lan == null ? "no" : lan.toString()).append("\n\n");
        out.append(config.describe().replace("; ", "\n"));
        showImmersive(new AlertDialog.Builder(this)
                .setTitle("Connection details")
                .setMessage(out.toString())
                .setPositiveButton("Close", null)
                .create());
    }

    /**
     * A focusable dialog brings the system bars back. Showing it unfocusable
     * with the activity's flags, then making it focusable, keeps them hidden.
     */
    private void showImmersive(AlertDialog dialog) {
        Window window = dialog.getWindow();
        window.setFlags(WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE,
                WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE);
        dialog.show();
        window.getDecorView().setSystemUiVisibility(getWindow().getDecorView().getSystemUiVisibility());
        window.clearFlags(WindowManager.LayoutParams.FLAG_NOT_FOCUSABLE);
    }

    // --- window ---------------------------------------------------------------------

    private void hideSystemUi() {
        getWindow().getDecorView().setSystemUiVisibility(
                View.SYSTEM_UI_FLAG_IMMERSIVE_STICKY
                        | View.SYSTEM_UI_FLAG_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_FULLSCREEN
                        | View.SYSTEM_UI_FLAG_LAYOUT_STABLE
                        | View.SYSTEM_UI_FLAG_LAYOUT_HIDE_NAVIGATION
                        | View.SYSTEM_UI_FLAG_LAYOUT_FULLSCREEN);
    }

    private void applyOrientation() {
        String wanted = config.orientation();
        int value = "portrait".equals(wanted) ? ActivityInfo.SCREEN_ORIENTATION_SENSOR_PORTRAIT
                : "auto".equals(wanted) ? ActivityInfo.SCREEN_ORIENTATION_FULL_SENSOR
                : ActivityInfo.SCREEN_ORIENTATION_SENSOR_LANDSCAPE;
        if (getRequestedOrientation() != value) setRequestedOrientation(value);
    }

    /**
     * A full-screen window is not resized for the soft keyboard (adjustResize
     * is ignored with FLAG_FULLSCREEN), so a setpoint field low on the page
     * would be typed into blind. Shrink the content by whatever the keyboard
     * covers instead; the WebView then scrolls the focused field into view.
     */
    private void keepAboveKeyboard() {
        root.getViewTreeObserver().addOnGlobalLayoutListener(() -> {
            Rect visible = new Rect();
            root.getWindowVisibleDisplayFrame(visible);
            int full = root.getRootView().getHeight();
            int covered = full - visible.bottom;
            int pad = covered > full / 6 ? covered : 0;
            if (root.getPaddingBottom() != pad) root.setPadding(0, 0, 0, pad);
        });
    }

    // --- helpers --------------------------------------------------------------------

    private TextView text(float sp, int color, Typeface face) {
        TextView view = new TextView(this);
        view.setTextSize(TypedValue.COMPLEX_UNIT_SP, sp);
        view.setTextColor(color);
        view.setTypeface(face);
        view.setGravity(Gravity.CENTER);
        return view;
    }

    private int dp(float value) {
        return Math.round(TypedValue.applyDimension(TypedValue.COMPLEX_UNIT_DIP, value,
                getResources().getDisplayMetrics()));
    }

    private static FrameLayout.LayoutParams match() {
        return new FrameLayout.LayoutParams(ViewGroup.LayoutParams.MATCH_PARENT,
                ViewGroup.LayoutParams.MATCH_PARENT);
    }
}
