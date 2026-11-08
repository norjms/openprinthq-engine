import { FlaskConical } from 'lucide-react';
import { useTranslation } from 'react-i18next';

/** Small "Untested on hardware" pill for printer types and units we could not test. */
export function UntestedHardwareBadge({ compact = false }: { compact?: boolean }) {
  const { t } = useTranslation();
  return (
    <span
      className="inline-flex shrink-0 items-center gap-0.5 rounded bg-amber-100 px-1 py-0.5 text-[9px] font-medium text-amber-700 dark:bg-amber-500/20 dark:text-amber-400"
      title={t('printers.materialUnit.untestedHelp')}
      data-testid="untested-hardware-badge"
    >
      <FlaskConical className="h-3 w-3" />
      {!compact && t('printers.materialUnit.untested')}
    </span>
  );
}
