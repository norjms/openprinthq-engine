/**
 * Alarm feed: what every printer is reporting right now (GET /alarms), with history and
 * acknowledgement. Backend: services/alarm_feed.py, docs/alarm-feed.md.
 */
import { useMemo, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { AlertOctagon, CheckCircle } from 'lucide-react';
import { api, type PrinterAlarm } from '../api/client';
import { useAuth } from '../contexts/AuthContext';
import { useToast } from '../contexts/ToastContext';
import { formatRelativeTime } from '../utils/date';

const SEVERITY_CLASS: Record<string, string> = {
  fatal: 'bg-red-500/20 text-red-400 border-red-500/40',
  serious: 'bg-orange-500/20 text-orange-400 border-orange-500/40',
  warning: 'bg-yellow-500/20 text-yellow-400 border-yellow-500/40',
};

export function AlarmsPage() {
  const { t } = useTranslation();
  const { hasPermission } = useAuth();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [history, setHistory] = useState(false);
  const [printerId, setPrinterId] = useState<number | null>(null);
  const canAck = hasPermission('printers:control');

  const { data: alarms, isLoading, isError } = useQuery({
    queryKey: ['alarms', history, printerId],
    queryFn: () => api.getAlarms({ state: history ? 'all' : 'active', printerId }),
    refetchInterval: 30000,
  });

  const printers = useMemo(() => {
    const seen = new Map<number, string>();
    for (const a of alarms ?? []) seen.set(a.printer_id, a.printer_name ?? `#${a.printer_id}`);
    return [...seen.entries()].sort((x, y) => x[1].localeCompare(y[1]));
  }, [alarms]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['alarms'] });
    queryClient.invalidateQueries({ queryKey: ['alarmSummary'] });
  };
  const ackOne = useMutation({
    mutationFn: (id: number) => api.ackAlarm(id),
    onSuccess: () => {
      showToast(t('alarms.acknowledged'));
      refresh();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });
  const ackPrinter = useMutation({
    mutationFn: (id: number) => api.ackPrinterAlarms(id),
    onSuccess: () => {
      showToast(t('alarms.acknowledged'));
      refresh();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  return (
    <div className="p-4 md:p-8">
      <div className="mb-6">
        <h1 className="text-2xl font-bold text-white flex items-center gap-3">
          <AlertOctagon className="w-7 h-7 text-bambu-green" />
          {t('alarms.title')}
        </h1>
        <p className="text-bambu-gray mt-1">{t('alarms.subtitle')}</p>
      </div>

      <div className="flex flex-wrap items-center gap-3 mb-4 text-sm">
        <label className="flex items-center gap-2 text-bambu-gray">
          <input type="checkbox" checked={history} onChange={(e) => setHistory(e.target.checked)} />
          {t('alarms.includeCleared')}
        </label>
        <select
          value={printerId ?? ''}
          onChange={(e) => setPrinterId(e.target.value ? Number(e.target.value) : null)}
          className="px-3 py-1.5 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-white"
          aria-label={t('alarms.allPrinters')}
        >
          <option value="">{t('alarms.allPrinters')}</option>
          {printers.map(([id, name]) => (
            <option key={id} value={id}>
              {name}
            </option>
          ))}
        </select>
        {printerId !== null && canAck && (
          <button
            type="button"
            onClick={() => ackPrinter.mutate(printerId)}
            disabled={ackPrinter.isPending}
            className="px-3 py-1.5 rounded-lg bg-bambu-dark-tertiary text-white hover:bg-bambu-dark-secondary disabled:opacity-50"
          >
            {t('alarms.acknowledgeAll')}
          </button>
        )}
      </div>

      {isLoading ? (
        <p className="text-bambu-gray">{t('common.loading')}</p>
      ) : isError ? (
        <p className="text-red-400">{t('alarms.loadFailed')}</p>
      ) : !alarms?.length ? (
        <div className="bg-bambu-dark-secondary rounded-lg p-8 text-center text-bambu-gray border border-bambu-dark-tertiary">
          <CheckCircle className="w-8 h-8 mx-auto mb-2 text-bambu-green" />
          {history ? t('alarms.noneRecorded') : t('alarms.none')}
        </div>
      ) : (
        <div className="space-y-2" data-testid="alarm-list">
          {alarms.map((a: PrinterAlarm) => (
            <div
              key={a.id}
              className={`bg-bambu-dark-secondary rounded-lg p-3 border border-bambu-dark-tertiary flex flex-wrap items-start gap-3 ${
                a.active ? '' : 'opacity-60'
              }`}
            >
              <span
                className={`px-2 py-0.5 rounded border text-xs font-medium uppercase ${
                  a.active ? SEVERITY_CLASS[a.severity] ?? '' : 'text-bambu-gray border-bambu-dark-tertiary'
                }`}
              >
                {t(`alarms.severity.${a.severity}`, a.severity)}
              </span>
              <div className="flex-1 min-w-[12rem]">
                <p className="text-white">
                  <span className="font-medium">{a.printer_name ?? `#${a.printer_id}`}</span> · {a.message}
                </p>
                <p className="text-xs text-bambu-gray font-mono">
                  {a.source} {a.code}
                </p>
                <p className="text-xs text-bambu-gray">
                  {t('alarms.since', { when: formatRelativeTime(a.first_seen, 'system', t) })}
                  {!a.active && a.cleared_at && ` · ${t('alarms.clearedAt', { when: formatRelativeTime(a.cleared_at, 'system', t) })}`}
                </p>
              </div>
              {a.acknowledged_at ? (
                <span className="text-xs text-bambu-gray self-center">
                  {t('alarms.seenBy', { name: a.acknowledged_by || '-' })}
                </span>
              ) : (
                canAck && (
                  <button
                    type="button"
                    onClick={() => ackOne.mutate(a.id)}
                    disabled={ackOne.isPending}
                    className="px-3 py-1.5 rounded-lg bg-bambu-green text-white text-sm hover:bg-bambu-green-dark disabled:opacity-50 self-center"
                  >
                    {t('alarms.acknowledge')}
                  </button>
                )
              )}
            </div>
          ))}
        </div>
      )}
    </div>
  );
}
