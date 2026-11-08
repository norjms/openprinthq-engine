/**
 * Chat agent settings: write-only tokens, allowed channels, link codes and links.
 */
import { describe, it, expect } from 'vitest';
import { screen, waitFor } from '@testing-library/react';
import userEvent from '@testing-library/user-event';
import { http, HttpResponse } from 'msw';
import { render } from '../utils';
import { server } from '../mocks/server';
import { ChatAgentSettings } from '../../components/ChatAgentSettings';
import { splitIds } from '../../utils/chatAgent';

const config = (over: Record<string, unknown> = {}) => ({
  enabled: true,
  slack_enabled: true,
  discord_enabled: false,
  allow_dms: false,
  llm_enabled: false,
  slack_allowed_channels: ['C1'],
  slack_allowed_workspaces: [],
  discord_allowed_channels: [],
  discord_allowed_guilds: [],
  llm_base_url: '',
  llm_model: '',
  confirm_timeout_seconds: 120,
  slack_bot_token_set: true,
  slack_bot_token_from_env: false,
  slack_app_token_set: false,
  slack_app_token_from_env: false,
  discord_bot_token_set: false,
  discord_bot_token_from_env: true,
  llm_api_key_set: false,
  llm_api_key_from_env: false,
  status: { slack: { running: true, error: null }, discord: { running: false, error: null } },
  ...over,
});

const users = [
  { id: 1, username: 'admin', role: 'admin', is_active: true, is_admin: true, auth_source: 'local', groups: [] },
  { id: 2, username: 'ann', role: 'user', is_active: true, is_admin: false, auth_source: 'local', groups: [] },
];

function useHandlers(state: { puts: unknown[]; codes: unknown[]; deleted: number[] }) {
  server.use(
    http.get('/api/v1/chat-agent/config', () => HttpResponse.json(config())),
    http.put('/api/v1/chat-agent/config', async ({ request }) => {
      const body = (await request.json()) as Record<string, unknown>;
      state.puts.push(body);
      return HttpResponse.json(config({ slack_app_token_set: true, slack_allowed_channels: body.slack_allowed_channels }));
    }),
    http.get('/api/v1/chat-agent/links', () =>
      HttpResponse.json([
        {
          id: 7,
          platform: 'discord',
          external_user_id: '55',
          external_team_id: '9',
          display_name: 'ann#1',
          user_id: 2,
          username: 'ann',
          created_at: '2026-11-08T10:00:00',
          last_used_at: null,
        },
      ]),
    ),
    http.delete('/api/v1/chat-agent/links/:id', ({ params }) => {
      state.deleted.push(Number(params.id));
      return HttpResponse.json({ success: true });
    }),
    http.post('/api/v1/chat-agent/link-codes', async ({ request }) => {
      state.codes.push(await request.json());
      return HttpResponse.json({ code: 'ABCD-EFGH', user_id: 2, username: 'ann', expires_at: '2026-11-08T10:10:00' });
    }),
    http.get('/api/v1/users/', () => HttpResponse.json(users)),
  );
}

describe('splitIds', () => {
  it('splits on commas and new lines and drops blanks and repeats', () => {
    expect(splitIds(' C1, C2\nC1,, ')).toEqual(['C1', 'C2']);
  });
});

describe('ChatAgentSettings', () => {
  it('sends only the tokens that were typed, and the channel list', async () => {
    const state = { puts: [] as unknown[], codes: [] as unknown[], deleted: [] as number[] };
    useHandlers(state);
    render(<ChatAgentSettings />);
    const botToken = await screen.findByTestId('chat-secret-slack_bot_token');
    expect(botToken).toHaveAttribute('placeholder', 'Saved. Type a new value to replace it.');
    expect(screen.getByTestId('chat-secret-discord_bot_token')).toBeDisabled();
    expect(screen.getByText('Connected')).toBeInTheDocument();

    await userEvent.type(screen.getByTestId('chat-secret-slack_app_token'), 'xapp-1');
    const channels = screen.getByTestId('chat-slack_allowed_channels');
    await userEvent.clear(channels);
    await userEvent.type(channels, 'C1, C2');
    await userEvent.click(screen.getByTestId('chat-save'));

    await waitFor(() => expect(state.puts).toHaveLength(1));
    const body = state.puts[0] as Record<string, unknown>;
    expect(body.slack_app_token).toBe('xapp-1');
    expect(body).not.toHaveProperty('slack_bot_token');
    expect(body).not.toHaveProperty('discord_bot_token');
    expect(body.slack_allowed_channels).toEqual(['C1', 'C2']);
  });

  it('makes a link code for a chosen user and unlinks', async () => {
    const state = { puts: [] as unknown[], codes: [] as unknown[], deleted: [] as number[] };
    useHandlers(state);
    render(<ChatAgentSettings />);
    await screen.findByText(/ann#1/);
    const select = await screen.findByTestId('chat-code-user');
    await waitFor(() => expect(screen.getByRole('option', { name: 'ann' })).toBeInTheDocument());
    await userEvent.selectOptions(select, '2');
    await userEvent.click(screen.getByTestId('chat-make-code'));
    await waitFor(() => expect(screen.getByTestId('chat-code')).toHaveTextContent('link ABCD-EFGH'));
    expect(state.codes).toEqual([{ user_id: 2 }]);

    await userEvent.click(screen.getByTitle('Unlink'));
    await waitFor(() => expect(state.deleted).toEqual([7]));
  });
});
