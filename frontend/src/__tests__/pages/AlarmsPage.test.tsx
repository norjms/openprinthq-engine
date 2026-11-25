/**
 * Alarms page: lists the engine's alarm feed, worst first as the API returns it, and acknowledges.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { render } from '../utils';
import { AlarmsPage } from '../../pages/AlarmsPage';
import { http, HttpResponse } from 'msw';
import { server } from '../mocks/server';

const alarm = (over: Record<string, unknown>) => ({
  id: 1,
  printer_id: 3,
  printer_name: 'MK4S',
  source: 'prusalink',
  code: 'attention',
  severity: 'warning',
  message: 'The printer needs attention',
  first_seen: '2026-11-07T21:00:00',
  last_seen: '2026-11-07T21:05:00',
  cleared_at: null,
  active: true,
  acknowledged_at: null,
  acknowledged_by: null,
  ...over,
});

describe('AlarmsPage', () => {
  it('shows the active alarms and acknowledges one', async () => {
    const acked: number[] = [];
    server.use(
      http.get('/api/v1/alarms', () =>
        HttpResponse.json([
          alarm({ id: 2, printer_name: 'H2D', source: 'bambu', code: '0700_8010', severity: 'fatal', message: 'AMS assist motor overloaded' }),
          alarm({}),
        ]),
      ),
      http.post('/api/v1/alarms/:id/ack', ({ params }) => {
        acked.push(Number(params.id));
        return HttpResponse.json({ acknowledged: 1 });
      }),
    );
    render(<AlarmsPage />);
    await waitFor(() => expect(screen.getByText(/AMS assist motor overloaded/)).toBeInTheDocument());
    expect(screen.getByText(/The printer needs attention/)).toBeInTheDocument();
    const buttons = screen.getAllByRole('button', { name: 'Acknowledge' });
    await userEvent.click(buttons[0]);
    await waitFor(() => expect(acked).toEqual([2]));
  });

  it('says so when nothing is wrong', async () => {
    server.use(http.get('/api/v1/alarms', () => HttpResponse.json([])));
    render(<AlarmsPage />);
    await waitFor(() => expect(screen.getByText('No printer is reporting a problem.')).toBeInTheDocument());
  });

  it('shows who acknowledged an alarm instead of the button', async () => {
    server.use(
      http.get('/api/v1/alarms', () =>
        HttpResponse.json([alarm({ acknowledged_at: '2026-11-07T21:10:00', acknowledged_by: 'james' })]),
      ),
    );
    render(<AlarmsPage />);
    await waitFor(() => expect(screen.getByText('Seen by james')).toBeInTheDocument());
    expect(screen.queryByRole('button', { name: 'Acknowledge' })).not.toBeInTheDocument();
  });
});
