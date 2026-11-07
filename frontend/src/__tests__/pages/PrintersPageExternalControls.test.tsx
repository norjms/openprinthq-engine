/**
 * Non-Bambu printer cards hide the Bambu-only speed levels.
 *
 * The speed control used to be gated on "not Klipper", so OctoPrint and
 * PrusaLink printers showed it too. It now shows only for Bambu printers
 * (connection_type missing or 'bambu').
 */

import { describe, it, expect, beforeEach } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import { render } from '../utils';
import { PrintersPage } from '../../pages/PrintersPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const base = {
  id: 1,
  ip_address: '192.0.2.10',
  serial_number: 'EXT-0001',
  access_code: '',
  model: '',
  enabled: true,
  location: 'Workshop',
  auto_archive: true,
  created_at: '2024-01-01T00:00:00Z',
  updated_at: '2024-01-01T00:00:00Z',
};

const printing = {
  connected: true,
  state: 'RUNNING',
  progress: 42,
  layer_num: 10,
  total_layers: 100,
  temperatures: { nozzle: 215, bed: 60 },
  remaining_time: 3600,
  filename: 'part.bgcode',
  wifi_signal: null,
  vt_tray: [],
};

function useStatus() {
  server.use(
    http.get('/api/v1/printers/:id/status', () => HttpResponse.json(printing)),
    http.get('/api/v1/queue/', () => HttpResponse.json([])),
  );
}

describe('PrintersPage - external printer controls', () => {
  beforeEach(() => useStatus());

  it.each([
    ['prusalink', { pause_resume_stop: true, print: true, home: false, temperature: false, gcode: false }],
    ['octoprint', { pause_resume_stop: true, print: true, home: true, temperature: true, gcode: true }],
  ])('a %s printer has no speed control', async (connection_type, controls) => {
    server.use(
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([{ ...base, name: `My ${connection_type}`, connection_type, controls }]),
      ),
    );
    render(<PrintersPage />);
    await waitFor(() => expect(screen.getByText(`My ${connection_type}`)).toBeInTheDocument());
    expect(screen.queryByTestId('speed-control')).not.toBeInTheDocument();
  });

  it('a Bambu printer keeps its speed control', async () => {
    server.use(
      http.get('/api/v1/printers/', () =>
        HttpResponse.json([{ ...base, name: 'My Bambu', serial_number: '00M09A350100001', model: 'X1C' }]),
      ),
    );
    render(<PrintersPage />);
    await waitFor(() => expect(screen.getByTestId('speed-control')).toBeInTheDocument());
  });
});
