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
import concurrent.futures
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
        self.MAX_RECORDING_FRAMES = int(
            self.config['STT'].get('max_recording_frames', 100)
        )
        self.LISTEN_SILENT_FRAMES = int(
            self.config['STT'].get('listen_silent_frames', 50)
        )
        self.END_SPEECH_SILENT_FRAMES = int(
            self.config['STT'].get('end_speech_silent_frames', 8)
        )
        self.MAX_EMPTY_LISTEN_ATTEMPTS = int(
            self.config['STT'].get('max_empty_listen_attempts', 3)
        )
        self.WHISPER_TIMEOUT_SEC = int(
            self.config['STT'].get('whisper_timeout_sec', 45)
        )
        self.mic_index = None
        self.silence_margin = 3.5
        self.WAKE_WORDS = self._parse_wake_words(self.config['STT']['wake_word'])
        self.TARS_RESPONSES = self._load_wake_responses()
        self._load_vosk_model()
        self._measure_background_noise()
        self._silence_bar_visible = False
        self.vadmethod = self.config['STT']['vad_method']
        self.stt_debug = str(
            self.config['STT'].get('stt_debug', 'true')
        ).lower() in ('true', '1', 'yes')
        self._stt_debug_t0 = time.time()

    def _stt_debug_reset(self):
        self._stt_debug_t0 = time.time()

    def _stt_debug(self, msg):
        if not self.stt_debug:
            return
        self._clear_silence_bar(finish_line=True)
        elapsed = time.time() - self._stt_debug_t0
        print(f"[STT +{elapsed:6.2f}s] {msg}", flush=True)

    def _parse_wake_words(self, wake_raw):
        """Comma-separated wake phrases, e.g. amy,yooni,hey tar."""
        if not wake_raw:
            return []
        return [
            w.strip().lower()
            for w in wake_raw.replace('，', ',').split(',')
            if w.strip()
        ]

    _AUDIO_EXTS = ('mp3', 'wav', 'ogg', 'm4a')

    def _text_audio_name_candidates(self, text):
        """Filename stems to search for a wake line's audio clip."""
        raw = text.strip()
        no_punct = raw.replace('?', '').replace('~', '').replace('!', '').strip()
        seen = set()
        candidates = []
        for value in (raw, no_punct, no_punct.replace(' ', '_'), no_punct.replace(' ', '')):
            if value and value not in seen:
                seen.add(value)
                candidates.append(value)
        return candidates

    def _wake_audio_search_dirs(self, base_dir, audio_dir_rel):
        """Candidate folders for pre-recorded wake clips (config path + common fallbacks)."""
        dirs = []
        seen = set()
        candidates = []
        if audio_dir_rel:
            candidates.append(audio_dir_rel)
        candidates.extend([
            'character/TARS/yooni_wakeword',
            'yooni_wakeword',
            'character/TARS/wake_audio_ko',
        ])
        for rel in candidates:
            path = os.path.join(base_dir, rel.replace('/', os.path.sep))
            norm = os.path.normpath(path)
            if norm in seen:
                continue
            seen.add(norm)
            if os.path.isdir(norm):
                dirs.append(norm)
        return dirs

    def _find_audio_in_dir(self, audio_dir, stem):
        """Find audio by stem; Linux is case-sensitive so also scan .MP3 etc."""
        if not audio_dir or not stem:
            return None
        for ext in self._AUDIO_EXTS:
            candidate = os.path.join(audio_dir, f"{stem}.{ext}")
            if os.path.isfile(candidate):
                return candidate
        try:
            target = stem.lower()
            for name in os.listdir(audio_dir):
                base, dot, suffix = name.rpartition('.')
                if not dot or suffix.lower() not in self._AUDIO_EXTS:
                    continue
                if base.lower() == target:
                    return os.path.join(audio_dir, name)
        except OSError:
            pass
        return None

    def _resolve_audio_file(self, base_dir, audio_dirs, filename):
        if not filename:
            return None
        filename = filename.strip().replace('/', os.path.sep)
        if os.path.isabs(filename):
            if os.path.isfile(filename):
                return filename
            base = os.path.basename(filename)
            parent = os.path.dirname(filename)
            if parent and os.path.isdir(parent):
                found = self._find_audio_in_dir(parent, os.path.splitext(base)[0])
                if found:
                    return found
            return None
        search_roots = list(audio_dirs) if audio_dirs else []
        search_roots.append(base_dir)
        for root in search_roots:
            candidate = os.path.join(root, filename)
            if os.path.isfile(candidate):
                return candidate
            try:
                for name in os.listdir(root):
                    if name.lower() == filename.lower():
                        return os.path.join(root, name)
            except OSError:
                pass
            stem = filename.rsplit('.', 1)[0] if '.' in filename else filename
            found = self._find_audio_in_dir(root, stem)
            if found:
                return found
            compact = stem.replace(' ', '')
            if compact != stem:
                found = self._find_audio_in_dir(root, compact)
                if found:
                    return found
        return None

    def _find_audio_by_text(self, audio_dirs, text):
        if not audio_dirs:
            return None
        for audio_dir in audio_dirs:
            for stem in self._text_audio_name_candidates(text):
                found = self._find_audio_in_dir(audio_dir, stem)
                if found:
                    return found
        return None

    def _find_audio_by_index(self, audio_dirs, idx):
        if not audio_dirs:
            return None
        for audio_dir in audio_dirs:
            for ext in self._AUDIO_EXTS:
                for name in (f"{idx + 1:02d}.{ext}", f"{idx + 1}.{ext}"):
                    candidate = os.path.join(audio_dir, name)
                    if os.path.isfile(candidate):
                        return candidate
            try:
                prefix = f"{idx + 1:02d}."
                prefix2 = f"{idx + 1}."
                for name in os.listdir(audio_dir):
                    lower = name.lower()
                    if lower.startswith(prefix) or lower.startswith(prefix2):
                        suffix = name.rsplit('.', 1)[-1].lower()
                        if suffix in self._AUDIO_EXTS:
                            return os.path.join(audio_dir, name)
            except OSError:
                pass
        return None

    def _load_wake_manifest(self, audio_dirs):
        """Optional manifest.json: explicit text -> audio pairing (no index guessing)."""
        for audio_dir in audio_dirs or []:
            path = os.path.join(audio_dir, 'manifest.json')
            if not os.path.isfile(path):
                continue
            try:
                with open(path, encoding='utf-8') as f:
                    data = json.load(f)
            except (OSError, json.JSONDecodeError) as e:
                queue_message(f"WARNING: wake manifest invalid ({path}): {e}")
                continue

            mapping = {}
            if isinstance(data, list):
                for item in data:
                    if not isinstance(item, dict):
                        continue
                    text = (item.get('text') or '').strip()
                    audio = item.get('audio') or item.get('file')
                    if text and audio:
                        mapping[text] = str(audio).strip()
            elif isinstance(data, dict):
                for text, audio in data.items():
                    text = str(text).strip()
                    if text and audio:
                        mapping[text] = str(audio).strip()
            if mapping:
                return mapping
        return {}

    def _load_wake_responses(self):
        """Load wake lines; each entry keeps text and its own audio_path in sync."""
        defaults = [
            {"text": "Yes? What do you need?", "audio_path": None},
            {"text": "Ready and listening.", "audio_path": None},
            {"text": "At your service.", "audio_path": None},
        ]
        rel_path = self.config['STT'].get('wake_responses_file', '')
        if not rel_path:
            return defaults

        base_dir = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
        path = os.path.join(base_dir, rel_path)
        if not os.path.isfile(path):
            queue_message(f"WARNING: wake_responses_file not found ({path}), using defaults.")
            return defaults

        with open(path, encoding='utf-8') as f:
            raw_lines = [
                line.strip()
                for line in f
                if line.strip() and not line.strip().startswith('#')
            ]
        if not raw_lines:
            queue_message(f"WARNING: wake_responses_file is empty ({path}), using defaults.")
            return defaults

        prerecorded = str(
            self.config['STT'].get('wake_prerecorded', 'false')
        ).lower() in ('true', '1', 'yes')
        audio_dir_rel = self.config['STT'].get('wake_audio_dir', '')
        audio_dirs = self._wake_audio_search_dirs(base_dir, audio_dir_rel)
        use_index = str(
            self.config['STT'].get('wake_audio_by_index', 'false')
        ).lower() in ('true', '1', 'yes')
        manifest = self._load_wake_manifest(audio_dirs)

        entries = []
        for idx, raw in enumerate(raw_lines):
            text = raw
            audio_spec = None
            if '|' in raw:
                text, audio_spec = raw.split('|', 1)
                text = text.strip()
                audio_spec = audio_spec.strip()

            audio_path = None
            if prerecorded:
                if text in manifest:
                    audio_path = self._resolve_audio_file(
                        base_dir, audio_dirs, manifest[text]
                    )
                if not audio_path and audio_spec:
                    audio_path = self._resolve_audio_file(
                        base_dir, audio_dirs, audio_spec
                    )
                if not audio_path:
                    audio_path = self._find_audio_by_text(audio_dirs, text)
                if not audio_path and use_index:
                    audio_path = self._find_audio_by_index(audio_dirs, idx)

            entries.append({"text": text, "audio_path": audio_path})

        if prerecorded:
            with_audio = sum(1 for e in entries if e['audio_path'])
            dir_label = audio_dir_rel or '(auto-detect)'
            queue_message(
                f"LOAD: Wake responses={len(entries)} paired audio={with_audio} "
                f"dir={dir_label}"
            )
            if audio_dirs:
                queue_message(f"LOAD: wake audio search dirs: {', '.join(audio_dirs)}")
            else:
                queue_message(
                    "WARNING: No wake audio folder found. Put mp3 in "
                    "character/TARS/yooni_wakeword/ (see manifest.json)."
                )
            for entry in entries:
                audio_label = (
                    os.path.basename(entry['audio_path'])
                    if entry['audio_path'] else 'MISSING'
                )
                queue_message(f"LOAD: wake '{entry['text']}' -> {audio_label}")

        return entries

    def _pick_wake_response(self):
        """Random wake entry; pre-recorded mode only picks fully paired entries."""
        pool = self.TARS_RESPONSES
        prerecorded = str(
            self.config['STT'].get('wake_prerecorded', 'false')
        ).lower() in ('true', '1', 'yes')
        if prerecorded:
            paired = [e for e in pool if e.get('text') and e.get('audio_path')]
            if paired:
                return random.choice(paired)
            queue_message(
                "WARNING: wake_prerecorded=true but no text+audio pairs; falling back to TTS."
            )
        return random.choice(pool)

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
        self._release_audio_input()
        if self.thread and self.thread.is_alive():
            self.thread.join(timeout=5.0)

    def _release_audio_input(self, pause=0.05):
        """Release PortAudio/ALSA input. Short pause by default for faster turn-around."""
        try:
            sd.stop()
        except Exception as e:
            print(f"WARNING: sd.stop() failed: {e}")
        if pause > 0:
            time.sleep(pause)

    def _new_wake_recognizer(self):
        return KaldiRecognizer(self.vosk_model, self.SAMPLE_RATE)

