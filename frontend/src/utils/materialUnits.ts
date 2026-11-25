/**
 * Non-Bambu multi-material units the engine reports in the AMS list, keyed by
 * `module_type` (see backend/app/services/material_slots.py). Their slots are
 * shown like AMS trays, but Bambu-only slot actions (RFID re-read, load and
 * unload over MQTT, drying, slot presets) do not apply to them.
 */
export const MATERIAL_UNIT_KINDS = ['cfs', 'qidi_box', 'ace', 'canvas'] as const;
export type MaterialUnitKind = (typeof MATERIAL_UNIT_KINDS)[number];

export function isMaterialUnit(moduleType: string | null | undefined): moduleType is MaterialUnitKind {
  return !!moduleType && (MATERIAL_UNIT_KINDS as readonly string[]).includes(moduleType);
}

/** "CFS 1", "QIDI Box 2", "ACE 1", "CANVAS 1" (units are 0-based in the API). */
export function materialUnitLabel(t: (key: string) => string, kind: MaterialUnitKind, unitId: number): string {
  return `${t(`printers.materialUnit.${kind}`)} ${unitId + 1}`;
}

/**
 * Connection types built from published protocol documentation without a
 * printer to test on. The card and the add dialog say so.
 */
export const UNTESTED_CONNECTION_TYPES: readonly string[] = ['elegoo', 'elegoo_cc2', 'anycubic'];

export function isUntestedConnection(connectionType: string | null | undefined): boolean {
  return !!connectionType && UNTESTED_CONNECTION_TYPES.includes(connectionType);
}
