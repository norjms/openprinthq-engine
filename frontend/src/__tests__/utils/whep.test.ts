import { describe, it, expect, vi, afterEach } from 'vitest';
import {
  WHEP_INITIAL_STATE,
  negotiateWhep,
  pickLiveSource,
  whepEndpoint,
  whepReducer,
  type WhepEvent,
  type WhepState,
} from '../../utils/whep';
import { FakePeerConnection, SDP_ANSWER } from '../mocks/fakePeerConnection';

const run = (events: WhepEvent[], from: WhepState = WHEP_INITIAL_STATE) => events.reduce(whepReducer, from);

describe('whepReducer', () => {
  it('connects then plays', () => {
    expect(run([{ type: 'start' }]).phase).toBe('connecting');
    expect(run([{ type: 'start' }, { type: 'playing' }]).phase).toBe('playing');
  });

  it('fails on error, timeout or disconnect while connecting', () => {
    expect(run([{ type: 'start' }, { type: 'error', reason: 'WHEP HTTP 502' }])).toEqual({
      phase: 'failed',
      reason: 'WHEP HTTP 502',
    });
    expect(run([{ type: 'start' }, { type: 'timeout' }])).toEqual({ phase: 'failed', reason: 'timeout' });
    expect(run([{ type: 'start' }, { type: 'disconnected' }]).phase).toBe('failed');
  });

  it('ignores a late timeout once playing', () => {
    expect(run([{ type: 'start' }, { type: 'playing' }, { type: 'timeout' }]).phase).toBe('playing');
  });

  it('fails when a playing connection drops', () => {
    expect(run([{ type: 'start' }, { type: 'playing' }, { type: 'disconnected' }]).phase).toBe('failed');
  });

  it('stays failed until started again', () => {
    const failed = run([{ type: 'start' }, { type: 'timeout' }]);
    expect(run([{ type: 'playing' }, { type: 'error' }], failed).phase).toBe('failed');
    expect(run([{ type: 'start' }], failed).phase).toBe('connecting');
  });

  it('ignores events while idle and resets on stop', () => {
    expect(run([{ type: 'playing' }, { type: 'error' }, { type: 'timeout' }]).phase).toBe('idle');
    expect(run([{ type: 'start' }, { type: 'playing' }, { type: 'stop' }])).toEqual(WHEP_INITIAL_STATE);
  });
});

describe('pickLiveSource', () => {
  const base = { webrtcConfigured: true, phase: 'connecting' as const, mjpegFailed: false, snapshotFailed: false };

  it('uses WebRTC while connecting or playing', () => {
    expect(pickLiveSource(base)).toBe('webrtc');
    expect(pickLiveSource({ ...base, phase: 'playing' })).toBe('webrtc');
  });

  it('falls back to MJPEG, then snapshot, then nothing', () => {
    expect(pickLiveSource({ ...base, phase: 'failed' })).toBe('mjpeg');
    expect(pickLiveSource({ ...base, phase: 'failed', mjpegFailed: true })).toBe('snapshot');
    expect(pickLiveSource({ ...base, phase: 'failed', mjpegFailed: true, snapshotFailed: true })).toBe('none');
  });

  it('never picks WebRTC when the printer has no WHEP URL', () => {
    expect(pickLiveSource({ ...base, webrtcConfigured: false })).toBe('mjpeg');
  });
});

describe('whepEndpoint', () => {
  it('adds the stream token when there is one', () => {
    expect(whepEndpoint(3, null)).toBe('/api/v1/printers/3/camera/webrtc');
    expect(whepEndpoint(3, 'a b')).toBe('/api/v1/printers/3/camera/webrtc?token=a%20b');
  });
});

describe('negotiateWhep', () => {
  afterEach(() => {
    vi.useRealTimers();
  });

  it('posts a recv-only offer as SDP and applies the answer', async () => {
    const pc = new FakePeerConnection();
    const fetchImpl = vi.fn().mockResolvedValue(new Response(SDP_ANSWER, { status: 201 }));
    await negotiateWhep(pc as unknown as RTCPeerConnection, '/api/v1/printers/1/camera/webrtc', {
      fetchImpl,
      authToken: 'jwt',
    });
    expect(pc.transceivers).toEqual(['video:recvonly', 'audio:recvonly']);
    const [url, init] = fetchImpl.mock.calls[0];
    expect(url).toBe('/api/v1/printers/1/camera/webrtc');
    expect(init.method).toBe('POST');
    expect(init.headers['Content-Type']).toBe('application/sdp');
    expect(init.headers['Authorization']).toBe('Bearer jwt');
    expect(init.body).toContain('v=0');
    expect(pc.remoteDescription).toEqual({ type: 'answer', sdp: SDP_ANSWER });
  });

  it('throws on an HTTP error', async () => {
    const pc = new FakePeerConnection();
    const fetchImpl = vi.fn().mockResolvedValue(new Response('bad gateway', { status: 502 }));
    await expect(negotiateWhep(pc as unknown as RTCPeerConnection, '/x', { fetchImpl })).rejects.toThrow('502');
    expect(pc.remoteDescription).toBeNull();
  });

  it('throws when the answer is not SDP', async () => {
    const pc = new FakePeerConnection();
    const fetchImpl = vi.fn().mockResolvedValue(new Response('{"detail":"x"}', { status: 201 }));
    await expect(negotiateWhep(pc as unknown as RTCPeerConnection, '/x', { fetchImpl })).rejects.toThrow('not SDP');
  });

  it('sends the offer once ICE gathering completes', async () => {
    const pc = new FakePeerConnection();
    pc.iceGatheringState = 'gathering';
    const fetchImpl = vi.fn().mockResolvedValue(new Response(SDP_ANSWER, { status: 201 }));
    const done = negotiateWhep(pc as unknown as RTCPeerConnection, '/x', { fetchImpl, iceTimeoutMs: 60000 });
    await Promise.resolve();
    await Promise.resolve();
    expect(fetchImpl).not.toHaveBeenCalled();
    pc.iceGatheringState = 'complete';
    pc.emit('icegatheringstatechange');
    await done;
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });

  it('sends what it has when ICE gathering runs past the budget', async () => {
    vi.useFakeTimers();
    const pc = new FakePeerConnection();
    pc.iceGatheringState = 'gathering';
    const fetchImpl = vi.fn().mockResolvedValue(new Response(SDP_ANSWER, { status: 201 }));
    const done = negotiateWhep(pc as unknown as RTCPeerConnection, '/x', { fetchImpl, iceTimeoutMs: 2000 });
    await vi.advanceTimersByTimeAsync(2100);
    await done;
    expect(fetchImpl).toHaveBeenCalledTimes(1);
  });
});
