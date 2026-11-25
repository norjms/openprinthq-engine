/**
 * WebRTC (WHEP) camera playback helpers (#56).
 *
 * The camera wall and the printer camera views try WebRTC first when a printer
 * has a WHEP URL, and fall back to the engine's MJPEG stream (and then to
 * snapshots) when it does not connect. Signalling goes through the engine at
 * POST /printers/{id}/camera/webrtc; media flows straight from the WHEP server
 * (usually go2rtc) to the browser.
 *
 * The state machine and the negotiation are plain functions so they can be
 * tested without a browser; the React glue lives in hooks/useWhepPlayer.ts.
 */

export type WhepPhase = 'idle' | 'connecting' | 'playing' | 'failed';

export type WhepEvent =
  | { type: 'start' }
  | { type: 'playing' }
  | { type: 'error'; reason?: string }
  | { type: 'timeout' }
  | { type: 'disconnected' }
  | { type: 'stop' };

export interface WhepState {
  phase: WhepPhase;
  reason: string | null;
}

export const WHEP_INITIAL_STATE: WhepState = { phase: 'idle', reason: null };

/** Time allowed from starting negotiation to the first frame playing. */
export const WHEP_CONNECT_TIMEOUT_MS = 8000;
/** Upper bound on ICE gathering before the offer is sent with what we have. */
export const WHEP_ICE_GATHER_TIMEOUT_MS = 2000;

/**
 * Transitions:
 *   idle/failed --start--> connecting --playing--> playing
 *   connecting --error|timeout|disconnected--> failed
 *   playing --error|disconnected--> failed   (a late timeout is ignored)
 *   any --stop--> idle
 * `failed` is sticky: the caller falls back to MJPEG and stays there until it
 * explicitly starts again (retry button, printer change, remount).
 */
export function whepReducer(state: WhepState, event: WhepEvent): WhepState {
  switch (event.type) {
    case 'stop':
      return WHEP_INITIAL_STATE;
    case 'start':
      return state.phase === 'connecting' ? state : { phase: 'connecting', reason: null };
    case 'playing':
      return state.phase === 'connecting' || state.phase === 'playing'
        ? { phase: 'playing', reason: null }
        : state;
    case 'timeout':
      return state.phase === 'connecting' ? { phase: 'failed', reason: 'timeout' } : state;
    case 'error':
    case 'disconnected':
      return state.phase === 'connecting' || state.phase === 'playing'
        ? { phase: 'failed', reason: event.type === 'error' ? (event.reason ?? 'error') : 'disconnected' }
        : state;
    default:
      return state;
  }
}

/** Which source a live camera view should draw. */
export type LiveSource = 'webrtc' | 'mjpeg' | 'snapshot' | 'none';

/**
 * The fallback chain: WebRTC while it is connecting or playing, MJPEG once it
 * has failed (or when the printer has no WHEP URL), snapshots once the MJPEG
 * stream has errored too, and nothing once snapshots have also failed.
 */
export function pickLiveSource(opts: {
  webrtcConfigured: boolean;
  phase: WhepPhase;
  mjpegFailed: boolean;
  snapshotFailed: boolean;
}): LiveSource {
  if (opts.webrtcConfigured && (opts.phase === 'connecting' || opts.phase === 'playing')) return 'webrtc';
  if (!opts.mjpegFailed) return 'mjpeg';
  if (!opts.snapshotFailed) return 'snapshot';
  return 'none';
}

/** True when this browser can play WebRTC at all. */
export function webrtcSupported(): boolean {
  return typeof window !== 'undefined' && typeof window.RTCPeerConnection === 'function';
}

/** The engine's signalling endpoint for one printer, with the stream token when there is one. */
export function whepEndpoint(printerId: number, token: string | null | undefined): string {
  const base = `/api/v1/printers/${printerId}/camera/webrtc`;
  return token ? `${base}?token=${encodeURIComponent(token)}` : base;
}

function waitForIceGathering(pc: RTCPeerConnection, timeoutMs: number): Promise<void> {
  if (pc.iceGatheringState === 'complete') return Promise.resolve();
  return new Promise((resolve) => {
    const done = () => {
      clearTimeout(timer);
      pc.removeEventListener('icegatheringstatechange', check);
      resolve();
    };
    const check = () => {
      if (pc.iceGatheringState === 'complete') done();
    };
    // Send what we have once the budget is spent: host candidates arrive in
    // milliseconds, and those are all a LAN viewer needs.
    const timer = setTimeout(done, timeoutMs);
    pc.addEventListener('icegatheringstatechange', check);
  });
}

/**
 * Non-trickle WHEP: offer recv-only video (and audio, which go2rtc may carry),
 * wait for ICE gathering, POST the complete offer, apply the answer. Throws on
 * any failure so the caller can fall back.
 */
export async function negotiateWhep(
  pc: RTCPeerConnection,
  endpoint: string,
  opts: { fetchImpl?: typeof fetch; authToken?: string | null; iceTimeoutMs?: number; signal?: AbortSignal } = {},
): Promise<void> {
  const fetchImpl = opts.fetchImpl ?? fetch;
  pc.addTransceiver('video', { direction: 'recvonly' });
  pc.addTransceiver('audio', { direction: 'recvonly' });
  const offer = await pc.createOffer();
  await pc.setLocalDescription(offer);
  await waitForIceGathering(pc, opts.iceTimeoutMs ?? WHEP_ICE_GATHER_TIMEOUT_MS);
  const sdp = pc.localDescription?.sdp ?? offer.sdp;
  if (!sdp) throw new Error('no local SDP');

  const headers: Record<string, string> = { 'Content-Type': 'application/sdp' };
  if (opts.authToken) headers['Authorization'] = `Bearer ${opts.authToken}`;
  const res = await fetchImpl(endpoint, { method: 'POST', headers, body: sdp, signal: opts.signal });
  if (res.status !== 201 && res.status !== 200) throw new Error(`WHEP HTTP ${res.status}`);
  const answer = await res.text();
  if (!answer.trimStart().startsWith('v=0')) throw new Error('WHEP answer is not SDP');
  await pc.setRemoteDescription({ type: 'answer', sdp: answer });
}
