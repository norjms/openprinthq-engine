import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Bell, CheckCircle, Loader2 } from 'lucide-react';
import { api } from '../api/client';
import { Button } from './Button';

function urlBase64ToUint8Array(base64: string): Uint8Array<ArrayBuffer> {
  const padded = (base64 + '='.repeat((4 - (base64.length % 4)) % 4)).replace(/-/g, '+').replace(/_/g, '/');
  const raw = atob(padded);
  const out = new Uint8Array(new ArrayBuffer(raw.length));
  for (let i = 0; i < raw.length; i++) out[i] = raw.charCodeAt(i);
  return out;
}

const supported = () =>
  typeof window !== 'undefined' && window.isSecureContext && 'serviceWorker' in navigator && 'PushManager' in window;

/** Subscribes the current browser to the server's web push (provider type "webpush"). */
export function WebPushSubscribe() {
  const { t } = useTranslation();
  const [subscribed, setSubscribed] = useState(false);
  const [busy, setBusy] = useState(false);
  const [error, setError] = useState<string | null>(null);

  useEffect(() => {
    if (!supported()) return;
    navigator.serviceWorker.ready
      .then((reg) => reg.pushManager.getSubscription())
      .then((sub) => setSubscribed(!!sub))
      .catch(() => undefined);
  }, []);

  if (!supported()) {
    return <p className="text-xs text-bambu-gray">{t('notifications.webpushUnsupported')}</p>;
  }

  const subscribe = async () => {
    setBusy(true);
    setError(null);
    try {
      if ((await Notification.requestPermission()) !== 'granted') {
        setError(t('notifications.webpushDenied'));
        return;
      }
      const { public_key } = await api.getPushPublicKey();
      const reg = await navigator.serviceWorker.ready;
      const sub =
        (await reg.pushManager.getSubscription()) ??
        (await reg.pushManager.subscribe({ userVisibleOnly: true, applicationServerKey: urlBase64ToUint8Array(public_key) }));
      const json = sub.toJSON();
      await api.subscribePush({
        endpoint: sub.endpoint,
        keys: { p256dh: json.keys?.p256dh ?? '', auth: json.keys?.auth ?? '' },
      });
      setSubscribed(true);
    } catch (e) {
      setError(e instanceof Error ? e.message : String(e));
    } finally {
      setBusy(false);
    }
  };

  return (
    <div className="space-y-1">
      {subscribed ? (
        <p className="flex items-center gap-2 text-sm text-bambu-green">
          <CheckCircle className="w-4 h-4" />
          {t('notifications.webpushSubscribed')}
        </p>
      ) : (
        <Button type="button" variant="secondary" onClick={subscribe} disabled={busy}>
          {busy ? <Loader2 className="w-4 h-4 animate-spin" /> : <Bell className="w-4 h-4" />}
          {t('notifications.webpushSubscribe')}
        </Button>
      )}
      {error && <p className="text-xs text-red-400">{error}</p>}
    </div>
  );
}
