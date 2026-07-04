"""
module_stt.py

Speech-to-Text (STT) Module for TARS-AI Application.

This module integrates both local and server-based transcription, wake word detection, 
and voice command handling. It supports custom callbacks to trigger actions upon 
detecting speech or specific keywords.
"""

# === Standard Libraries ===
import os
import random
import sounddevice as sd
from vosk import Model, KaldiRecognizer
from pocketsphinx import LiveSpeech
import threading
import queue
import requests
from scipy.signal import resample_poly
from datetime import datetime
from io import BytesIO
import time
import wave
import numpy as np
import json
from typing import Callable, Optional
from vosk import SetLogLevel

# Suppress Vosk logs by setting the log level to 0 (ERROR and above)
SetLogLevel(-1)  # Adjust to 0 for minimal output or -1 to suppress all logs

#needed to supress warning
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# === Class Definition ===
class STTManager:
    def __init__(self, config, shutdown_event: threading.Event, amp_gain: float = 10.0):
        """
        Initialize the STTManager.

        Parameters:
        - config (dict): Configuration dictionary.
        - shutdown_event (Event): Event to signal stopping the assistant.
        """
        self.config = config
        self.shutdown_event = shutdown_event
        self.SAMPLE_RATE = 44100
        self.device_index = self._get_default_input_device()

        self.running = False
        self.amp_gain = amp_gain
        self.audio_queue = queue.Queue()

        vosk_model_path = os.path.join(os.getcwd(), "..", "stt", self.config['STT']['vosk_model'])
        if not os.path.exists(vosk_model_path):
            raise FileNotFoundError(f"Vosk model not found at {vosk_model_path}")

        self.vosk_model = Model(vosk_model_path)
        self.recognizer = KaldiRecognizer(self.vosk_model, self.SAMPLE_RATE)

        self.WAKE_WORD = config['STT']['wake_word'].lower()
        self.wake_word_callback: Optional[Callable[[str], None]] = None
        self.utterance_callback: Optional[Callable[[str], None]] = None
        self.post_utterance_callback: Optional[Callable] = None

        self.detecting_wake = True

#Main Thread Calls
    def start(self):
        """
        Start the STTManager in a separate thread.
        """
        self.running = True
        self.thread = threading.Thread(
            target=self._audio_loop, daemon=True
        )
        self.thread.start()

    def stop(self):
        """
        Stop the STTManager.
        """
        self.running = False
        self.shutdown_event.set()
        self.thread.join()

    def _audio_loop(self):
        print("INFO: STT Listening loop started.")
        try:
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype='int16',
                blocksize=8000,
                latency='high',
                device=self.device_index
            ) as stream:
                while self.running and not self.shutdown_event.is_set():
                    data, _ = stream.read(4000)
                    data = self._amplify_audio(data)

                    self.audio_queue.put(data)

                    if self.detecting_wake:
                        if self.recognizer.AcceptWaveform(data.tobytes()):
                            result = json.loads(self.recognizer.Result())
                            text = result.get("text", "").lower()
                            print(f"DEBUG: Vosk recognized text : {text}")
                            if self.WAKE_WORD in text:
                                self._on_wake_word_detected()
        except Exception as e:
            print(f"ERROR: Audio loop failure: {e}")

    def _on_wake_word_detected(self):
        print(f"TARS: Wake word detected.")
        self.detecting_wake = False
        if self.wake_word_callback:
            self.wake_word_callback(random.choice([
                "Ready.", "Yes?", "At your service.", "What can I do for you?"
            ]))

        result = self._transcribe()
        if self.utterance_callback and result:
            self.utterance_callback(result)

        if self.post_utterance_callback:
            self.post_utterance_callback()

        self.detecting_wake = True

    def _transcribe(self):
        import librosa

        print("TARS: Listening for utterance...")
        combined = []
        silent = 0
        max_silence = 5

        for i in range(50):
            try:
                data = self.audio_queue.get(timeout=1)
            except queue.Empty:
                print("DEBUG: audio_queue timeout")
                break

            rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
            print(f"DEBUG: Frame {i}, RMS = {rms}")

            if self._is_silence(data):
                silent += 1
                print(f"DEBUG: silence frame {silent}/{max_silence}")
                if silent > max_silence:
                    break
            else:
                silent = 0
                combined.append(data)

            if self.recognizer.AcceptWaveform(data.tobytes()):
                result = self.recognizer.Result()
                print("TARS: Utterance detected.")
                return result


        # ✅ 루프 끝나기 전에 combined 전체로 재시도
        if combined:
            full_data = b''.join([frame.tobytes() for frame in combined])
            if self.recognizer.AcceptWaveform(full_data):
                result = self.recognizer.Result()
                print("TARS: Utterance detected (from combined data).")
                return result
            else:
                result = self.recognizer.FinalResult()
                print(f"DEBUG: Final result = {result}")
                return result

        return None

    def _is_silence(self, data):
        rms = np.sqrt(np.mean(data.astype(np.float32) ** 2))
        return rms < 500

