import { describe, it, expect, vi, beforeEach, afterEach } from 'vitest';
import { act, fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { CameraTile } from '../../components/CameraTile';
import { FakePeerConnection, SDP_ANSWER } from '../mocks/fakePeerConnection';

// WebRTC (WHEP) playback with automatic fallback (#56): WebRTC while it
// connects or plays, MJPEG once it fails, snapshots once MJPEG fails, then
// "no signal".

async function settle(times = 6) {
  for (let i = 0; i < times; i++) {
    await act(async () => {
      await Promise.resolve();
    });
  }
}

let whepStatus = 201;
let whepBody = SDP_ANSWER;

function renderTile(props: Partial<React.ComponentProps<typeof CameraTile>> = {}) {
  return render(
    <CameraTile
      printerId={5}
      printerName="H2D"
      mode="live"
      snapshotIntervalMs={1000}
      connected
      webrtc
      {...props}
    />,
  );
}

describe('CameraTile WebRTC', () => {
  beforeEach(() => {
    vi.useFakeTimers();
    FakePeerConnection.instances = [];
    whepStatus = 201;
    whepBody = SDP_ANSWER;
    vi.stubGlobal('RTCPeerConnection', FakePeerConnection);
    vi.spyOn(global, 'fetch').mockImplementation(async (input: RequestInfo | URL) => {
      const url = String(input);
      if (url.includes('/camera/webrtc')) return new Response(whepBody, { status: whepStatus });
      return new Response(null, { status: 200 });
    });
  });

  afterEach(() => {
    vi.useRealTimers();
    vi.unstubAllGlobals();
    vi.restoreAllMocks();
  });

  it('negotiates through the engine and shows video once it plays', async () => {
    renderTile();
    await settle();

    const webrtcCall = vi.mocked(fetch).mock.calls.find(([u]) => String(u).includes('/camera/webrtc'));
    expect(webrtcCall).toBeTruthy();
    expect(String(webrtcCall![0])).toContain('/api/v1/printers/5/camera/webrtc');
    expect(FakePeerConnection.instances[0].remoteDescription?.sdp).toBe(SDP_ANSWER);

    // While connecting: a still frame holds the tile, never the MJPEG stream.
    const still = screen.getByAltText('H2D') as HTMLImageElement;
    expect(still.src).toContain('/camera/snapshot');
    const video = screen.getByTestId('camera-tile-webrtc');
    expect(video.className).toContain('opacity-0');

    fireEvent.playing(video);
    await settle(1);
    expect(screen.queryByAltText('H2D')).toBeNull();
    expect(screen.getByTestId('camera-tile-webrtc').className).not.toContain('opacity-0');
  });

  it('falls back to MJPEG when the WHEP server fails', async () => {
    whepStatus = 502;
    whepBody = 'bad gateway';
    renderTile();
    await settle();
    expect(screen.queryByTestId('camera-tile-webrtc')).toBeNull();
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/stream');
    expect(FakePeerConnection.instances[0].closed).toBe(true);
  });

  it('falls back to MJPEG when no frame plays before the timeout', async () => {
    renderTile();
    await settle();
    expect(screen.getByTestId('camera-tile-webrtc')).toBeTruthy();
    await act(async () => {
      vi.advanceTimersByTime(8100);
    });
    expect(screen.queryByTestId('camera-tile-webrtc')).toBeNull();
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/stream');
  });

  it('falls back to MJPEG when a playing connection fails', async () => {
    renderTile();
    await settle();
    fireEvent.playing(screen.getByTestId('camera-tile-webrtc'));
    await settle(1);
    await act(async () => {
      FakePeerConnection.instances[0].setConnectionState('failed');
    });
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/stream');
  });

  it('then falls back to snapshots, then to no signal', async () => {
    whepStatus = 502;
    renderTile();
    await settle();
    fireEvent.error(screen.getByAltText('H2D'));
    await settle(1);
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/snapshot');
    fireEvent.error(screen.getByAltText('H2D'));
    await settle(1);
    expect(screen.queryByAltText('H2D')).toBeNull();
    expect(screen.getByText(/no signal/i)).toBeTruthy();
  });

  it('goes straight to MJPEG when the browser has no WebRTC', async () => {
    vi.stubGlobal('RTCPeerConnection', undefined);
    renderTile();
    await settle();
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/stream');
  });

  it('does not try WebRTC for printers without a WHEP URL or outside live mode', async () => {
    renderTile({ webrtc: false });
    await settle();
    expect(FakePeerConnection.instances).toHaveLength(0);
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/stream');
  });

  it('closes the peer connection when the tile leaves live mode', async () => {
    const { rerender } = renderTile();
    await settle();
    rerender(<CameraTile printerId={5} printerName="H2D" mode="snapshot" snapshotIntervalMs={1000} connected webrtc />);
    await settle(1);
    expect(FakePeerConnection.instances[0].closed).toBe(true);
    expect((screen.getByAltText('H2D') as HTMLImageElement).src).toContain('/camera/snapshot');
  });
});
