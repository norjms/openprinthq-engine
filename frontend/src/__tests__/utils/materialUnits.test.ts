/**
 * Tests for the non-Bambu multi-material unit helpers.
 */

import { describe, it, expect } from 'vitest';
import { isMaterialUnit, materialUnitLabel, MATERIAL_UNIT_KINDS } from '../../utils/materialUnits';

describe('isMaterialUnit', () => {
  it('recognises the four unit kinds', () => {
    for (const kind of MATERIAL_UNIT_KINDS) {
      expect(isMaterialUnit(kind)).toBe(true);
    }
  });

  it('leaves Bambu module types and empty values alone', () => {
    expect(isMaterialUnit('ams')).toBe(false);
    expect(isMaterialUnit('n3f')).toBe(false);
    expect(isMaterialUnit('')).toBe(false);
    expect(isMaterialUnit(undefined)).toBe(false);
  });
});

describe('materialUnitLabel', () => {
  it('numbers units from one', () => {
    const t = (key: string) => (key === 'printers.materialUnit.qidi_box' ? 'QIDI Box' : key);
    expect(materialUnitLabel(t, 'qidi_box', 0)).toBe('QIDI Box 1');
    expect(materialUnitLabel(t, 'qidi_box', 1)).toBe('QIDI Box 2');
  });
});
