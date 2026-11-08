import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { Button } from './Button';

// Technical examples, not prose: the same in every locale.
const WHEP_PLACEHOLDER = 'http://go2rtc.local:1984/api/webrtc?src=printer';
const GO2RTC_PLACEHOLDER = 'http://go2rtc.local:1984';
const STREAM_PLACEHOLDER = 'printer';

interface WebRtcCameraSettingsProps {
  printerId: number;
  webrtcUrl: string | null | undefined;
  disabled?: boolean;
  /** Save the WHEP URL (null clears it). */
  onSaveUrl: (printerId: number, url: string | null) => void;
  /** Expand a go2rtc base URL + stream name into the camera URLs server-side. */
  onApplyGo2rtc: (printerId: number, baseUrl: string, stream: string) => void;
}

/**
 * Per-printer WebRTC (WHEP) camera settings (#56). Shown whether or not the
 * external camera is on: a WHEP URL can also front a printer's own camera
 * (go2rtc ingesting the Bambu RTSP feed), with the built-in stream as the
 * fallback.
 */
export function WebRtcCameraSettings({ printerId, webrtcUrl, disabled, onSaveUrl, onApplyGo2rtc }: WebRtcCameraSettingsProps) {
  const { t } = useTranslation();
  const [draft, setDraft] = useState<string | null>(null);
  const [base, setBase] = useState('');
  const [stream, setStream] = useState('');
  const value = draft ?? webrtcUrl ?? '';

  const commit = () => {
    if (draft === null) return;
    const next = draft.trim();
    setDraft(null);
    if (next === (webrtcUrl ?? '')) return;
    onSaveUrl(printerId, next || null);
  };

  return (
    <div className="space-y-2 mt-2" data-testid={`webrtc-settings-${printerId}`}>
      <div className="space-y-1">
        <label className="text-xs text-bambu-gray" htmlFor={`webrtc-url-${printerId}`}>
          {t('settings.webrtcCamera.url')}
        </label>
        <input
          id={`webrtc-url-${printerId}`}
          type="text"
          placeholder={WHEP_PLACEHOLDER}
          value={value}
          disabled={disabled}
          onChange={(e) => setDraft(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => {
            if (e.key === 'Enter') commit();
          }}
          className="w-full px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:border-bambu-green focus:outline-none"
        />
        <p className="text-xs text-bambu-gray opacity-75">{t('settings.webrtcCamera.help')}</p>
      </div>
      <details className="text-xs">
        <summary className="cursor-pointer text-bambu-gray hover:text-white">{t('settings.webrtcCamera.go2rtcTitle')}</summary>
        <div className="mt-2 space-y-2">
          <div className="flex flex-col gap-2 sm:flex-row">
            <input
              type="text"
              aria-label={t('settings.webrtcCamera.go2rtcUrl')}
              placeholder={GO2RTC_PLACEHOLDER}
              value={base}
              disabled={disabled}
              onChange={(e) => setBase(e.target.value)}
              className="flex-1 px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:border-bambu-green focus:outline-none"
            />
            <input
              type="text"
              aria-label={t('settings.webrtcCamera.go2rtcStream')}
              placeholder={STREAM_PLACEHOLDER}
              value={stream}
              disabled={disabled}
              onChange={(e) => setStream(e.target.value)}
              className="sm:w-40 px-3 py-2 bg-bambu-dark-secondary border border-bambu-dark-tertiary rounded text-white text-sm focus:border-bambu-green focus:outline-none"
            />
            <Button
              size="sm"
              variant="secondary"
              disabled={disabled || !base.trim() || !stream.trim()}
              onClick={() => onApplyGo2rtc(printerId, base.trim(), stream.trim())}
            >
              {t('settings.webrtcCamera.go2rtcApply')}
            </Button>
          </div>
          <p className="text-bambu-gray opacity-75">{t('settings.webrtcCamera.go2rtcHelp')}</p>
        </div>
      </details>
    </div>
  );
}
