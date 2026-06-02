""" Created by mskull """

import subprocess
import re

from modules.module_config import load_config
from modules.module_messageQue import queue_message

# Loaded once at import. Provides [VOLUME] control/card and [CHAR] language.
_CONFIG = load_config()


class RaspbianVolumeManager:
    def __init__(self, control='Master', card=None):
        self.control = control
        # card may be an index ("2") or name; empty/None means the default card.
        self.card = card if card not in (None, '') else None

    def _amixer(self, *args):
        """Build an amixer command, targeting a specific card when configured."""
        cmd = ['amixer']
        if self.card is not None:
            cmd += ['-c', str(self.card)]
        cmd += list(args)
        return cmd

    def get_volume(self):
        try:
            # Executes the amixer command to get volume information
            output = subprocess.check_output(
                self._amixer('get', self.control),
                stderr=subprocess.STDOUT
            ).decode('utf-8')

            # Search for volume percentages in the output for both channels
            left_match = re.search(r'Front Left: Playback \d+ \[(\d+)%\]', output)
            right_match = re.search(r'Front Right: Playback \d+ \[(\d+)%\]', output)

            if left_match and right_match:
                # Calculate the average volume of both channels
                left_volume = int(left_match.group(1))
                right_volume = int(right_match.group(1))
                return (left_volume + right_volume) // 2

            elif left_match:  # If only the left channel is present
                return int(left_match.group(1))

            elif right_match:  # If only the right channel is present
                return int(right_match.group(1))

            # Fallback for mono controls such as a USB DAC 'PCM'
            # (e.g. "Mono: Playback 47 [70%] [-18.00dB] [on]").
            mono_match = re.search(r'\[(\d+)%\]', output)
            if mono_match:
                return int(mono_match.group(1))

            raise RuntimeError("Volume percentage not found in amixer output.")
        except subprocess.CalledProcessError as e:
            queue_message(f"Error getting volume: {e}")
            return None

    def set_volume(self, percent):
        if not (0 <= percent <= 100):
            raise ValueError("Volume percentage must be between 0 and 100.")
        try:
            subprocess.check_call(
                self._amixer('set', self.control, f'{percent}%'),
                stdout=subprocess.DEVNULL,
                stderr=subprocess.STDOUT
            )
            # Verify the current volume after setting it
            current_volume = self.get_volume()
            queue_message(f"Volume set to {percent}%. Current volume is {current_volume}%.")
        except subprocess.CalledProcessError as e:
            queue_message(f"Error setting volume: {e}")


def correct_transcription(transcribed_text):
    """
    Corrects common misinterpretations of volume commands by the STT module.
    Parameters:
        transcribed_text (str): The transcribed user input.
    Returns:
        str: The corrected user input.
    """
    corrections = {
        "the grease volume": "decrease volume",
        "degrees volume": "decrease volume",
        "the greek volume": "decrease volume",
        "the great volume": "decrease volume",
        "the greece volume": "decrease volume",
        "the brief volume": "decrease volume",
        "the grief volume": "decrease volume",
        "increase volume": "increase volume",
        "reduce volume": "decrease volume",
    }
    for wrong, correct in corrections.items():
        if wrong in transcribed_text.lower():
            suggestion = "increase" if "increase" in correct else "decrease"
            queue_message(f"I think I heard {suggestion}. Proceeding as '{correct}'.")
            return transcribed_text.lower().replace(wrong, correct)
    return transcribed_text


# Keyword tables for intent detection (Korean + English). Order of checks in
# _detect_volume_action matters (unmute before mute, max before set, verbs before
# bare numbers). These run only after NB has already classified input as Volume.
_KW_UNMUTE = ['음소거 해제', '음소거해제', '뮤트 해제', '뮤트해제', '소리 켜', '소리켜',
              'unmute', 'activate sound']
_KW_MUTE = ['음소거', '뮤트', '소거', '조용', '입 다물', '입다물', '시끄', 'mute']
_KW_MAX = ['최대', '맥스', 'maximum', 'max volume', 'max the volume', 'full volume']
_KW_UP = ['키워', '올려', '높여', '크게', '업', 'increase', 'raise', 'louder', 'turn up',
          'volume up', 'sound up', 'higher']
