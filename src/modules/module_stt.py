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
import soundfile as sf
from vosk import Model, KaldiRecognizer
from faster_whisper import WhisperModel
from pocketsphinx import LiveSpeech
import threading
import requests
from datetime import datetime
from io import BytesIO
import time
import wave
import numpy as np
import json
import librosa
from typing import Callable, Optional
from vosk import SetLogLevel
from modules.module_messageQue import queue_message

# Suppress Vosk logs by setting the log level to 0 (ERROR and above)
SetLogLevel(-1)  # Adjust to 0 for minimal output or -1 to suppress all logs

#needed to supress warning
os.environ["TOKENIZERS_PARALLELISM"] = "false"

# === Class Definition ===
class STTManager:
    def __init__(self, config, shutdown_event: threading.Event, amp_gain: float = 4.0):
        """
        Initialize the STTManager.

        Parameters:
        - config (dict): Configuration dictionary.
        - shutdown_event (Event): Event to signal stopping the assistant.
        """
        self.config = config
        self.shutdown_event = shutdown_event
        self.SAMPLE_RATE = 44100
        self.running = False
        self.wake_word_callback: Optional[Callable[[str], None]] = None
        self.utterance_callback: Optional[Callable[[str], None]] = None
        self.amp_gain = amp_gain  # Amplification gain factor
        self.post_utterance_callback: Optional[Callable] = None
        self.vosk_model = None
        self.faster_whisper_model = None
        self._load_whisper_model()
        self.silence_threshold = 10  # Default value; updated dynamically
        self.MAX_SILENT_FRAMES = 100
        self.MAX_RECORDING_FRAMES = 60
        self.WAKE_WORD = self.config['STT']['wake_word']
        self.silence_margin = 3.5
        self.TARS_RESPONSES = [
            "Yes? What do you need?",
            "Ready and listening.",
            "At your service.",
            "Go ahead.",
            "What can I do for you?",
            "Listening. What's up?",
            "Here. What do you require?",
            "Yes? I'm here.",
            "Standing by.",
            "Online and awaiting your command."
        ]
        self._load_vosk_model()
        self._measure_background_noise()
        self.update_bar, self.clear_bar = self._init_progress_bar()
        self.vadmethod = self.config['STT']['vad_method']

#Main Thread Calls
    def start(self):
        """
        Start the STTManager in a separate thread.
        """
        self.running = True
        #self.thread = threading.Thread(
        #    target=self._stt_processing_loop, name="STTThread", daemon=True
        #)
        #self.thread.start()
        def run():
            while self.running:
                try:
                    self._stt_processing_loop()
                except Exception as e:
                    print(f"[WATCHDOG] STT loop crashed: {e}")
                    time.sleep(2)  # 재시작 전 잠시 대기

        self.thread = threading.Thread(target=run, name="STTWatchdog", daemon=True)
        self.thread.start()

    def stop(self):
        """
        Stop the STTManager.
        """
        self.running = False
        self.shutdown_event.set()
        self.thread.join()