# Progress bar
    def _update_silence_bar(self, frames, max_frames):
        """In-place silence bar (same line, does not erase USER/TARS above)."""
        bar_length = 10
        progress = int((frames / max_frames) * bar_length)
        filled = "#" * progress
        empty = "-" * (bar_length - progress)
        bar = f"[SILENCE: {filled}{empty}] {frames}/{max_frames}"
        print(f"\r{bar}", end="", flush=True)
        self._silence_bar_visible = True

    def _clear_silence_bar(self, finish_line=False):
        if not self._silence_bar_visible:
            return
        if finish_line:
            print(flush=True)
        else:
            print("\r" + " " * 50 + "\r", end="", flush=True)
        self._silence_bar_visible = False

    def update_bar(self, frames, max_frames):
        self._update_silence_bar(frames, max_frames)

    def clear_bar(self):
        self._clear_silence_bar(finish_line=False)

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
            self.DEBUG = False
            rms = self.prepare_audio_data(self.amplify_audio(data))
            self.silence_threshold_margin = self.silence_threshold * self.silence_margin

            if rms is None:
                # Even if RMS calculation fails, return proper tuple
                return False, detected_speech, silent_frames

            if rms > self.silence_threshold_margin:
                if not detected_speech:
                    self._stt_debug(
                        f"VAD: speech detected rms={rms:.1f} "
                        f"threshold={self.silence_threshold_margin:.1f}"
                    )
                detected_speech = True
                silent_frames = 0
                
                if self.DEBUG:
                    queue_message(f"AUDIO: {rms:.2f}/{self.silence_threshold:.2f}/{self.silence_threshold_margin:.2f}")
                
                self._clear_silence_bar()
            else:
                silent_frames += 1

                if self.DEBUG:
                    queue_message(f"SILENT: {rms:.2f}/{self.silence_threshold:.2f}/{self.silence_threshold_margin:.2f}")

                if detected_speech:
                    bar_limit = self.END_SPEECH_SILENT_FRAMES
                    end_speech = silent_frames > self.END_SPEECH_SILENT_FRAMES
                else:
                    bar_limit = self.LISTEN_SILENT_FRAMES
                    end_speech = False

                self._update_silence_bar(silent_frames, bar_limit)

                if end_speech:
                    self._stt_debug(
                        f"VAD: end-of-utterance silence "
                        f"frames={silent_frames}/{self.END_SPEECH_SILENT_FRAMES}"
                    )
                    self._clear_silence_bar()
                    return True, detected_speech, silent_frames

                if not detected_speech and silent_frames >= self.LISTEN_SILENT_FRAMES:
                    self._stt_debug(
                        f"VAD: idle silence "
                        f"frames={silent_frames}/{self.LISTEN_SILENT_FRAMES}"
                    )
                    self._clear_silence_bar()
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
                    self._stt_debug_reset()
                    self._stt_debug("wake session start")
                    empty_attempts = 0
                    while self.running:
                        self._stt_debug(
                            f"listen attempt {empty_attempts + 1}/"
                            f"{self.MAX_EMPTY_LISTEN_ATTEMPTS}"
                        )
                        if self._transcribe_utterance():
                            empty_attempts = 0
                            self._stt_debug("listen attempt succeeded")
                        else:
                            empty_attempts += 1
                            self._stt_debug(
                                f"listen attempt failed "
                                f"({empty_attempts}/{self.MAX_EMPTY_LISTEN_ATTEMPTS})"
                            )
                            if empty_attempts >= self.MAX_EMPTY_LISTEN_ATTEMPTS:
                                self._stt_debug("empty limit reached → sleeping")
                                break
                    self._release_audio_input()
                    self._stt_debug("wake session end")
        except Exception as e:
            print(f"ERROR: Error in STT processing loop: {e}")
        finally:
            print(f"INFO: STT Manager stopped.")

