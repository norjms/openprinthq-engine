/** Minimal RTCPeerConnection stand-in for WHEP tests (#56). jsdom has no WebRTC. */
export class FakePeerConnection {
  static instances: FakePeerConnection[] = [];
  iceGatheringState: RTCIceGatheringState = 'complete';
  connectionState: RTCPeerConnectionState = 'new';
  localDescription: RTCSessionDescriptionInit | null = null;
  remoteDescription: RTCSessionDescriptionInit | null = null;
  ontrack: ((ev: RTCTrackEvent) => void) | null = null;
  onconnectionstatechange: (() => void) | null = null;
  transceivers: string[] = [];
  closed = false;
  private listeners: Record<string, Array<() => void>> = {};

  constructor() {
    FakePeerConnection.instances.push(this);
  }

  addTransceiver(kind: string, init?: RTCRtpTransceiverInit) {
    this.transceivers.push(`${kind}:${init?.direction ?? 'sendrecv'}`);
  }

  async createOffer(): Promise<RTCSessionDescriptionInit> {
    return { type: 'offer', sdp: 'v=0\r\no=- 1 1 IN IP4 0.0.0.0\r\n' };
  }

  async setLocalDescription(desc: RTCSessionDescriptionInit) {
    this.localDescription = desc;
  }

  async setRemoteDescription(desc: RTCSessionDescriptionInit) {
    this.remoteDescription = desc;
  }

  addEventListener(type: string, fn: () => void) {
    (this.listeners[type] ??= []).push(fn);
  }

  removeEventListener(type: string, fn: () => void) {
    this.listeners[type] = (this.listeners[type] ?? []).filter((f) => f !== fn);
  }

  emit(type: string) {
    for (const fn of this.listeners[type] ?? []) fn();
  }

  setConnectionState(state: RTCPeerConnectionState) {
    this.connectionState = state;
    this.onconnectionstatechange?.();
  }

  close() {
    this.closed = true;
  }
}

export const SDP_ANSWER = 'v=0\r\no=- 2 2 IN IP4 0.0.0.0\r\n';
