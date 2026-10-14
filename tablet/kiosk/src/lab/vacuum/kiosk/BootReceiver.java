package lab.vacuum.kiosk;

import android.content.BroadcastReceiver;
import android.content.Context;
import android.content.Intent;

/**
 * Back to the panel after a boot or an `adb install -r` update. Being the
 * home screen covers the boot already; an update kills the running activity
 * and nothing else would bring it back until somebody touched the tablet.
 */
public final class BootReceiver extends BroadcastReceiver {
    @Override
    public void onReceive(Context context, Intent intent) {
        Intent start = new Intent(context, MainActivity.class)
                .addFlags(Intent.FLAG_ACTIVITY_NEW_TASK);
        context.startActivity(start);
    }
}