#Progress bar
    def _init_progress_bar(self):
        """Initialize progress bar for silence tracking."""
        bar_length = 10

        def update_bar(frames, max_frames):
            progress = int((frames / max_frames) * bar_length)
            filled = "#" * progress
            empty = "-" * (bar_length - progress)
            bar = f"\r[SILENCE: {filled}{empty}] {frames}/{max_frames}"
            print(bar, end="", flush=True)

        def clear_bar():
            print("\r" + " " * (bar_length + 30) + "\r", end="", flush=True)

        return update_bar, clear_bar

    def prepare_audio_data(self, data: np.ndarray) -> Optional[float]:
        """
        Compute the RMS of the audio data.
        Returns:
            float or None: RMS value or None if invalid.
        """
        if data.size == 0:
            queue_message("WARNING: Empty audio data received.")
            return None
        data = data.reshape(-1).astype(np.float64)
        data = np.nan_to_num(data, nan=0.0, posinf=0.0, neginf=0.0)
        data = np.clip(data, -32000, 32000)
        if np.all(data == 0):
            queue_message("WARNING: Audio data is silent or all zeros.")
            return None
        try:
            return np.sqrt(np.mean(np.square(data)))
        except Exception as e:
            queue_message(f"ERROR: RMS calculation failed: {e}")
            return None

    def amplify_audio(self, data: np.ndarray) -> np.ndarray:
        """
        Amplify the input audio data using the configured amplification gain.
        """
        return np.clip(data * self.amp_gain, -32768, 32767).astype(np.int16)

    def _is_silence_detected_rms(self, data, detected_speech, silent_frames):
        """RMS-based silence detection with visual progress bar"""
        try:
            update_bar, clear_bar = self._init_progress_bar()
            self.DEBUG = False
            rms = self.prepare_audio_data(self.amplify_audio(data))
            self.silence_threshold_margin = self.silence_threshold * self.silence_margin

            if rms is None:
                # Even if RMS calculation fails, return proper tuple
                return False, detected_speech, silent_frames

            if rms > self.silence_threshold_margin:
                detected_speech = True
                silent_frames = 0
                
                if self.DEBUG:
                    queue_message(f"AUDIO: {rms:.2f}/{self.silence_threshold:.2f}/{self.silence_threshold_margin:.2f}")
                
                clear_bar()
            else:
                silent_frames += 1
                
                if self.DEBUG:
                    queue_message(f"SILENT: {rms:.2f}/{self.silence_threshold:.2f}/{self.silence_threshold_margin:.2f}")
                
                update_bar(silent_frames, self.MAX_SILENT_FRAMES)

                if silent_frames > self.MAX_SILENT_FRAMES:
                    clear_bar()
                    return True, detected_speech, silent_frames

            
            return False, detected_speech, silent_frames
        
        except Exception as e:
            queue_message(f"ERROR: RMS silence detection failed: {e}")
            # Return safe default values
            return False, detected_speech, silent_frames
  
    # === Audio adjustments ===

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

    def voice_activity_detection_main(self, data, detected_speech, silent_frames=0):
        """
        Determines if the current audio frame contains silence using VAD or RMS.
        Returns a tuple: (is_silence, detected_speech, silent_frames)
        """
        # Get the vad_method from the configuration, defaulting to "rms" if not set.
        #print(self.vadmethod)
    
        if self.vadmethod == "silero":
            return self._is_silence_detected_silero(data, detected_speech, silent_frames)
        elif self.vadmethod == "rms":
            return self._is_silence_detected_rms(data, detected_speech, silent_frames)
        else:
            return self._is_silence_detected_rms(data, detected_speech, silent_frames)

    def _load_vosk_model(self):
        """
        Initialize the Vosk model for local STT transcription.
        """
        # if self.config['STT']['stt_processor'] == 'vosk':
        vosk_model_path = os.path.join(os.getcwd(), "..", "stt", self.config['STT']['vosk_model'])
        if not os.path.exists(vosk_model_path):
            print(f"ERROR: Vosk model not found. Downloading...")
            download_url = f"https://alphacephei.com/vosk/models/{self.config['STT']['vosk_model']}.zip"
            self._download_vosk_model(download_url, os.path.join(os.getcwd(), "..", "stt"))
            print(f"INFO: Restarting model loading...")
            self._load_vosk_model()
            return

        self.vosk_model = Model(vosk_model_path)
        print(f"INFO: Vosk model loaded successfully.")

    def _load_whisper_model(self):
        if self.config["STT"]["stt_processor"] in ["faster-whisper", "whisper"]:
            model_size = self.config["STT"].get("whisper_model", "small")
            try:
                print(f"INFO: Loading Faster-Whisper model: {model_size}")
                self.faster_whisper_model = WhisperModel(model_size, compute_type="int8")
                print("INFO: Faster-Whisper model loaded successfully.")
            except Exception as e:
                print(f"ERROR: Failed to load Faster-Whisper model: {e}")

#Main Loop
    def _stt_processing_loop(self):
        """
        Main loop to detect wake words and process utterances.
        """
        try:
            while self.running:
                print("STT loop running...")
                if self.shutdown_event.is_set():
                    break
                if self._detect_wake_word():
                    # If wake word detected, transcribe the user utterance
                    self._transcribe_utterance()
        except Exception as e:
            print(f"ERROR: Error in STT processing loop: {e}")
        finally:
            print(f"INFO: STT Manager stopped.")

