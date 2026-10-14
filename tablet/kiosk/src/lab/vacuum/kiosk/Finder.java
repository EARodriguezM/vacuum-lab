package lab.vacuum.kiosk;

import android.content.Context;
import android.net.ConnectivityManager;
import android.net.LinkAddress;
import android.net.LinkProperties;
import android.net.Network;
import android.net.NetworkCapabilities;

import java.io.ByteArrayOutputStream;
import java.io.InputStream;
import java.net.HttpURLConnection;
import java.net.Inet4Address;
import java.net.InetAddress;
import java.net.InetSocketAddress;
import java.net.Socket;
import java.net.URL;
import java.util.ArrayList;
import java.util.List;
import java.util.concurrent.ExecutorService;
import java.util.concurrent.Executors;
import java.util.concurrent.Future;
import java.util.concurrent.TimeUnit;

/**
 * Finding the panel: probe a known address, or sweep the Wi-Fi subnet.
 *
 * All of it blocks, so it runs on the activity's worker thread, never the UI
 * thread.
 */
final class Finder {

    private Finder() {}

    /** The panel answers its ping with the marker text: anything else is not it. */
    static boolean probe(String base, String path, String marker, int timeoutMs) {
        return ping(base, path, marker, timeoutMs) >= 0;
    }

    /**
     * Like probe, but returns the server's clock from its Date header, in ms:
     * -1 when the panel did not answer, 0 when it answered without a date.
     * The lab is offline, so the lab PC is the only clock the tablet can
     * agree with -- and the panel's own timestamps come from it.
     */
    static long ping(String base, String path, String marker, int timeoutMs) {
        HttpURLConnection connection = null;
        try {
            connection = (HttpURLConnection) new URL(base + path).openConnection();
            connection.setConnectTimeout(timeoutMs);
            connection.setReadTimeout(timeoutMs);
            connection.setUseCaches(false);
            if (connection.getResponseCode() != 200) return -1;
            try (InputStream in = connection.getInputStream()) {
                ByteArrayOutputStream body = new ByteArrayOutputStream();
                byte[] buffer = new byte[2048];
                int n;
                // The ping reply is small; a cap keeps a wrong service from
                // streaming something huge at us.
                while ((n = in.read(buffer)) > 0 && body.size() < 65536) body.write(buffer, 0, n);
                if (!body.toString("UTF-8").contains(marker)) return -1;
            }
            return Math.max(connection.getHeaderFieldDate("Date", 0), 0);
        } catch (Exception e) {
            return -1;
        } finally {
            if (connection != null) connection.disconnect();
        }
    }

    /** The tablet's IPv4 address on Wi-Fi or Ethernet, with its prefix length; null if none. */
    static LinkAddress lanAddress(Context context) {
        ConnectivityManager cm = context.getSystemService(ConnectivityManager.class);
        if (cm == null) return null;
        for (Network network : cm.getAllNetworks()) {
            NetworkCapabilities caps = cm.getNetworkCapabilities(network);
            if (caps == null) continue;
            if (!caps.hasTransport(NetworkCapabilities.TRANSPORT_WIFI)
                    && !caps.hasTransport(NetworkCapabilities.TRANSPORT_ETHERNET)) continue;
            LinkProperties props = cm.getLinkProperties(network);
            if (props == null) continue;
            for (LinkAddress address : props.getLinkAddresses()) {
                if (address.getAddress() instanceof Inet4Address) return address;
            }
        }
        return null;
    }

    /**
     * Sweep the /24 the tablet is on for the panel's port, then confirm each
     * open port with the ping. A wider subnet is still swept as the /24 around
     * the tablet: 65 000 hosts is minutes of traffic, and the lab PC has always
     * been on the same /24 as the lab's Wi-Fi clients when they share a network.
     *
     * Returns the base URL, or null.
     */
    static String scanLan(LinkAddress self, int port, String path, String marker) {
        return scanLan(prefix(self), self.getAddress().getHostAddress(), port, path, marker);
    }

    /** The same sweep over prefix.1 to prefix.254 ("a.b.c"), skipping `skip`. */
    static String scanLan(String prefix, String skip, int port, String path, String marker) {
        List<String> hosts = new ArrayList<>(254);
        for (int last = 1; last < 255; last++) {
            String host = prefix + "." + last;
            if (host.equals(skip)) continue;
            hosts.add(host);
        }
        ExecutorService pool = Executors.newFixedThreadPool(48);
        try {
            List<Future<String>> results = new ArrayList<>(hosts.size());
            for (String host : hosts) {
                results.add(pool.submit(() -> {
                    try (Socket socket = new Socket()) {
                        socket.connect(new InetSocketAddress(InetAddress.getByName(host), port), 350);
                    } catch (Exception e) {
                        return null;
                    }
                    String base = "http://" + host + ":" + port;
                    return probe(base, path, marker, 1500) ? base : null;
                }));
            }
            for (Future<String> result : results) {
                String base = result.get(10, TimeUnit.SECONDS);
                if (base != null) return base;
            }
        } catch (Exception e) {
            return null;
        } finally {
            pool.shutdownNow();
        }
        return null;
    }

    /** "a.b.c.0/24" for the sweep above, for the waiting screen. */
    static String lanRange(LinkAddress self) {
        return prefix(self) + ".0/24";
    }

    /** "a.b.c" of the tablet's own address. */
    static String prefix(LinkAddress self) {
        byte[] own = self.getAddress().getAddress();
        return (own[0] & 0xff) + "." + (own[1] & 0xff) + "." + (own[2] & 0xff);
    }

    /** "USB link" for the loopback the lab PC forwards, otherwise host:port. */
    static String label(String base) {
        String host = base.replaceFirst("(?i)^https?://", "");
        if (host.startsWith("127.0.0.1") || host.startsWith("localhost")) return "USB link (" + host + ")";
        return host;
    }
}
