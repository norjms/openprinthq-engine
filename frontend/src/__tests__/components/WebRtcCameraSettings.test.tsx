import { describe, it, expect, vi } from 'vitest';
import { fireEvent, screen } from '@testing-library/react';
import { render } from '../utils';
import { WebRtcCameraSettings } from '../../components/WebRtcCameraSettings';

describe('WebRtcCameraSettings', () => {
  it('saves the WHEP URL on blur, and clears it when emptied', () => {
    const onSaveUrl = vi.fn();
    render(<WebRtcCameraSettings printerId={4} webrtcUrl={null} onSaveUrl={onSaveUrl} onApplyGo2rtc={vi.fn()} />);
    const input = screen.getByLabelText('WebRTC URL (optional)');
    fireEvent.change(input, { target: { value: ' http://go2rtc.lan:1984/api/webrtc?src=h2d ' } });
    fireEvent.blur(input);
    expect(onSaveUrl).toHaveBeenCalledWith(4, 'http://go2rtc.lan:1984/api/webrtc?src=h2d');
  });

  it('does not save an unchanged value', () => {
    const onSaveUrl = vi.fn();
    render(<WebRtcCameraSettings printerId={4} webrtcUrl="http://a/whep" onSaveUrl={onSaveUrl} onApplyGo2rtc={vi.fn()} />);
    const input = screen.getByLabelText('WebRTC URL (optional)');
    fireEvent.change(input, { target: { value: 'http://a/whep' } });
    fireEvent.blur(input);
    expect(onSaveUrl).not.toHaveBeenCalled();

    fireEvent.change(input, { target: { value: '' } });
    fireEvent.keyDown(input, { key: 'Enter' });
    expect(onSaveUrl).toHaveBeenCalledWith(4, null);
  });

  it('applies a go2rtc base URL and stream name', () => {
    const onApply = vi.fn();
    render(<WebRtcCameraSettings printerId={9} webrtcUrl={null} onSaveUrl={vi.fn()} onApplyGo2rtc={onApply} />);
    const apply = screen.getByRole('button', { name: 'Apply' });
    expect(apply).toBeDisabled();
    fireEvent.change(screen.getByLabelText('go2rtc URL'), { target: { value: 'http://go2rtc.lan:1984 ' } });
    fireEvent.change(screen.getByLabelText('Stream name'), { target: { value: 'voron' } });
    fireEvent.click(apply);
    expect(onApply).toHaveBeenCalledWith(9, 'http://go2rtc.lan:1984', 'voron');
  });
});