#Vosk INIT
    def _download_vosk_model(self, url, dest_folder):
        """Download the Vosk model from the specified URL with basic progress display."""
        file_name = url.split("/")[-1]
        dest_path = os.path.join(dest_folder, file_name)

        print(f"INFO: Downloading Vosk model from {url}...")
        response = requests.get(url, stream=True)
        response.raise_for_status()

        total_size = int(response.headers.get('content-length', 0))
        downloaded_size = 0

        with open(dest_path, "wb") as file:
            for chunk in response.iter_content(chunk_size=8192):
                file.write(chunk)
                downloaded_size += len(chunk)
                progress = (downloaded_size / total_size) * 100 if total_size else 0
                print(f"\r[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: Download progress: {progress:.2f}%", end="")
                
        print(f"\n[{datetime.now().strftime('%Y-%m-%d %H:%M:%S')}] INFO: Download complete. Extracting...")
        if file_name.endswith(".zip"):
            import zipfile
            with zipfile.ZipFile(dest_path, 'r') as zip_ref:
                zip_ref.extractall(dest_folder)
            os.remove(dest_path)
            print(f"INFO: Zip file deleted.")
        print(f"INFO: Extraction complete.")

    def _load_vosk_model(self):
        """
        Initialize the Vosk model for local STT transcription.
        """
        if self.config['STT']['stt_processor'] == 'vosk':
            vosk_model_path = os.path.join(os.getcwd(), "stt", self.config['STT']['vosk_model'])
            if not os.path.exists(vosk_model_path):
                print(f"ERROR: Vosk model not found. Downloading...")
                download_url = f"https://alphacephei.com/vosk/models/{self.config['STT']['vosk_model']}.zip"  # Example URL
                self._download_vosk_model(download_url, os.path.join(os.getcwd(), "stt"))
                print(f"INFO: Restarting model loading...")
                self._load_vosk_model()
                return

            self.vosk_model = Model(vosk_model_path)
            print(f"INFO: Vosk model loaded successfully.")

#MISC
    def _is_silence_detected(self, data, detected_speech, silent_frames, max_silent_frames):
        """
        Check if silence has been detected in the audio data.
        """
        rms = self.prepare_audio_data(data)

        # Silence detection logic
        #if rms < self.silence_threshold:
            #print(f"Silence {rms} rms | {self.silence_threshold} threshold")  # Voice detected
        #else:
            #print(f"SOUND__ {rms} rms | {self.silence_threshold} threshold")


        if rms > self.silence_threshold:  # Voice detected
            #if not detected_speech:
                #print(f"STAT: Speech detected.")
            detected_speech = True
            silent_frames = 0  # Reset silent frames
        else:  # Silence detected
            silent_frames += 1
            if silent_frames > max_silent_frames:
                #print(f"STAT: Silence detected.")
                return True, detected_speech, silent_frames

        return False, detected_speech, silent_frames

    def prepare_audio_data(self, data: np.ndarray) -> Optional[float]:
        """
        Prepare and sanitize audio data for further processing.
        - Flattens data.
        - Sanitizes invalid or extreme values.
        - Calculates and returns RMS value.

        Parameters:
        - data (np.ndarray): Raw audio data.

        Returns:
        - Optional[float]: RMS value of the audio data, or None if the data is invalid.
        """
        if data.size == 0:
            print(f"WARNING: Received empty audio data.")
            return None  # Invalid data

        # Flatten and sanitize audio data
        data = data.reshape(-1).astype(np.float64)  # Convert to 1D and float64 for precision
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)  # Replace invalid values
        data = np.clip(data, -32000, 32000)  # Clip extreme values to avoid issues

        # Check for invalid or silent data
        if np.all(data == 0):
            print(f"WARNING: Audio data is all zeros or silent.")
            return None  # Invalid data

        # Calculate RMS (Root Mean Square)
        try:
            rms = np.sqrt(np.mean(np.square(data)))
            return rms
        except Exception as e:
            print(f"ERROR: Failed to calculate RMS: {e}")
            return None  # Error during RMS calculation

    def _amplify_audio(self, data: np.ndarray) -> np.ndarray:
        """
        Amplify audio data using the set amplification gain.

        Parameters:
        - data (np.ndarray): Raw audio data.

        Returns:
        - np.ndarray: Amplified audio data.
        """
        return np.clip(data * self.amp_gain, -32768, 32767).astype(np.int16)
    
    def _get_default_input_device(self):
        """
        Automatically select the first available input device with audio input capabilities.
        """
        try:
            devices = sd.query_devices()
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:  # Device has input capability
                    print(f"Using input device: {device['name']} (Index: {i})")
                    return i
            raise ValueError("No suitable input devices found.")
        except Exception as e:
            print(f"Error detecting input device: {e}")
            return int(self.config['STT'].get("mic_device",1))

    def play_beep(self, frequency, duration, SAMPLE_RATE, volume):
        """
        Play a beep sound to indicate the system is listening.

        Parameters:
        - frequency (int): Frequency of the beep in Hz (e.g., 1000 for 1kHz).
        - duration (float): Duration of the beep in seconds.
        - SAMPLE_RATE (int): Sample rate in Hz (default: 44100).
        - volume (float): Volume of the beep (0.0 to 1.0).
        """
        # Generate a sine wave
        t = np.linspace(0, duration, int(SAMPLE_RATE * duration), endpoint=False)
        wave = volume * np.sin(2 * np.pi * frequency * t)
        
        # Play the sine wave
        sd.play(wave, samplerate=SAMPLE_RATE)
        sd.wait()  # Wait until the sound finishes playing

#Callbacks
    def set_wake_word_callback(self, callback: Callable[[str], None]):
        """
        Set the callback function for wake word detection.
        """
        self.wake_word_callback = callback

    def set_utterance_callback(self, callback: Callable[[str], None]):
        """
        Set the callback function for user utterance.
        """
        self.utterance_callback = callback

    def set_post_utterance_callback(self, callback):
        """
        Set a callback to execute after the utterance is handled.
        """
        self.post_utterance_callback = callback
