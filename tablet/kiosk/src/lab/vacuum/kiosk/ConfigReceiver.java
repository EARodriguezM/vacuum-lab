package lab.vacuum.kiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;
import android.content.SharedPreferences;

/**
 * Configuration over adb, for example:
 *
 *   adb shell am broadcast -n lab.vacuum.kiosk/.ConfigReceiver \
 *       -a lab.vacuum.kiosk.CONFIGURE \
 *       --es urls "http://127.0.0.1:8600, http://172.16.2.69:8600, lan" \
 *       --es pin 482613
 *
 * Extras: urls, pin, lan_port (int), text_zoom (int), orientation
 * (landscape|portrait|auto), wake_audio (bool), probe_path, probe_marker,
 * timezone (e.g. America/Bogota; device owner only),
 * reset (bool: back to defaults first), release (bool: stop being device
 * owner so the app can be uninstalled). The reply, printed by `am broadcast`,
 * is the resulting configuration.
 *
 * Only the adb shell can send this: the manifest requires DUMP.
 */
public final class ConfigReceiver extends BroadcastReceiver {

    @Override
    public void onReceive(Context context, Intent intent) {
        Config config = new Config(context);

        if (intent.getBooleanExtra("release", false)) {
            Kiosk.release(context);
            setResultData("released: no longer device owner; the app can be uninstalled");
            return;
        }
        if (intent.getBooleanExtra("reset", false)) config.reset();
        if (intent.hasExtra("timezone")) Kiosk.setTimeZone(context, intent.getStringExtra("timezone"));

        SharedPreferences.Editor edit = config.edit();
        String[] strings = {"urls", "pin", "orientation", "probe_path", "probe_marker"};
        for (String key : strings) {
            if (intent.hasExtra(key)) edit.putString(key, intent.getStringExtra(key).trim());
        }
        if (intent.hasExtra("lan_port")) edit.putInt("lan_port", intent.getIntExtra("lan_port", 8600));
        if (intent.hasExtra("text_zoom")) edit.putInt("text_zoom", intent.getIntExtra("text_zoom", 100));
        if (intent.hasExtra("wake_audio")) edit.putBoolean("wake_audio", intent.getBooleanExtra("wake_audio", true));
        // A changed server list makes the remembered Wi-Fi find meaningless.
        if (intent.hasExtra("urls")) edit.remove("last_lan");
        edit.commit();

        // Restart the search with the new settings.
        context.startActivity(new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK)
                .putExtra(MainActivity.EXTRA_RECONFIGURE, true));

        setResultData((Kiosk.isOwner(context) ? "device owner; " : "not device owner; ")
                + config.describe());
    }
}
