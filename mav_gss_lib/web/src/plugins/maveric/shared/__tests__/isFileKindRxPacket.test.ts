import { describe, it, expect } from 'vitest';
import { isFileKindRxPacket } from '../../missionFacts';
import { fileCaps } from '../fileKinds';
import type { RxPacket } from '@/lib/types';

function pkt(cmdId: string, ptype = 'CMD', src = 'GS'): RxPacket {
  return {
    mission: { id: 'maveric', cmd_id: cmdId, facts: { header: { ptype, src } } },
  } as unknown as RxPacket;
}

describe('isFileKindRxPacket', () => {
  it('matches imaging cmd_id family for image kind', () => {
    expect(isFileKindRxPacket(pkt('img_get_chunks'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('cam_capture'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('lcd_display'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('aii_get_chunks'), fileCaps('image'))).toBe(false);
    expect(isFileKindRxPacket(pkt('mag_get_chunks'), fileCaps('image'))).toBe(false);
  });

  it('matches AII family only', () => {
    expect(isFileKindRxPacket(pkt('aii_get_chunks'), fileCaps('aii'))).toBe(true);
    expect(isFileKindRxPacket(pkt('aii_dir'), fileCaps('aii'))).toBe(true);
    expect(isFileKindRxPacket(pkt('img_get_chunks'), fileCaps('aii'))).toBe(false);
  });

  it('matches MAG family only', () => {
    expect(isFileKindRxPacket(pkt('mag_get_chunks'), fileCaps('mag'))).toBe(true);
    expect(isFileKindRxPacket(pkt('mag_capture'), fileCaps('mag'))).toBe(true);
    expect(isFileKindRxPacket(pkt('mag_kill'), fileCaps('mag'))).toBe(true);
    expect(isFileKindRxPacket(pkt('img_delete'), fileCaps('mag'))).toBe(false);
  });

  it('image preserves the legacy error-ptype fallback for HLNV/ASTR', () => {
    // Legacy isImagingRxPacket included ERR/NACK/FAIL/TIMEOUT from
    // imaging nodes in the imaging RX log. Preserve that for image kind.
    expect(isFileKindRxPacket(pkt('something_else', 'ERR', 'HLNV'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('something_else', 'NACK', 'ASTR'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('something_else', 'TIMEOUT', 'HLNV'), fileCaps('image'))).toBe(true);
    expect(isFileKindRxPacket(pkt('something_else', 'CMD', 'HLNV'), fileCaps('image'))).toBe(false);
    expect(isFileKindRxPacket(pkt('something_else', 'ERR', 'GS'), fileCaps('image'))).toBe(false);
  });

  it('aii/mag opt out of error fallback (ambiguous across kinds)', () => {
    expect(isFileKindRxPacket(pkt('something_else', 'ERR', 'HLNV'), fileCaps('aii'))).toBe(false);
    expect(isFileKindRxPacket(pkt('something_else', 'NACK', 'ASTR'), fileCaps('mag'))).toBe(false);
  });
});
