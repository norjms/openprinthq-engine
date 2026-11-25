import { useEffect, useState } from 'react';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { useTranslation } from 'react-i18next';
import { Pencil, Plus, Trash2, X } from 'lucide-react';

import { api, type EjectionProfile, type EjectionProfileInput } from '../api/client';
import { Button } from './Button';
import { useToast } from '../contexts/ToastContext';

// Bed-ejection profiles (OpenPrintHQ farm feature). A profile is the G-code that
// pushes a finished part off the bed plus the guards around it; the engine runs it
// after a successful print on printers with auto-eject on (services/bed_ejection.py).

const EMPTY: EjectionProfileInput = {
  name: '',
  gcode: '',
  max_bed_temp_c: 30,
  timeout_s: 1800,
  settle_s: 30,
  max_consecutive: 5,
  allowed_plates: null,
  verify_with_camera: false,
};

const inputCls =
  'w-full rounded border border-gray-300 dark:border-gray-600 bg-white dark:bg-gray-800 text-sm px-2 py-1 text-gray-900 dark:text-gray-100';

export function EjectionProfilesModal({ onClose }: { onClose: () => void }) {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const [editing, setEditing] = useState<number | 'new' | null>(null);
  const [form, setForm] = useState<EjectionProfileInput>(EMPTY);
  const [plates, setPlates] = useState('');

  const profilesQuery = useQuery({ queryKey: ['ejection-profiles'], queryFn: api.getEjectionProfiles });

  useEffect(() => {
    const handleKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose();
    };
    window.addEventListener('keydown', handleKey);
    return () => window.removeEventListener('keydown', handleKey);
  }, [onClose]);

  const refresh = () => {
    queryClient.invalidateQueries({ queryKey: ['ejection-profiles'] });
    queryClient.invalidateQueries({ queryKey: ['printers'] });
  };

  const saveMutation = useMutation({
    mutationFn: (data: EjectionProfileInput) =>
      editing === 'new' || editing === null ? api.createEjectionProfile(data) : api.updateEjectionProfile(editing, data),
    onSuccess: () => {
      showToast(t('ejection.saved'), 'success');
      setEditing(null);
      refresh();
    },
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const deleteMutation = useMutation({
    mutationFn: (id: number) => api.deleteEjectionProfile(id),
    onSuccess: refresh,
    onError: (e: Error) => showToast(e.message, 'error'),
  });

  const startEdit = (p: EjectionProfile | null) => {
    if (p) {
      setForm({
        name: p.name,
        gcode: p.gcode,
        max_bed_temp_c: p.max_bed_temp_c,
        timeout_s: p.timeout_s,
        settle_s: p.settle_s,
        max_consecutive: p.max_consecutive,
        allowed_plates: p.allowed_plates,
        verify_with_camera: p.verify_with_camera,
      });
      setPlates((p.allowed_plates || []).join(', '));
      setEditing(p.id);
    } else {
      setForm(EMPTY);
      setPlates('');
      setEditing('new');
    }
  };

  const submit = (e: React.FormEvent) => {
    e.preventDefault();
    const list = plates
      .split(',')
      .map((s) => s.trim())
      .filter(Boolean);
    saveMutation.mutate({ ...form, allowed_plates: list.length ? list : null });
  };

  const num = (v: string) => (v === '' ? null : Number(v));

  return (
    <div className="fixed inset-0 z-50 flex items-center justify-center bg-black/50 p-4">
      <div className="bg-white dark:bg-gray-900 rounded-lg shadow-xl max-w-2xl w-full max-h-[90vh] overflow-y-auto">
        <div className="flex items-center justify-between p-4 border-b border-gray-200 dark:border-gray-700">
          <h2 className="text-lg font-semibold text-gray-900 dark:text-gray-100">{t('ejection.title')}</h2>
          <button
            onClick={onClose}
            className="text-gray-400 hover:text-gray-600 dark:hover:text-gray-200"
            aria-label={t('common.close')}
          >
            <X className="w-5 h-5" />
          </button>
        </div>
        <div className="p-4 space-y-4">
          <p className="text-sm text-gray-600 dark:text-gray-400">{t('ejection.description')}</p>

          {editing === null ? (
            <>
              <ul className="divide-y divide-gray-200 dark:divide-gray-700">
                {(profilesQuery.data || []).map((p) => (
                  <li key={p.id} className="flex items-center justify-between py-2">
                    <div className="text-sm text-gray-900 dark:text-gray-100">
                      <div className="font-medium">{p.name}</div>
                      <div className="text-xs text-gray-500">
                        {t('ejection.summary', {
                          temp: p.max_bed_temp_c ?? '-',
                          cap: p.max_consecutive || '-',
                        })}
                      </div>
                    </div>
                    <div className="flex gap-2">
                      <Button variant="secondary" size="sm" onClick={() => startEdit(p)} aria-label={t('common.edit')}>
                        <Pencil className="w-4 h-4" />
                      </Button>
                      <Button
                        variant="secondary"
                        size="sm"
                        onClick={() => window.confirm(t('ejection.confirmDelete', { name: p.name })) && deleteMutation.mutate(p.id)}
                        aria-label={t('common.delete')}
                      >
                        <Trash2 className="w-4 h-4" />
                      </Button>
                    </div>
                  </li>
                ))}
                {profilesQuery.data && profilesQuery.data.length === 0 && (
                  <li className="py-2 text-sm text-gray-500">{t('ejection.none')}</li>
                )}
              </ul>
              <Button onClick={() => startEdit(null)}>
                <Plus className="w-4 h-4 mr-1" />
                {t('ejection.add')}
              </Button>
            </>
          ) : (
            <form onSubmit={submit} className="space-y-3">
              <div>
                <label htmlFor="ej-name" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                  {t('ejection.name')}
                </label>
                <input id="ej-name" className={inputCls} required maxLength={100} value={form.name}
                  onChange={(e) => setForm({ ...form, name: e.target.value })} />
              </div>
              <div>
                <label htmlFor="ej-gcode" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                  {t('ejection.gcode')}
                </label>
                <textarea id="ej-gcode" className={`${inputCls} font-mono`} rows={6} required value={form.gcode}
                  onChange={(e) => setForm({ ...form, gcode: e.target.value })} />
                <p className="text-xs text-gray-500 mt-1">{t('ejection.gcodeHelp')}</p>
              </div>
              <div className="grid grid-cols-2 gap-3">
                <div>
                  <label htmlFor="ej-temp" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                    {t('ejection.maxBedTemp')}
                  </label>
                  <input id="ej-temp" type="number" min={0} max={150} className={inputCls}
                    value={form.max_bed_temp_c ?? ''} onChange={(e) => setForm({ ...form, max_bed_temp_c: num(e.target.value) })} />
                </div>
                <div>
                  <label htmlFor="ej-cap" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                    {t('ejection.maxConsecutive')}
                  </label>
                  <input id="ej-cap" type="number" min={0} max={1000} className={inputCls}
                    value={form.max_consecutive} onChange={(e) => setForm({ ...form, max_consecutive: Number(e.target.value) || 0 })} />
                </div>
                <div>
                  <label htmlFor="ej-timeout" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                    {t('ejection.timeout')}
                  </label>
                  <input id="ej-timeout" type="number" min={30} max={14400} className={inputCls}
                    value={form.timeout_s} onChange={(e) => setForm({ ...form, timeout_s: Number(e.target.value) || 1800 })} />
                </div>
                <div>
                  <label htmlFor="ej-settle" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                    {t('ejection.settle')}
                  </label>
                  <input id="ej-settle" type="number" min={0} max={3600} className={inputCls}
                    value={form.settle_s} onChange={(e) => setForm({ ...form, settle_s: Number(e.target.value) || 0 })} />
                </div>
              </div>
              <div>
                <label htmlFor="ej-plates" className="block text-sm text-gray-700 dark:text-gray-300 mb-1">
                  {t('ejection.allowedPlates')}
                </label>
                <input id="ej-plates" className={inputCls} value={plates} onChange={(e) => setPlates(e.target.value)}
                  placeholder="Textured PEI Plate" />
                <p className="text-xs text-gray-500 mt-1">{t('ejection.allowedPlatesHelp')}</p>
              </div>
              <label className="flex items-center gap-2 text-sm text-gray-700 dark:text-gray-300">
                <input type="checkbox" checked={form.verify_with_camera}
                  onChange={(e) => setForm({ ...form, verify_with_camera: e.target.checked })} />
                {t('ejection.verifyWithCamera')}
              </label>
              <div className="flex justify-end gap-2">
                <Button variant="secondary" type="button" onClick={() => setEditing(null)}>
                  {t('common.cancel')}
                </Button>
                <Button type="submit" disabled={saveMutation.isPending}>
                  {t('common.save')}
                </Button>
              </div>
            </form>
          )}
        </div>
      </div>
    </div>
  );
}