#Detect Wake
    def _detect_wake_word(self) -> bool:
        """
        Detect the wake word using Vosk recognizer.
        Reopens the mic stream only on errors — not on a fixed timer.
        """
        MAX_SILENT_READS = 80  # ~8s of dead frames before reopen
        MAX_OVERFLOW_STREAK = 200  # reopen only after sustained xruns (silent)

        if self.config['STT']['use_indicators']:
            self.play_beep(400, 0.1, 44100, 0.6)  # sleeping tone
        print(f"TARS: Sleeping...")

        mic_index = self._get_input_device()
        last_heartbeat = time.time()
        wake_response = None

        while self.running and not self.shutdown_event.is_set() and wake_response is None:
            recognizer = self._new_wake_recognizer()
            last_partial = ""
            silent_reads = 0
            overflow_streak = 0

            try:
                self._release_audio_input()
                with sd.InputStream(
                    samplerate=self.SAMPLE_RATE,
                    channels=1,
                    dtype='int16',
                    blocksize=4000,
                    latency='high',
                    device=mic_index
                ) as stream:
                    print("InputStream opened successfully")
                    while self.running and not self.shutdown_event.is_set():
                        if time.time() - last_heartbeat > 60:
                            print("DEBUG: Waiting for wake word...")
                            last_heartbeat = time.time()

                        data, overflowed = stream.read(4000)
                        if overflowed:
                            overflow_streak += 1
                            if overflow_streak >= MAX_OVERFLOW_STREAK:
                                break
                        else:
                            overflow_streak = 0

                        if data is None or data.size == 0:
                            silent_reads += 1
                        elif np.max(np.abs(data)) == 0:
                            silent_reads += 1
                        else:
                            silent_reads = 0

                        if silent_reads >= MAX_SILENT_READS:
                            print("WARNING: mic input dead, reopening stream.")
                            break

                        if recognizer.AcceptWaveform(data.tobytes()):
                            result = json.loads(recognizer.Result())
                            text = result.get("text", "").lower()
                            if text:
                                print(f"DEBUG: Vosk recognized text: {text}")
                            if self.WAKE_WORDS and any(w in text for w in self.WAKE_WORDS):
                                if self.config['STT']['use_indicators']:
                                    self.play_beep(1200, 0.1, 44100, 0.8)
                                wake_response = self._pick_wake_response()
                                break
                            last_partial = ""
                        else:
                            partial = json.loads(recognizer.PartialResult())
                            ptext = partial.get("partial", "").strip().lower()
                            if ptext and ptext != last_partial and len(ptext) > 2:
                                print(f"DEBUG: Vosk partial: {ptext}")
                                last_partial = ptext

            except Exception as e:
                print(f"ERROR: Wake word detection stream failed: {e}")
                self.mic_index = None
                self._release_audio_input(pause=0.3)
                try:
                    mic_index = self._get_input_device()
                except Exception as dev_err:
                    print(f"ERROR: Could not re-open mic: {dev_err}")
                time.sleep(2)

        if wake_response:
            label = wake_response.get('text', wake_response) if isinstance(wake_response, dict) else wake_response
            audio_path = wake_response.get('audio_path') if isinstance(wake_response, dict) else None
            print(f"TARS: {label}", flush=True)
            if audio_path:
                self._stt_debug(f"wake play text='{label}' file={os.path.basename(audio_path)}")
            else:
                self._stt_debug(f"wake TTS text='{label}'")
            self._release_audio_input(pause=0.05)
            if self.wake_word_callback:
                t0 = time.time()
                self.wake_word_callback(wake_response)
                self._stt_debug(f"wake audio done ({time.time() - t0:.2f}s)")
            return True

        return False

