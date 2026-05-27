from pydub.generators import Sine
from pydub.playback import play

while True:
	tone = Sine(440).to_audio_segment(duration=3000)
	play(tone)
