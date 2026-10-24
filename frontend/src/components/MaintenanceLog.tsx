import { useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { ClipboardList, Plus, Trash2 } from 'lucide-react';
import { api, MAINTENANCE_LOG_CATEGORIES } from '../api/client';
import type { Permission } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

/**
 * A printer's maintenance history: schedules marked done and free-form entries
 * (work with no schedule), plus a form to log one. Free-form entries can be
 * deleted; rows recorded by marking a schedule done cannot.
 */
export function MaintenanceLog({
  printerId,
  hasPermission,
}: {
  printerId: number;
  hasPermission: (permission: Permission) => boolean;
}) {
  const { t } = useTranslation();
  const { showToast } = useToast();
  const queryClient = useQueryClient();
  const [adding, setAdding] = useState(false);
  const [title, setTitle] = useState('');
  const [category, setCategory] = useState<string>('inspection');
  const [notes, setNotes] = useState('');
  const queryKey = ['maintenanceLog', printerId];

  const { data: entries = [] } = useQuery({
    queryKey,
    queryFn: () => api.getMaintenanceLog(printerId),
  });

  const addMutation = useMutation({
    mutationFn: () => api.addMaintenanceLog(printerId, { title: title.trim(), category, notes: notes.trim() || null }),
    onSuccess: () => {
      queryClient.invalidateQueries({ queryKey });
      setAdding(false);
      setTitle('');
      setNotes('');
      showToast(t('maintenance.log.logged'), 'success');
    },
    onError: (error: Error) => showToast(error.message || t('maintenance.log.failed'), 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (entryId: number) => api.deleteMaintenanceLog(entryId),
    onSuccess: () => queryClient.invalidateQueries({ queryKey }),
    onError: (error: Error) => showToast(error.message, 'error'),
  });

  const canAdd = hasPermission('maintenance:update');
  const canDelete = hasPermission('maintenance:delete');

  return (
    <div className="mt-4 pt-4 border-t border-bambu-dark-tertiary">
      <div className="flex items-center justify-between mb-3">
        <h3 className="text-sm font-medium text-white flex items-center gap-2">
          <ClipboardList className="w-4 h-4 text-bambu-gray" />
          {t('maintenance.log.title')}
        </h3>
        {canAdd && !adding && (
          <Button size="sm" variant="secondary" onClick={() => setAdding(true)}>
            <Plus className="w-3.5 h-3.5" />
            {t('maintenance.log.add')}
          </Button>
        )}
      </div>

      {adding && (
        <form
          className="space-y-2 mb-4 p-3 rounded-lg bg-bambu-dark/50"
          onSubmit={(e) => {
            e.preventDefault();
            if (title.trim()) addMutation.mutate();
          }}
        >
          <div className="flex flex-col sm:flex-row gap-2">
            <label className="sr-only" htmlFor={`mlog-title-${printerId}`}>
              {t('maintenance.log.task')}
            </label>
            <input
              id={`mlog-title-${printerId}`}
              type="text"
              maxLength={200}
              required
              autoFocus
              value={title}
              onChange={(e) => setTitle(e.target.value)}
              placeholder={t('maintenance.log.taskPlaceholder')}
              className="flex-1 px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white focus:border-bambu-green focus:outline-none"
            />
            <label className="sr-only" htmlFor={`mlog-cat-${printerId}`}>
              {t('maintenance.log.category')}
            </label>
            <select
              id={`mlog-cat-${printerId}`}
              value={category}
              onChange={(e) => setCategory(e.target.value)}
              className="px-2 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white focus:border-bambu-green focus:outline-none"
            >
              {MAINTENANCE_LOG_CATEGORIES.map((c) => (
                <option key={c} value={c}>
                  {t(`maintenance.log.categories.${c}`)}
                </option>
              ))}
            </select>
          </div>
          <label className="sr-only" htmlFor={`mlog-notes-${printerId}`}>
            {t('maintenance.log.notes')}
          </label>
          <textarea
            id={`mlog-notes-${printerId}`}
            rows={2}
            maxLength={4000}
            value={notes}
            onChange={(e) => setNotes(e.target.value)}
            placeholder={t('maintenance.log.notes')}
            className="w-full px-3 py-2 bg-bambu-dark border border-bambu-dark-tertiary rounded-lg text-sm text-white focus:border-bambu-green focus:outline-none"
          />
          <div className="flex gap-2 justify-end">
            <Button size="sm" variant="secondary" type="button" onClick={() => setAdding(false)}>
              {t('common.cancel')}
            </Button>
            <Button size="sm" type="submit" disabled={!title.trim() || addMutation.isPending}>
              {t('maintenance.log.save')}
            </Button>
          </div>
        </form>
      )}

      {entries.length === 0 ? (
        <p className="text-xs text-bambu-gray">{t('maintenance.log.empty')}</p>
      ) : (
        <ul className="space-y-2">
          {entries.map((e) => (
            <li key={e.id} className="flex items-start justify-between gap-3 text-sm">
              <div className="min-w-0">
                <div className="text-white flex items-center gap-2 flex-wrap">
                  <span className="truncate">{e.title}</span>
                  <span className="px-1.5 py-0.5 text-[10px] rounded bg-bambu-dark text-bambu-gray">
                    {e.scheduled
                      ? t('maintenance.log.scheduled')
                      : t(`maintenance.log.categories.${e.category}`, e.category)}
                  </span>
                </div>
                <div className="text-xs text-bambu-gray">
                  {new Date(e.performed_at).toLocaleString()}
                  {e.performed_by ? ` - ${e.performed_by}` : ''}
                  {` - ${Math.round(e.hours_at_maintenance)} ${t('common.hours')}`}
                </div>
                {e.notes && <div className="text-xs text-bambu-gray/80 mt-0.5 whitespace-pre-line">{e.notes}</div>}
              </div>
              {!e.scheduled && canDelete && (
                <button
                  type="button"
                  onClick={() => deleteMutation.mutate(e.id)}
                  className="p-1 text-bambu-gray hover:text-red-500 shrink-0"
                  aria-label={t('maintenance.log.delete')}
                  data-tip={t('maintenance.log.delete')}
                >
                  <Trash2 className="w-3.5 h-3.5" />
                </button>
              )}
            </li>
          ))}
        </ul>
      )}
    </div>
  );
}
