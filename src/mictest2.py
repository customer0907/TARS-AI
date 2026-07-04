import os
import random
import sounddevice as sd
from vosk import Model, KaldiRecognizer
import threading
import requests
import numpy as np
import time
import logging

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')

class STTManager:
    def __init__(self, sample_rate=44100):
        self.SAMPLE_RATE = sample_rate
        self.silence_threshold = 10  # Default silence threshold

    def _measure_background_noise(self):
        """
        Measure the background noise level for 2-3 seconds and set the silence threshold.
        """
        silence_margin = 2.5  # Add a 250% margin to background noise level
        print(f"INFO: Measuring background noise...")

        spinner = ['|', '/', '-', '\\']  # Spinner symbols
        try:
            background_rms_values = []
            total_frames = 20  # 20 frames ~ 2-3 seconds

            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=8000,  # Larger block size
                latency='high'  # High latency to reduce underruns
            ) as stream:
                for i in range(total_frames):
                    data, _ = stream.read(4000)

                    # Prepare and amplify the data stream
                    rms = self._prepare_audio_data(self._amplify_audio(data))
                    background_rms_values.append(rms)

                    # Display spinner animation with clear line
                    spinner_frame = spinner[i % len(spinner)]  # Rotate spinner symbol
                    print(f"\rSTAT: Measuring Noise Level... {spinner_frame}", end="", flush=True)
                    time.sleep(0.1)  # Simulate processing time for smooth animation

                # Clear the spinner and print the final result
                print("\r", end="", flush=True)  # Clear spinner line

            # Calculate the threshold
            if background_rms_values:  # Ensure the list is not empty
                background_noise = np.mean(background_rms_values)
            else:
                background_noise = 0  # Fallback if no valid values are collected
            self.silence_threshold = max(background_noise * silence_margin, 10)  # Avoid setting a very low threshold

            # Convert the threshold to dB for ease of reading
            db = 20 * np.log10(self.silence_threshold)  # Convert RMS to decibels

            # Clear the spinner and print the result
            print(f"\r{' ' * 40}\r", end="", flush=True)  # Clear the line
            print(f"INFO: Silence threshold set to: {db:.2f} dB")

        except Exception as e:
            print(f"ERROR: Failed to measure background noise: {e}")

    def _prepare_audio_data(self, data):
        """
        Prepare audio data for RMS calculation.
        """
        if data.size == 0:
            return 0

        data = data.astype(np.float32)  # Convert to float32 for calculation
        rms = np.sqrt(np.mean(data ** 2))  # Calculate RMS
        return rms

    def _amplify_audio(self, data):
        """
        Amplify audio data by a set gain.
        """
        gain = 4.0
        amplified = np.clip(data * gain, -32768, 32767).astype(np.int16)  # Clip and convert back to int16
        return amplified

if __name__ == "__main__":
    stt_manager = STTManager(sample_rate=44100)
    stt_manager._measure_background_noise()
