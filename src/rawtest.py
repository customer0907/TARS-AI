import sounddevice as sd

try:
    stream = sd.RawInputStream(
        device="hw:3,0",
        samplerate=44100,
        channels=1,
        dtype='int16',
        blocksize=4000
    )
    stream.start()
    print("Success!")
    stream.stop()
    stream.close()
except Exception as e:
    print(f"Failed: {e}")
