import { useEffect, useState } from 'react';
import { useTranslation } from 'react-i18next';
import { useMutation, useQuery, useQueryClient } from '@tanstack/react-query';
import { Bot, Link2, Loader2, Trash2 } from 'lucide-react';
import { api } from '../api/client';
import type { ChatAgentConfig, ChatAgentConfigUpdate, ChatLinkCode } from '../api/client';
import { Card, CardContent, CardHeader } from './Card';
import { Button } from './Button';
import { Toggle } from './Toggle';
import { useToast } from '../contexts/ToastContext';
import { useAuth } from '../contexts/AuthContext';
import { splitIds } from '../utils/chatAgent';

const inputClass = 'w-full bg-gray-800 border border-gray-700 rounded px-3 py-2 text-white text-sm';

type SecretName = 'slack_bot_token' | 'slack_app_token' | 'discord_bot_token' | 'llm_api_key';


interface Draft {
  enabled: boolean;
  slack_enabled: boolean;
  discord_enabled: boolean;
  allow_dms: boolean;
  llm_enabled: boolean;
  slack_allowed_channels: string;
  slack_allowed_workspaces: string;
  discord_allowed_channels: string;
  discord_allowed_guilds: string;
  llm_base_url: string;
  llm_model: string;
  confirm_timeout_seconds: number;
}

function toDraft(c: ChatAgentConfig): Draft {
  return {
    enabled: c.enabled,
    slack_enabled: c.slack_enabled,
    discord_enabled: c.discord_enabled,
    allow_dms: c.allow_dms,
    llm_enabled: c.llm_enabled,
    slack_allowed_channels: c.slack_allowed_channels.join(', '),
    slack_allowed_workspaces: c.slack_allowed_workspaces.join(', '),
    discord_allowed_channels: c.discord_allowed_channels.join(', '),
    discord_allowed_guilds: c.discord_allowed_guilds.join(', '),
    llm_base_url: c.llm_base_url,
    llm_model: c.llm_model,
    confirm_timeout_seconds: c.confirm_timeout_seconds,
  };
}

