package lab.vacuum.kiosk;

import android.content.Context;
import android.content.SharedPreferences;

import java.util.ArrayList;
import java.util.List;
import java.util.Locale;

/**
 * Everything the kiosk can be told, kept in SharedPreferences.
 *
 * Set over adb with ConfigReceiver (see provision.sh); there is deliberately
 * no on-screen editor for the server list, so an operator cannot point the
 * lab's control tablet somewhere else by accident.
 */
final class Config {

    /**
     * The keyword that means "search the Wi-Fi subnet for the panel": `lan`
     * alone sweeps the /24 the tablet is on; `lan:a.b.c` sweeps a.b.c.0/24
     * instead -- for a lab PC on another subnet than the tablet's Wi-Fi.
     */
    static final String LAN = "lan";

    /** Whether a candidate is a Wi-Fi search, of either kind. */
    static boolean isLan(String candidate) {
        return candidate.equalsIgnoreCase(LAN)
                || candidate.matches("(?i)^lan:\\d{1,3}\\.\\d{1,3}\\.\\d{1,3}(\\.0/24)?$");
    }

    /** "a.b.c" for `lan:a.b.c`, or null for the tablet's own subnet. */
    static String lanSubnet(String candidate) {
        if (candidate.equalsIgnoreCase(LAN)) return null;
        return candidate.substring(4).replaceFirst("\\.0/24$", "");
    }

    /**
     * USB first: the lab PC runs `adb reverse tcp:8600 tcp:8600`, so the
     * panel is on the tablet's own loopback while the cable is in. Then the
     * Wi-Fi search, because the lab PC's address moves on its own.
     */
    static final String DEFAULT_URLS = "http://127.0.0.1:8600, " + LAN;

    private final SharedPreferences prefs;

    Config(Context context) {
        prefs = context.getSharedPreferences("kiosk", Context.MODE_PRIVATE);
    }

    String urls() { return prefs.getString("urls", DEFAULT_URLS); }

    /** Server candidates in priority order: base URLs, and LAN for the search. */
    List<String> candidates() {
        List<String> out = new ArrayList<>();
        for (String raw : urls().split("[,\\s]+")) {
            String text = raw.trim();
            if (text.isEmpty()) continue;
            if (isLan(text)) {
                out.add(text.toLowerCase(java.util.Locale.ROOT).replaceFirst("\\.0/24$", ""));
                continue;
            }
            if (!text.matches("(?i)^https?://.*")) text = "http://" + text;
            while (text.endsWith("/")) text = text.substring(0, text.length() - 1);
            // A bare host means the panel's default port, as the panel's own
            // settings page does.
            if (!text.substring(text.indexOf("://") + 3).contains(":")) text += ":" + lanPort();
            out.add(text);
        }
        return out;
    }

    int lanPort() { return prefs.getInt("lan_port", 8600); }

    /** Where the Wi-Fi search last found the panel: tried before scanning. */
    String lastLan() { return prefs.getString("last_lan", ""); }

    void setLastLan(String base) { prefs.edit().putString("last_lan", base).apply(); }

    /** Admin menu PIN. Empty means the corner taps open the menu directly. */
    String pin() { return prefs.getString("pin", ""); }

    /**
     * WebView text zoom in percent. 100 renders the panel exactly as its CSS
     * says; left alone, WebView would follow the system font scale instead.
     */
    int textZoom() { return prefs.getInt("text_zoom", 100); }

    /** landscape, portrait or auto. */
    String orientation() { return prefs.getString("orientation", "landscape"); }

    /**
     * Send one cancelled touch after every page load. The panel only creates
     * its alarm AudioContext on the first pointerdown, so without this an
     * alarm after a reconnect is silent until somebody touches the screen.
     */
    boolean wakeAudio() { return prefs.getBoolean("wake_audio", true); }

    /** The unauthenticated endpoint used to recognise the panel. */
    String probePath() { return prefs.getString("probe_path", "/api/ping"); }

    /** Text the probe's reply must contain, so another service on 8600 is not mistaken for the panel. */
    String probeMarker() { return prefs.getString("probe_marker", "vacuum-lab"); }

    SharedPreferences.Editor edit() { return prefs.edit(); }

    void reset() { prefs.edit().clear().apply(); }

    String describe() {
        return String.format(Locale.ROOT,
                "urls=%s; lan_port=%d; last_lan=%s; pin=%s; text_zoom=%d; orientation=%s; "
                        + "wake_audio=%b; probe=%s contains '%s'",
                urls(), lanPort(), lastLan().isEmpty() ? "-" : lastLan(),
                pin().isEmpty() ? "none" : "set", textZoom(), orientation(), wakeAudio(),
                probePath(), probeMarker());
    }
}
