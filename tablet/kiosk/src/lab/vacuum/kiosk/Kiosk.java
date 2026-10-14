package lab.vacuum.kiosk;

import android.app.Activity;
import android.app.ActivityManager;
import android.app.admin.DevicePolicyManager;
import android.content.ComponentName;
import android.content.Context;
import android.content.Intent;
import android.content.IntentFilter;
import android.os.UserManager;
import android.provider.Settings;
import android.util.Log;

/**
 * Device-owner policy. Every call here is a no-op unless provision.sh made
 * this app the device owner, so the same APK can be tried on a tablet that
 * still has an account on it: it then runs as an ordinary home screen.
 */
final class Kiosk {

    static final String TAG = "VacuumKiosk";

    private Kiosk() {}

    static ComponentName admin(Context context) {
        return new ComponentName(context, AdminReceiver.class);
    }

    static boolean isOwner(Context context) {
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        return dpm != null && dpm.isDeviceOwnerApp(context.getPackageName());
    }

    static boolean inLockTask(Context context) {
        ActivityManager am = context.getSystemService(ActivityManager.class);
        return am != null && am.getLockTaskModeState() != ActivityManager.LOCK_TASK_MODE_NONE;
    }

    /** Idempotent: run on every resume, so a policy lost to an update comes back. */
    static void applyPolicies(Context context) {
        if (!isOwner(context)) return;
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        ComponentName admin = admin(context);
        try {
            dpm.setLockTaskPackages(admin, new String[] {context.getPackageName()});
            // Nothing but the panel: no home, recents, notifications or status
            // bar info. The power menu stays, so the tablet can still be shut
            // down properly from the screen.
            dpm.setLockTaskFeatures(admin, DevicePolicyManager.LOCK_TASK_FEATURE_GLOBAL_ACTIONS);
            dpm.setStatusBarDisabled(admin, true);
            // Only takes effect while no screen lock is set, which is how the
            // tablet is provisioned: a lock screen in front of a control panel
            // only slows down whoever has to stop the pump.
            dpm.setKeyguardDisabled(admin, true);
            // No "... keeps stopping" or "isn't responding" dialog over the
            // panel: Android closes the app instead. A system app crashing
            // in the background (the setup wizard, once Play services was
            // removed) covered the rig's readings until somebody tapped it
            // away. If the kiosk itself stops answering it is restarted, as
            // the home screen, rather than frozen behind a dialog.
            dpm.addUserRestriction(admin, UserManager.DISALLOW_SYSTEM_ERROR_DIALOGS);

            IntentFilter home = new IntentFilter(Intent.ACTION_MAIN);
            home.addCategory(Intent.CATEGORY_HOME);
            home.addCategory(Intent.CATEGORY_DEFAULT);
            dpm.addPersistentPreferredActivity(admin, home,
                    new ComponentName(context, MainActivity.class));

            // AC, USB and wireless: the screen never sleeps while powered.
            dpm.setGlobalSetting(admin, Settings.Global.STAY_ON_WHILE_PLUGGED_IN, "7");
            // The clock comes from the lab PC (syncClock), and setTime is
            // refused while network time is on. Offline, network time never
            // arrives anyway: this tablet booted believing it was 2009.
            dpm.setGlobalSetting(admin, Settings.Global.AUTO_TIME, "0");
        } catch (RuntimeException e) {
            Log.w(TAG, "applying device-owner policy failed", e);
        }
    }

    /**
     * Set the tablet's clock to the lab PC's when they disagree by more than
     * a few seconds. The panel measures alarm ages and operator waits with
     * the browser clock, so a tablet years off shows them as nonsense.
     * The Date header has 1 s resolution: 5 s of slack stops it chasing that.
     */
    static void syncClock(Context context, long serverMillis) {
        if (Math.abs(System.currentTimeMillis() - serverMillis) < 5000) return;
        if (!isOwner(context)) return;
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        try {
            boolean set = dpm.setTime(admin(context), serverMillis);
            Log.i(TAG, "clock " + (set ? "set" : "NOT set") + " from the lab PC: " + serverMillis);
        } catch (RuntimeException e) {
            Log.w(TAG, "setting the clock failed", e);
        }
    }

    /** Device-owner time zone, from provisioning (the zone of the computer that ran it). */
    static void setTimeZone(Context context, String zone) {
        if (!isOwner(context) || zone == null || zone.isEmpty()) return;
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        try {
            dpm.setGlobalSetting(admin(context), Settings.Global.AUTO_TIME_ZONE, "0");
            dpm.setTimeZone(admin(context), zone);
        } catch (RuntimeException e) {
            Log.w(TAG, "setting the time zone failed", e);
        }
    }

    static void lock(Activity activity) {
        if (!isOwner(activity) || inLockTask(activity)) return;
        try {
            activity.startLockTask();
        } catch (RuntimeException e) {
            Log.w(TAG, "startLockTask failed", e);
        }
    }

    static void unlock(Activity activity) {
        if (!inLockTask(activity)) return;
        try {
            activity.stopLockTask();
        } catch (RuntimeException e) {
            Log.w(TAG, "stopLockTask failed", e);
        }
    }

    /**
     * Undo everything, so the app can be uninstalled: a device owner cannot be
     * removed any other way short of a factory reset.
     */
    @SuppressWarnings("deprecation")   // the only way a device owner can step down on API 28
    static void release(Context context) {
        if (!isOwner(context)) return;
        DevicePolicyManager dpm = context.getSystemService(DevicePolicyManager.class);
        ComponentName admin = admin(context);
        try {
            dpm.clearPackagePersistentPreferredActivities(admin, context.getPackageName());
            dpm.setStatusBarDisabled(admin, false);
            dpm.setKeyguardDisabled(admin, false);
            dpm.clearUserRestriction(admin, UserManager.DISALLOW_SYSTEM_ERROR_DIALOGS);
            dpm.setLockTaskPackages(admin, new String[0]);
        } catch (RuntimeException e) {
            Log.w(TAG, "clearing device-owner policy failed", e);
        }
        dpm.clearDeviceOwnerApp(context.getPackageName());
    }
}
