import { useEffect, useState } from 'react';
import { motion } from 'framer-motion';
import { ShieldCheck, ShieldAlert, RefreshCw } from 'lucide-react';
import FingerprintJS from '@fingerprintjs/fingerprintjs';
import { useVerifyDevice } from '@workspace/api-client-react';
import { Card } from '@/components/ui/card';

const getTelegramUserId = (): number | null => {
  try {
    const initDataUnsafe = (window as any).Telegram?.WebApp?.initDataUnsafe;
    if (initDataUnsafe?.user?.id) {
      return Number(initDataUnsafe.user.id);
    }
  } catch (e) {
    // Ignore error
  }
  return null;
};

type Status = 'checking' | 'success' | 'failed' | 'no-telegram';

/**
 * Opened from the bot's "🔒 Verify Device" button. Computes a stable
 * per-device fingerprint (screen, timezone, canvas/WebGL rendering, fonts,
 * etc — see @fingerprintjs/fingerprintjs) and asks the API server to bind it
 * to the caller's verified Telegram id. If another account already owns this
 * device, verification fails so the bot can refuse to release a referral
 * bonus for this account — stopping one person farming bonuses with many
 * Telegram accounts on the same phone/browser.
 */
export default function Verify() {
  const [status, setStatus] = useState<Status>('checking');
  const verifyMutation = useVerifyDevice();

  useEffect(() => {
    try {
      (window as any).Telegram?.WebApp?.ready();
      (window as any).Telegram?.WebApp?.expand();
    } catch (e) {
      // Ignore error
    }

    const telegramUserId = getTelegramUserId();
    if (!telegramUserId) {
      setStatus('no-telegram');
      return;
    }

    (async () => {
      try {
        const fp = await FingerprintJS.load();
        const result = await fp.get();
        verifyMutation.mutate(
          { data: { fingerprint: result.visitorId } },
          {
            onSuccess: (res) => setStatus(res.verified ? 'success' : 'failed'),
            onError: () => setStatus('failed'),
          },
        );
      } catch (e) {
        setStatus('failed');
      }
    })();
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, []);

  return (
    <div className="min-h-screen bg-background flex items-center justify-center p-6">
      <motion.div
        initial={{ opacity: 0, y: 10 }}
        animate={{ opacity: 1, y: 0 }}
        className="w-full max-w-sm"
      >
        <Card className="p-8 flex flex-col items-center text-center gap-4 bg-white dark:bg-card">
          {status === 'checking' && (
            <>
              <RefreshCw className="h-12 w-12 text-primary animate-spin" />
              <h2 className="text-xl font-bold text-foreground">Verifying device...</h2>
              <p className="text-muted-foreground text-sm">
                Please wait while we confirm this device.
              </p>
            </>
          )}

          {status === 'success' && (
            <>
              <ShieldCheck className="h-14 w-14 text-success" />
              <h2 className="text-xl font-bold text-foreground">Verification successful</h2>
              <p className="text-muted-foreground text-sm">
                Your device has been verified. You can go back to the bot now — any pending bonus will be credited shortly.
              </p>
            </>
          )}

          {status === 'failed' && (
            <>
              <ShieldAlert className="h-14 w-14 text-destructive" />
              <h2 className="text-xl font-bold text-foreground">Verification failed</h2>
              <p className="text-muted-foreground text-sm">
                This device is already linked to another account. You can't create multiple accounts on the same device.
              </p>
            </>
          )}

          {status === 'no-telegram' && (
            <>
              <ShieldAlert className="h-14 w-14 text-muted-foreground" />
              <h2 className="text-xl font-bold text-foreground">Open from Telegram</h2>
              <p className="text-muted-foreground text-sm">
                We couldn't detect your Telegram account. Please open this page using the "Verify Device" button inside the bot.
              </p>
            </>
          )}
        </Card>
      </motion.div>
    </div>
  );
}