#Detect Wake
    def _detect_wake_word(self) -> bool:
        """
        Detect the wake word using Vosk recognizer.
        Automatically restarts stream every 300 seconds to prevent USB sleep.
        """
        WAKE_RESTART_INTERVAL = 300  # 5분마다 InputStream 재시작
        last_restart_time = time.time()

        if self.config['STT']['use_indicators']:
            self.play_beep(400, 0.1, 44100, 0.6)  # sleeping tone
        print(f"TARS: Sleeping...")

        recognizer = KaldiRecognizer(self.vosk_model, self.SAMPLE_RATE)
        mic_index = self._get_default_input_device()

        while True:
            try:
                with sd.InputStream(
                    samplerate=self.SAMPLE_RATE,
                    channels=1,
                    dtype='int16',
                    blocksize=8000,
                    latency='high',
                    device=mic_index
                ) as stream:
                    print("InputStream opened successfully")
                    while True:
                        # 주기적으로 stream 리셋
                        if time.time() - last_restart_time > WAKE_RESTART_INTERVAL:
                            print("INFO: Restarting mic input stream to prevent idle sleep.")
                            last_restart_time = time.time()
                            break  # 내부 while 탈출 → with 블록 탈출 → stream 재시작

                        data, _ = stream.read(4000)
                        if recognizer.AcceptWaveform(data.tobytes()):
                            result = json.loads(recognizer.Result())
                            text = result.get("text", "").lower()
                            print(f"DEBUG: Vosk recognized text: {text}")
                            if self.WAKE_WORD in text:
                                if self.config['STT']['use_indicators']:
                                    self.play_beep(1200, 0.1, 44100, 0.8)  # wake tone
                                wake_response = random.choice(self.TARS_RESPONSES)
                                print(f"TARS: {wake_response}")

                                if self.wake_word_callback:
                                    self.wake_word_callback(wake_response)
                                return True

            except Exception as e:
                print(f"ERROR: Wake word detection stream failed: {e}")
                time.sleep(2)  # 장치 재시도 유예시간

#Transcripe functions
    def _transcribe_utterance(self):
        """
        Process a user utterance after wake word detection.
        """
        print(f"STAT: Listening...")
        try:
            # Map "whisper" to faster-whisper as well.
            processor = self.config["STT"].get("stt_processor", "vosk")
            if processor in ["whisper", "faster-whisper"]:
                result = self._transcribe_with_faster_whisper()
            elif processor == "silero":
                result = self._transcribe_silero()
            elif processor == "external":
                result = self._transcribe_with_server()
            else:
                result = self._transcribe_with_vosk()
                print("transcribe_with_vosk() starts.")
            
            # Call post-utterance callback if utterance was detected recently, otherwise return to wake word detection
            if self.post_utterance_callback and result:
                if not hasattr(self, 'loopcheck'):
                    self.loopcheck = 0 

                self.loopcheck += 1
                print(f"loop check : {self.loopcheck}")
                
                self.post_utterance_callback()

        except Exception as e:
            print(f"ERROR: Utterance transcription failed: {e}")

    def _transcribe_with_faster_whisper(self):
        """Transcribe audio using Faster-Whisper."""
        audio_buffer = BytesIO()
        detected_speech = False
        silent_frames = 0
        max_silent_frames = self.MAX_SILENT_FRAMES

        with sd.InputStream(
            samplerate=self.SAMPLE_RATE, channels=1, dtype="int16"
        ) as stream, wave.open(audio_buffer, "wb") as wf:
            wf.setnchannels(1)
            wf.setsampwidth(2)
            wf.setframerate(self.SAMPLE_RATE)
            for _ in range(self.MAX_RECORDING_FRAMES):
                data, _ = stream.read(4000)

                is_silence, detected_speech, silent_frames = self.voice_activity_detection_main(data, detected_speech, silent_frames)
                if is_silence:
                    if not detected_speech:
                        return None
                    break

                wf.writeframes(data.tobytes())

        audio_buffer.seek(0)

        if audio_buffer.getbuffer().nbytes == 0:
            queue_message("ERROR: No audio recorded.")
            return None

        audio_data, sample_rate = sf.read(audio_buffer, dtype="float32")
        print("DEBUG: audio_data shape:", audio_data.shape)
        print("DEBUG: sample_rate:", sample_rate)
        
        audio_data = np.clip(audio_data, -1.0, 1.0)
        TARGET_SAMPLE_RATE = 16000

        if sample_rate != TARGET_SAMPLE_RATE:
            audio_data = librosa.resample(audio_data, orig_sr=sample_rate, target_sr=TARGET_SAMPLE_RATE)
            print("DEBUG: audio_data.shape after resample:",audio_data.shape)

        segments, _ = self.faster_whisper_model.transcribe(
            audio_data, temperature=0.0, beam_size=1, language="en"
        )
        segments = list(segments)

        transcribed_text = " ".join(segment.text for segment in segments).strip()
        if transcribed_text:
            formatted_result = {"text": transcribed_text}
            if self.utterance_callback:
                self.utterance_callback(json.dumps(formatted_result))
            return formatted_result
        else:
            queue_message("ERROR: No transcription from Faster-Whisper.")
            return None

    def _transcribe_with_vosk(self):
        """
        Transcribe audio using the local Vosk model.
        """
        recognizer = KaldiRecognizer(self.vosk_model, self.SAMPLE_RATE)
        detected_speech = False
        silent_frames = 0
        max_silent_frames = 20  # Adjust based on desired duration (~1.25 seconds)

        mic_index = self._get_default_input_device()
        with sd.InputStream(
            samplerate=self.SAMPLE_RATE,
            channels=1,
            dtype="int16",
            blocksize=8000,  # Larger block size
            latency='high',  # High latency to reduce underruns
            device=mic_index
        ) as stream:

            for i in range(50):  # Limit duration (~12.5 seconds)
                data, _ = stream.read(4000)
                data = self._amplify_audio(data)  # Apply amplification

                is_silence, detected_speech, silent_frames = self._is_silence_detected(
                    data, detected_speech, silent_frames, max_silent_frames
                )

                self.update_bar(silent_frames, max_silent_frames)

                if is_silence:
                    self.clear_bar()
                    break

                if recognizer.AcceptWaveform(data.tobytes()):
                    result = recognizer.Result()
                    print(f"DEBUG: Vosk final result: {result}")
                    if self.utterance_callback:
                        self.utterance_callback(result)
                    return result

        #print(f"INFO: No transcription within duration limit.")
        return None

    def _transcribe_with_server(self):
        """
        Transcribe audio by sending it to a server for processing.
        """
        try:
            audio_buffer = BytesIO()
            detected_speech = False
            silent_frames = 0
            max_silent_frames = 3  # ~1.25 seconds of silence

            print(f"STAT: Starting audio recording...")
            mic_index = self._get_default_input_device()
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=8000,  # Larger block size
                latency='high',  # High latency to reduce underruns
                device=mic_index
            ) as stream:
                with wave.open(audio_buffer, "wb") as wf:
                    wf.setnchannels(1)
                    wf.setsampwidth(2)
                    wf.setframerate(self.SAMPLE_RATE)

                    for _ in range(50):  # Limit maximum recording duration (~12.5 seconds)
                        data, _ = stream.read(4000)
                        data = self._amplify_audio(data)  # Apply amplification
                        wf.writeframes(data.tobytes())

                        is_silence, detected_speech, silent_frames = self._is_silence_detected(
                            data, detected_speech, silent_frames, max_silent_frames
                        )
                        if is_silence:
                            break

            # Ensure the audio buffer is not empty
            audio_buffer.seek(0)
            if audio_buffer.getbuffer().nbytes == 0:
                print(f"ERROR: Audio buffer is empty. No audio recorded.")
                return None

            print(f"STAT: Sending audio to server...")
            files = {"audio": ("audio.wav", audio_buffer, "audio/wav")}
            response = requests.post(f"{self.config['STT']['server_url']}/save_audio", files=files, timeout=10)

            if response.status_code == 200:
                transcription = response.json().get("transcription", [])
                if transcription:
                    raw_text = transcription[0].get("text", "").strip()
                    formatted_result = {
                        "text": raw_text,
                        "result": [
                            {"conf": 1.0, "start": seg.get("start", 0), "end": seg.get("end", 0), "word": seg.get("text", "")}
                            for seg in transcription
                        ],
                    }
                    if self.utterance_callback:
                        self.utterance_callback(json.dumps(formatted_result))
                    return formatted_result

        except requests.RequestException as e:
            print(f"ERROR: Server request failed: {e}")
        return None

