import os
import sounddevice as sd
import numpy as np
import lameenc
import threading
import logging
from core.api_client import api
from core.config import AUDIO_TEMP_FILE

class AudioManager:
    def __init__(self):
        # Audio configuration optimized for ESP32
        self.sample_rate = 22050
        self.bitrate = 96
        self.target_rms = 0.30
        self.extra_gain = 1.8
        
        self.is_recording = False
        self.recorded_audio = None
        self._record_thread = None

    def record_audio_sync(self, duration=5):
        """Records audio synchronously for a set duration"""
        logging.info(f"Recording for {duration} seconds...")
        audio = sd.rec(
            int(duration * self.sample_rate),
            samplerate=self.sample_rate,
            channels=1,
            dtype='float32'
        )
        sd.wait()
        self.recorded_audio = audio
        return self.process_and_encode()

    def process_and_encode(self):
        """Applies normalization and encodes to MP3"""
        if self.recorded_audio is None:
            raise ValueError("No audio recorded.")

        # Flatten to 1D array and remove DC offset
        audio = self.recorded_audio.flatten()
        audio = audio - np.mean(audio)

        # RMS Normalization (improves speech loudness)
        rms = np.sqrt(np.mean(audio ** 2))
        if rms > 0:
            audio = audio * (self.target_rms / rms)

        # Apply Extra Gain and Soft Clipping (prevents harsh distortion)
        audio = audio * self.extra_gain
        audio = np.tanh(audio)

        # Convert float (-1 to 1) to int16 PCM
        audio_int16 = np.int16(audio * 32767)
        pcm_data = audio_int16.tobytes()

        return self._encode_to_mp3(pcm_data)

    def _encode_to_mp3(self, pcm_data):
        """Encodes raw PCM data into MP3 using lameenc"""
        encoder = lameenc.Encoder()
        encoder.set_bit_rate(self.bitrate)
        encoder.set_in_sample_rate(self.sample_rate)
        encoder.set_channels(1)
        encoder.set_quality(2)

        mp3_data = encoder.encode(pcm_data)
        mp3_data += encoder.flush()

        with open(AUDIO_TEMP_FILE, "wb") as f:
            f.write(mp3_data)
            
        return AUDIO_TEMP_FILE

    def broadcast_audio(self, ip_list):
        """Sends the encoded MP3 file to a list of target IPs."""
        if not os.path.exists(AUDIO_TEMP_FILE):
            return False, "No audio file available to broadcast."

        results = {}
        for ip in ip_list:
            try:
                # Uses the extended 60s timeout in api_client
                resp = api.wearable_send_audio(ip, AUDIO_TEMP_FILE)
                results[ip] = {"status": "success", "response": resp}
            except Exception as e:
                results[ip] = {"status": "failed", "error": str(e)}
                
        return True, results

# Global audio manager instance
audio_system = AudioManager()