export function ChatAgentSettings() {
  const { t } = useTranslation();
  const queryClient = useQueryClient();
  const { showToast } = useToast();
  const { authEnabled, user, hasPermission } = useAuth();

  const canReadSettings = !authEnabled || hasPermission('settings:read');
  const canEditSettings = !authEnabled || hasPermission('settings:update');
  const canManageUsers = !authEnabled || hasPermission('users:update');
  const canSeeAllLinks = !authEnabled || hasPermission('users:read');

  const [draft, setDraft] = useState<Draft | null>(null);
  const [secrets, setSecrets] = useState<Partial<Record<SecretName, string>>>({});
  const [showAll, setShowAll] = useState(false);
  const [codeUserId, setCodeUserId] = useState<number | ''>('');
  const [code, setCode] = useState<ChatLinkCode | null>(null);

  const { data: config, isLoading } = useQuery({
    queryKey: ['chat-agent-config'],
    queryFn: api.getChatAgentConfig,
    enabled: canReadSettings,
    refetchInterval: 15000,
  });

  const { data: links } = useQuery({
    queryKey: ['chat-agent-links', showAll],
    queryFn: () => api.getChatLinks(showAll),
  });

  const { data: users } = useQuery({
    queryKey: ['users'],
    queryFn: api.getUsers,
    enabled: canManageUsers,
  });

  useEffect(() => {
    if (config && draft === null) setDraft(toDraft(config));
  }, [config, draft]);

  const saveMutation = useMutation({
    mutationFn: () => {
      if (!draft) throw new Error('not loaded');
      const body: ChatAgentConfigUpdate = {
        enabled: draft.enabled,
        slack_enabled: draft.slack_enabled,
        discord_enabled: draft.discord_enabled,
        allow_dms: draft.allow_dms,
        llm_enabled: draft.llm_enabled,
        slack_allowed_channels: splitIds(draft.slack_allowed_channels),
        slack_allowed_workspaces: splitIds(draft.slack_allowed_workspaces),
        discord_allowed_channels: splitIds(draft.discord_allowed_channels),
        discord_allowed_guilds: splitIds(draft.discord_allowed_guilds),
        llm_base_url: draft.llm_base_url,
        llm_model: draft.llm_model,
        confirm_timeout_seconds: draft.confirm_timeout_seconds,
      };
      for (const [name, value] of Object.entries(secrets)) {
        if (value !== undefined) (body as Record<string, unknown>)[name] = value;
      }
      return api.updateChatAgentConfig(body);
    },
    onSuccess: (saved) => {
      queryClient.setQueryData(['chat-agent-config'], saved);
      setDraft(toDraft(saved));
      setSecrets({});
      showToast(t('chatAgent.saved'));
    },
    onError: (e: unknown) => showToast(e instanceof Error ? e.message : String(e), 'error'),
  });

  const codeMutation = useMutation({
    mutationFn: () => api.createChatLinkCode(codeUserId === '' ? undefined : codeUserId),
    onSuccess: (c) => setCode(c),
    onError: (e: unknown) => showToast(e instanceof Error ? e.message : String(e), 'error'),
  });

  const unlinkMutation = useMutation({
    mutationFn: (id: number) => api.deleteChatLink(id),
    onSuccess: () => queryClient.invalidateQueries({ queryKey: ['chat-agent-links'] }),
    onError: (e: unknown) => showToast(e instanceof Error ? e.message : String(e), 'error'),
  });

  const set = <K extends keyof Draft>(key: K, value: Draft[K]) => setDraft((d) => (d ? { ...d, [key]: value } : d));

  const secretField = (name: SecretName, label: string) => {
    const isSet = config?.[`${name}_set`];
    const fromEnv = config?.[`${name}_from_env`];
    const pending = secrets[name];
    return (
      <div>
        <label className="block text-sm text-bambu-gray mb-1">{label}</label>
        <div className="flex gap-2">
          <input
            type="password"
            autoComplete="off"
            value={pending ?? ''}
            onChange={(e) => setSecrets((s) => ({ ...s, [name]: e.target.value }))}
            placeholder={fromEnv ? t('chatAgent.tokenFromEnv') : isSet ? t('chatAgent.tokenSet') : t('chatAgent.tokenNotSet')}
            disabled={!canEditSettings || !!fromEnv}
            className={inputClass}
            data-testid={`chat-secret-${name}`}
          />
          {isSet && !fromEnv && canEditSettings && (
            <Button variant="secondary" onClick={() => setSecrets((s) => ({ ...s, [name]: '' }))}>
              {t('chatAgent.clearToken')}
            </Button>
          )}
        </div>
        {pending === '' && isSet && <p className="text-xs text-amber-500 mt-1">{t('chatAgent.tokenWillClear')}</p>}
      </div>
    );
  };

  const statusBadge = (platform: 'slack' | 'discord') => {
    const st = config?.status?.[platform];
    if (st?.error) return <span className="text-xs text-red-500">{t('chatAgent.statusError', { error: st.error })}</span>;
    return (
      <span className={`text-xs ${st?.running ? 'text-green-500' : 'text-bambu-gray'}`}>
        {st?.running ? t('chatAgent.statusRunning') : t('chatAgent.statusStopped')}
      </span>
    );
  };

  const idField = (key: keyof Draft, label: string, hint?: string) => (
    <div>
      <label className="block text-sm text-bambu-gray mb-1">{label}</label>
      <input
        type="text"
        value={String(draft?.[key] ?? '')}
        onChange={(e) => set(key, e.target.value as never)}
        disabled={!canEditSettings}
        className={inputClass}
        data-testid={`chat-${key}`}
      />
      {hint && <p className="text-xs text-bambu-gray mt-1">{hint}</p>}
    </div>
  );

  return (
    <div className="flex flex-col lg:flex-row gap-4 lg:gap-6">
      {canReadSettings && (
        <div className="space-y-3 flex-1 lg:max-w-xl">
          <Card id="card-chat-agent">
            <CardHeader>
              <div className="flex items-center justify-between">
                <div className="flex items-center gap-2">
                  <Bot className="w-5 h-5 text-bambu-green" />
                  <h2 className="text-lg font-semibold text-white">{t('chatAgent.title')}</h2>
                </div>
                {draft && (
                  <Toggle checked={draft.enabled} onChange={(v) => set('enabled', v)} disabled={!canEditSettings} />
                )}
              </div>
              <p className="text-sm text-bambu-gray mt-2">{t('chatAgent.description')}</p>
            </CardHeader>
            {(isLoading || !draft) && (
              <CardContent>
                <Loader2 className="w-5 h-5 animate-spin text-bambu-gray" />
              </CardContent>
            )}
          </Card>

          {draft && (
            <>
              <Card id="card-chat-slack">
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <h3 className="text-base font-semibold text-white">
                      Slack <span className="ml-2">{statusBadge('slack')}</span>
                    </h3>
                    <Toggle
                      checked={draft.slack_enabled}
                      onChange={(v) => set('slack_enabled', v)}
                      disabled={!canEditSettings}
                    />
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {secretField('slack_bot_token', t('chatAgent.botToken'))}
                  {secretField('slack_app_token', t('chatAgent.appToken'))}
                  {idField('slack_allowed_channels', t('chatAgent.allowedChannels'), t('chatAgent.allowedChannelsHint'))}
                  {idField('slack_allowed_workspaces', t('chatAgent.allowedWorkspaces'))}
                </CardContent>
              </Card>

              <Card id="card-chat-discord">
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <h3 className="text-base font-semibold text-white">
                      Discord <span className="ml-2">{statusBadge('discord')}</span>
                    </h3>
                    <Toggle
                      checked={draft.discord_enabled}
                      onChange={(v) => set('discord_enabled', v)}
                      disabled={!canEditSettings}
                    />
                  </div>
                </CardHeader>
                <CardContent className="space-y-3">
                  {secretField('discord_bot_token', t('chatAgent.botToken'))}
                  {idField('discord_allowed_channels', t('chatAgent.allowedChannels'), t('chatAgent.allowedChannelsHint'))}
                  {idField('discord_allowed_guilds', t('chatAgent.allowedGuilds'))}
                </CardContent>
              </Card>

              <Card id="card-chat-behaviour">
                <CardHeader>
                  <h3 className="text-base font-semibold text-white">{t('chatAgent.behaviourTitle')}</h3>
                </CardHeader>
                <CardContent className="space-y-3">
                  <div className="flex items-center justify-between">
                    <div>
                      <p className="text-sm text-white">{t('chatAgent.allowDms')}</p>
                      <p className="text-xs text-bambu-gray">{t('chatAgent.allowDmsHint')}</p>
                    </div>
                    <Toggle checked={draft.allow_dms} onChange={(v) => set('allow_dms', v)} disabled={!canEditSettings} />
                  </div>
                  <div>
                    <label className="block text-sm text-bambu-gray mb-1">{t('chatAgent.confirmTimeout')}</label>
                    <input
                      type="number"
                      min={30}
                      max={900}
                      value={draft.confirm_timeout_seconds}
                      onChange={(e) => set('confirm_timeout_seconds', Number(e.target.value) || 120)}
                      disabled={!canEditSettings}
                      className={inputClass}
                    />
                    <p className="text-xs text-bambu-gray mt-1">{t('chatAgent.confirmTimeoutHint')}</p>
                  </div>
                </CardContent>
              </Card>

              <Card id="card-chat-llm">
                <CardHeader>
                  <div className="flex items-center justify-between">
                    <h3 className="text-base font-semibold text-white">{t('chatAgent.llmTitle')}</h3>
                    <Toggle checked={draft.llm_enabled} onChange={(v) => set('llm_enabled', v)} disabled={!canEditSettings} />
                  </div>
                  <p className="text-xs text-bambu-gray mt-1">{t('chatAgent.llmHint')}</p>
                </CardHeader>
                <CardContent className="space-y-3">
                  {idField('llm_base_url', t('chatAgent.llmBaseUrl'))}
                  {idField('llm_model', t('chatAgent.llmModel'))}
                  {secretField('llm_api_key', t('chatAgent.llmApiKey'))}
                </CardContent>
              </Card>

              {canEditSettings && (
                <div className="flex justify-end">
                  <Button onClick={() => saveMutation.mutate()} disabled={saveMutation.isPending} data-testid="chat-save">
                    {saveMutation.isPending && <Loader2 className="w-4 h-4 animate-spin" />}
                    {t('chatAgent.save')}
                  </Button>
                </div>
              )}
            </>
          )}
        </div>
      )}

      <div className="space-y-3 flex-1 lg:max-w-xl">
        <Card id="card-chat-links">
          <CardHeader>
            <div className="flex items-center gap-2">
              <Link2 className="w-5 h-5 text-bambu-green" />
              <h3 className="text-base font-semibold text-white">{t('chatAgent.linksTitle')}</h3>
            </div>
            <p className="text-sm text-bambu-gray mt-2">{t('chatAgent.linksDescription')}</p>
          </CardHeader>
          <CardContent className="space-y-3">
            <div className="flex flex-wrap items-end gap-2">
              {canManageUsers && users && (
                <div className="flex-1 min-w-[10rem]">
                  <label className="block text-sm text-bambu-gray mb-1">{t('chatAgent.makeCodeFor')}</label>
                  <select
                    value={codeUserId}
                    onChange={(e) => setCodeUserId(e.target.value === '' ? '' : Number(e.target.value))}
                    className={inputClass}
                    data-testid="chat-code-user"
                  >
                    {user && <option value="">{t('chatAgent.myself')}</option>}
                    {!user && <option value="">-</option>}
                    {users
                      .filter((u) => u.is_active && u.id !== user?.id)
                      .map((u) => (
                        <option key={u.id} value={u.id}>
                          {u.username}
                        </option>
                      ))}
                  </select>
                </div>
              )}
              <Button
                onClick={() => codeMutation.mutate()}
                disabled={codeMutation.isPending || (!user && codeUserId === '')}
                data-testid="chat-make-code"
              >
                {t('chatAgent.makeCode')}
              </Button>
            </div>
            {code && (
              <div className="p-3 rounded border border-bambu-green/40 bg-bambu-green/10" data-testid="chat-code">
                <p className="text-sm text-white">
                  {t('chatAgent.codeInstructions', { command: `link ${code.code}`, user: code.username })}
                </p>
                <p className="font-mono text-lg text-white mt-1">link {code.code}</p>
                <p className="text-xs text-bambu-gray mt-1">
                  {t('chatAgent.codeExpires', { time: new Date(`${code.expires_at}Z`).toLocaleTimeString() })}
                </p>
              </div>
            )}

            {canSeeAllLinks && (
              <label className="flex items-center gap-2 text-sm text-bambu-gray">
                <input type="checkbox" checked={showAll} onChange={(e) => setShowAll(e.target.checked)} />
                {t('chatAgent.showAll')}
              </label>
            )}
            {links && links.length === 0 && <p className="text-sm text-bambu-gray">{t('chatAgent.noLinks')}</p>}
            <ul className="divide-y divide-gray-700">
              {links?.map((link) => (
                <li key={link.id} className="flex items-center justify-between py-2" data-testid="chat-link-row">
                  <div className="min-w-0">
                    <p className="text-sm text-white truncate">
                      {link.platform === 'slack' ? 'Slack' : 'Discord'}: {link.display_name || link.external_user_id}
                      {' -> '}
                      {link.username}
                    </p>
                    <p className="text-xs text-bambu-gray">
                      {t('chatAgent.lastUsed', {
                        time: link.last_used_at ? new Date(`${link.last_used_at}Z`).toLocaleString() : t('chatAgent.never'),
                      })}
                    </p>
                  </div>
                  <Button variant="ghost" onClick={() => unlinkMutation.mutate(link.id)} title={t('chatAgent.unlink')}>
                    <Trash2 className="w-4 h-4" />
                  </Button>
                </li>
              ))}
            </ul>
          </CardContent>
        </Card>
      </div>
    </div>
  );
}