#MISC
    def _is_silence_detected(self, data, detected_speech, silent_frames, max_silent_frames):
        """
        Check if silence has been detected in the audio data.
        """
        rms = self._prepare_audio_data(data)

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

    def prepare_audio_data_og(self, data: np.ndarray) -> Optional[float]:
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

    def amplify_audio_og(self, data: np.ndarray) -> np.ndarray:
        """
        Amplify audio data using the set amplification gain.

        Parameters:
        - data (np.ndarray): Raw audio data.

        Returns:
        - np.ndarray: Amplified audio data.
        """
        return np.clip(data * self.amp_gain, -32768, 32767).astype(np.int16)
    
    def _measure_background_noise_og(self):
        """
        Measure the background noise level for 2-3 seconds and set the silence threshold.
        """
        silence_margin = 2.5  # Add a 250% margin to background noise level
        print(f"INFO: Measuring background noise...")

        spinner = ['|', '/', '-', '\\']  # Spinner symbols
        try:
            background_rms_values = []
            total_frames = 20  # 20 frames ~ 2-3 seconds

            mic_index = self._get_default_input_device()
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                blocksize=8000,  # Larger block size
                latency='high',  # High latency to reduce underruns
                device=mic_index
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

            #convert the threshold to dbz for easy of reading
            db = 20 * np.log10(self.silence_threshold)  # Convert RMS to decibels

            # Clear the spinner and print the result
            print(f"\r{' ' * 40}\r", end="", flush=True)  # Clear the line
            print(f"INFO: Silence threshold set to: {db:.2f} dB")

        except Exception as e:
            print(f"ERROR: Failed to measure background noise: {e}")

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
            raise

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
