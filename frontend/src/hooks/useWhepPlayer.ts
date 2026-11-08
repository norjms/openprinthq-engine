import { useCallback, useEffect, useReducer, useRef, useState } from 'react';
import { getAuthToken } from '../api/client';
import {
  WHEP_CONNECT_TIMEOUT_MS,
  WHEP_INITIAL_STATE,
  negotiateWhep,
  webrtcSupported,
  whepReducer,
  type WhepEvent,
  type WhepPhase,
} from '../utils/whep';

export interface WhepPlayer {
  phase: WhepPhase;
  /** Attach to the <video> element; it must be mounted while phase is connecting or playing. */
  videoRef: React.RefObject<HTMLVideoElement | null>;
  /** Wire to the <video> element's onPlaying. The element is the authority on whether video plays. */
  onPlaying: () => void;
  /** Start WebRTC again after a failure (retry button). */
  retry: () => void;
}

/**
 * Plays a printer camera over WebRTC (WHEP) through the engine's signalling
 * endpoint. Pass `endpoint = null` to disable (no WHEP URL, snapshot mode,
 * minimised viewer, or the stream token is not ready yet).
 *
 * The phase only becomes `playing` when the <video> element reports it is
 * playing: a negotiated track is not a picture, and treating it as one leaves
 * a black tile labelled live. A connection that has not produced a playing
 * frame within the timeout counts as failed, and the caller falls back.
 */
export function useWhepPlayer(endpoint: string | null, timeoutMs: number = WHEP_CONNECT_TIMEOUT_MS): WhepPlayer {
  const [state, dispatch] = useReducer(whepReducer, WHEP_INITIAL_STATE);
  const [attempt, setAttempt] = useState(0);
  const videoRef = useRef<HTMLVideoElement | null>(null);
  const playingRef = useRef(false);

  useEffect(() => {
    playingRef.current = false;
    if (!endpoint) {
      dispatch({ type: 'stop' });
      return;
    }
    dispatch({ type: 'start' });
    if (!webrtcSupported()) {
      dispatch({ type: 'error', reason: 'unsupported' });
      return;
    }

    let closed = false;
    const abort = new AbortController();
    const pc = new RTCPeerConnection();
    let timer: ReturnType<typeof setTimeout> | undefined;

    const teardown = () => {
      if (closed) return;
      closed = true;
      clearTimeout(timer);
      abort.abort();
      pc.ontrack = null;
      pc.onconnectionstatechange = null;
      try {
        pc.close();
      } catch {
        // already closed
      }
      const video = videoRef.current;
      if (video) video.srcObject = null;
    };
    // Any failure releases the peer connection at once; the caller is about
    // to fall back to MJPEG and must not pay for both.
    const fail = (event: WhepEvent) => {
      if (closed) return;
      dispatch(event);
      teardown();
    };

    timer = setTimeout(() => {
      if (!playingRef.current) fail({ type: 'timeout' });
    }, timeoutMs);

    pc.ontrack = (ev) => {
      const video = videoRef.current;
      if (!video || ev.track.kind !== 'video') return;
      video.srcObject = ev.streams[0] ?? new MediaStream([ev.track]);
      video.play().catch(() => {
        // Autoplay of a muted inline video is allowed everywhere; a rejection
        // here means no frame will come, and the timeout handles that.
      });
    };
    pc.onconnectionstatechange = () => {
      if (pc.connectionState === 'failed' || pc.connectionState === 'closed') {
        fail({ type: 'disconnected' });
      }
    };

    negotiateWhep(pc, endpoint, { authToken: getAuthToken(), signal: abort.signal }).catch((err: unknown) => {
      fail({ type: 'error', reason: err instanceof Error ? err.message : 'error' });
    });

    return teardown;
  }, [endpoint, attempt, timeoutMs]);

  const onPlaying = useCallback(() => {
    playingRef.current = true;
    dispatch({ type: 'playing' });
  }, []);
  const retry = useCallback(() => setAttempt((a) => a + 1), []);

  return { phase: state.phase, videoRef, onPlaying, retry };
}