_KW_DOWN = ['줄여', '낮춰', '낮추', '내려', '작게', '다운', 'decrease', 'lower', 'softer',
            'reduce', 'turn down', 'volume down', 'sound down']
_KW_CHECK = ['현재 볼륨', '볼륨 얼마', '볼륨얼마', '소리 얼마', 'check volume', 'current volume']

DEFAULT_STEP = 10


def _detect_volume_action(text):
    """
    Map an EN/KO volume utterance to (action, amount).
    action in {increase, decrease, set, mute, unmute, check, None}; amount may be None.
    """
    t = text.lower()
    num = re.search(r'(\d{1,3})', t)
    num_val = int(num.group(1)) if num else None

    if any(k in t for k in _KW_UNMUTE):
        return 'unmute', None
    if any(k in t for k in _KW_MUTE):
        return 'mute', None
    if any(k in t for k in _KW_MAX):
        return 'set', 100
    if any(k in t for k in _KW_UP):
        return 'increase', num_val
    if any(k in t for k in _KW_DOWN):
        return 'decrease', num_val
    # Bare "set to N" style: a number present with no up/down verb.
    if num_val is not None:
        return 'set', num_val
    if any(k in t for k in _KW_CHECK):
        return 'check', None
    return None, None


def handle_volume_command(user_input):
    """
    Interprets and handles volume-related commands (Korean or English).
    The volume control/card come from [VOLUME] in config; responses are localized
    by the [CHAR] language switch. NB has already routed the input here as Volume.

    Parameters:
        user_input (str): The user's command.
    Returns:
        str: A response describing the result of the volume action.
    """
    # Correct the input text based on common misinterpretations
    user_input = correct_transcription(user_input)

    lang = _CONFIG['CHAR'].get('language', 'en')
    vol_cfg = _CONFIG.get('VOLUME', {})
    volume_manager = RaspbianVolumeManager(
        control=vol_cfg.get('control', 'Master'),
        card=vol_cfg.get('card', ''),
    )

    current_volume = volume_manager.get_volume()

    def msg(ko, en):
        return ko if lang == 'ko' else en

    if current_volume is None:
        return msg("현재 볼륨을 가져올 수 없어. 사운드 설정을 확인해줘.",
                   "Unable to retrieve the current volume level. Please try again.")

    action, amount = _detect_volume_action(user_input)

    if action == 'increase':
        step = amount if amount else DEFAULT_STEP
        new_volume = min(current_volume + step, 100)
        volume_manager.set_volume(new_volume)
        current_volume = volume_manager.get_volume()
        return msg(f"볼륨을 {step}% 올렸어. 지금 {current_volume}%야.",
                   f"Volume increased by {step}%. Current volume is {current_volume}%.")

    if action == 'decrease':
        step = amount if amount else DEFAULT_STEP
        new_volume = max(current_volume - step, 0)
        volume_manager.set_volume(new_volume)
        current_volume = volume_manager.get_volume()
        return msg(f"볼륨을 {step}% 줄였어. 지금 {current_volume}%야.",
                   f"Volume decreased by {step}%. Current volume is {current_volume}%.")

    if action == 'set':
        if amount is None or not (0 <= amount <= 100):
            return msg("볼륨을 0에서 100 사이로 말해줘.",
                       "Please provide a valid volume between 0 and 100.")
        volume_manager.set_volume(amount)
        current_volume = volume_manager.get_volume()
        return msg(f"볼륨을 {amount}%로 맞췄어. 지금 {current_volume}%야.",
                   f"Volume set to {amount}%. Current volume is {current_volume}%.")

    if action == 'mute':
        volume_manager.set_volume(0)
        return msg("음소거했어.", "Volume has been muted. Current volume is 0%.")

    if action == 'unmute':
        default_volume = 50
        volume_manager.set_volume(default_volume)
        current_volume = volume_manager.get_volume()
        return msg(f"음소거를 풀었어. 지금 {current_volume}%야.",
                   f"Volume has been unmuted. Current volume is {current_volume}%.")

    if action == 'check':
        return msg(f"지금 볼륨은 {current_volume}%야.",
                   f"The current volume is {current_volume}%.")

    return msg("볼륨 명령을 못 알아들었어. 키워/줄여/음소거/볼륨 50처럼 말해줘.",
               "Volume control command not recognized. Please specify a valid action "
               "(e.g., increase, decrease, mute, unmute, set).")