#Transcripe functions
    def _transcribe_utterance(self):
        """
        Process a user utterance after wake word detection.
        Returns True if speech was transcribed and handled.
        """
        print(f"\nSTAT: Listening...")
        self._stt_debug(f"transcribe start processor={self.config['STT'].get('stt_processor')}")
        result = None
        try:
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
        except Exception as e:
            print(f"ERROR: Utterance transcription failed: {e}")
            self._stt_debug(f"transcribe exception: {e}")
        finally:
            self.clear_bar()

        self._stt_debug(f"transcribe end success={bool(result)}")
        return bool(result)

    def _transcribe_with_faster_whisper(self):
        """Transcribe audio using Faster-Whisper."""
        audio_buffer = BytesIO()
        detected_speech = False
        silent_frames = 0
        mic_index = self._get_input_device()

        self._stt_debug("whisper: opening InputStream")
        try:
            with sd.InputStream(
                samplerate=self.SAMPLE_RATE,
                channels=1,
                dtype="int16",
                device=mic_index,
                blocksize=4000,
                latency="high",
            ) as stream, wave.open(audio_buffer, "wb") as wf:
                wf.setnchannels(1)
                wf.setsampwidth(2)
                wf.setframerate(self.SAMPLE_RATE)
                self._stt_debug(
                    f"whisper: recording max_frames={self.MAX_RECORDING_FRAMES} "
                    f"idle_silence={self.LISTEN_SILENT_FRAMES} "
                    f"end_speech_silence={self.END_SPEECH_SILENT_FRAMES}"
                )
                for frame_i in range(self.MAX_RECORDING_FRAMES):
                    if not self.running or self.shutdown_event.is_set():
                        self._stt_debug("whisper: aborted (shutdown)")
                        return None

                    t_read = time.time()
                    data, overflowed = stream.read(4000)
                    read_ms = (time.time() - t_read) * 1000
                    if read_ms > 300:
                        self._stt_debug(
                            f"whisper: SLOW stream.read {read_ms:.0f}ms "
                            f"frame={frame_i} overflow={overflowed}"
                        )

                    is_silence, detected_speech, silent_frames = self.voice_activity_detection_main(
                        data, detected_speech, silent_frames
                    )

                    if is_silence:
                        if not detected_speech:
                            self._stt_debug(
                                f"whisper: no speech timeout silent={silent_frames}"
                            )
                            return None
                        self._stt_debug("whisper: speech ended (silence after speech)")
                        break

                    if not detected_speech and silent_frames >= self.LISTEN_SILENT_FRAMES:
                        self._stt_debug(
                            f"whisper: idle timeout silent={silent_frames}"
                        )
                        return None

                    wf.writeframes(data.tobytes())
                else:
                    self._stt_debug("whisper: max recording frames reached")
        except Exception as e:
            self._stt_debug(f"whisper: record error: {e}")
            raise
        finally:
            self.clear_bar()

        if not detected_speech:
            self._stt_debug("whisper: no speech in buffer")
            return None

        audio_buffer.seek(0)
        nbytes = audio_buffer.getbuffer().nbytes
        self._stt_debug(f"whisper: recorded {nbytes} bytes")

        if nbytes == 0:
            queue_message("ERROR: No audio recorded.")
            return None

        t0 = time.time()
        audio_data, sample_rate = sf.read(audio_buffer, dtype="float32")
        self._stt_debug(f"whisper: sf.read ({time.time() - t0:.2f}s)")
        audio_data = np.clip(audio_data, -1.0, 1.0)
        TARGET_SAMPLE_RATE = 16000

        if sample_rate != TARGET_SAMPLE_RATE:
            t0 = time.time()
            audio_data = librosa.resample(audio_data, orig_sr=sample_rate, target_sr=TARGET_SAMPLE_RATE)
            self._stt_debug(f"whisper: resample ({time.time() - t0:.2f}s)")

        stt_lang = self.config.get("STT", {}).get("stt_language", "en")
        whisper_kwargs = {"temperature": 0.0, "beam_size": 1}
        if stt_lang and stt_lang.lower() != "auto":
            whisper_kwargs["language"] = stt_lang

        if self.faster_whisper_model is None:
            queue_message("ERROR: Faster-Whisper model is not loaded.")
            return None

        self._stt_debug(f"whisper: inference start lang={stt_lang}")
        try:
            t0 = time.time()
            with concurrent.futures.ThreadPoolExecutor(max_workers=1) as executor:
                future = executor.submit(
                    self._run_whisper_transcribe, audio_data, whisper_kwargs
                )
                segments = future.result(timeout=self.WHISPER_TIMEOUT_SEC)
            self._stt_debug(f"whisper: inference done ({time.time() - t0:.2f}s)")
        except concurrent.futures.TimeoutError:
            self._stt_debug(f"whisper: inference TIMEOUT {self.WHISPER_TIMEOUT_SEC}s")
            queue_message(
                f"ERROR: Faster-Whisper timed out after {self.WHISPER_TIMEOUT_SEC}s."
            )
            return None
        except Exception as e:
            self._stt_debug(f"whisper: inference error: {e}")
            queue_message(f"ERROR: Faster-Whisper transcription failed: {e}")
            return None

        transcribed_text = " ".join(segment.text for segment in segments).strip()
        self._stt_debug(f"whisper: text={transcribed_text!r}")
        if transcribed_text:
            formatted_result = {"text": transcribed_text}
            if self.utterance_callback:
                self._stt_debug("utterance_callback start (LLM+TTS)")
                t0 = time.time()
                self.utterance_callback(json.dumps(formatted_result))
                self._stt_debug(f"utterance_callback done ({time.time() - t0:.2f}s)")
            return formatted_result

        self._stt_debug("whisper: empty transcription")
        print("INFO: No transcription from Faster-Whisper (empty result).")
        return None

    def _run_whisper_transcribe(self, audio_data, whisper_kwargs):
        segments, _ = self.faster_whisper_model.transcribe(audio_data, **whisper_kwargs)
        return list(segments)

    def _transcribe_with_vosk(self):
        """
        Transcribe audio using the local Vosk model.
        """
        recognizer = KaldiRecognizer(self.vosk_model, self.SAMPLE_RATE)
        detected_speech = False
        silent_frames = 0
        max_silent_frames = 20  # Adjust based on desired duration (~1.25 seconds)

        mic_index = self._get_input_device()
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
            mic_index = self._get_input_device()
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

            mic_index = self._get_input_device()
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

    def _get_input_device(self):
        """Return cached mic index; resolve once at first use."""
        if self.mic_index is None:
            self.mic_index = self._resolve_input_device()
        return self.mic_index

    def _resolve_input_device(self):
        """Select the first available input device."""
        try:
            devices = sd.query_devices()
            for i, device in enumerate(devices):
                if device['max_input_channels'] > 0:
                    print(f"Using input device: {device['name']} (Index: {i})")
                    return i
            raise ValueError("No suitable input devices found.")
        except Exception as e:
            print(f"Error detecting input device: {e}")
            raise

    def _get_default_input_device(self):
        return self._get_input_device()

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
