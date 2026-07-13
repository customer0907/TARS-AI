import sys
import time
import threading
import queue
import re
from datetime import datetime

# Queue for handling message processing
message_queue = queue.Queue()
output_lock = threading.Lock()  # 🔹 Single lock for ALL stdout operations

_TIMESTAMP_PREFIX = re.compile(r'^\[\d{2}:\d{2}:\d{2}\] ')


def format_console_line(message: str) -> str:
    """Prefix USER:/TARS:/INFO: lines with [HH:MM:SS] for the live console."""
    text = message.strip()
    if not text or _TIMESTAMP_PREFIX.match(text):
        return text
    for prefix in ("USER:", "TARS:", "INFO:"):
        if text.startswith(prefix):
            ts = datetime.now().strftime("%H:%M:%S")
            return f"[{ts}] {text}"
    return text

def process_message_queue():
    """ Continuously process the message queue in order. """
    live_key = None
    live_width = 0
    live_timestamp = None

    while True:
        item = message_queue.get()

        if item is None:  # Stop signal
            if live_key is not None:
                with output_lock:
                    sys.stdout.write("\n")
                    sys.stdout.flush()
            break

        if isinstance(item, tuple) and len(item) == 4 and item[0] == "__live_update__":
            _, key, message, final = item
            with output_lock:
                if live_key is not None and live_key != key:
                    sys.stdout.write("\n")
                    live_width = 0
                    live_timestamp = None
                display = format_console_line(message)
                timestamp_match = _TIMESTAMP_PREFIX.match(display)
                if live_key == key and live_timestamp and timestamp_match:
                    display = live_timestamp + display[timestamp_match.end():]
                elif timestamp_match:
                    live_timestamp = timestamp_match.group(0)
                padding = " " * max(0, live_width - len(display))
                sys.stdout.write(f"\r{display}{padding}")
                if final:
                    sys.stdout.write("\n")
                    live_key = None
                    live_width = 0
                    live_timestamp = None
                else:
                    live_key = key
                    live_width = len(display)
                sys.stdout.flush()
            message_queue.task_done()
            continue

        if live_key is not None:
            with output_lock:
                sys.stdout.write("\n")
                sys.stdout.flush()
            live_key = None
            live_width = 0
            live_timestamp = None

        # Handle cases where stream_text is missing (default to False)
        if isinstance(item, tuple) and len(item) == 2:
            message, stream_text = item
        else:
            message = item
            stream_text = False  # Default if not provided

        if stream_text:
            # 🔹 Run text streaming in a **separate thread** to avoid blocking
            threading.Thread(target=stream_text_blocking, args=(message,), daemon=True).start()
        else:
            with output_lock:  # 🔹 Lock only while printing
                print(message, flush=True)

        message_queue.task_done()

def stream_text_blocking(text, delay=0.03):
    """ Streams text character-by-character in a non-blocking manner. """
    
    def _stream():
        with output_lock:  # 🔹 Ensures no other process writes during streaming
            sys.stdout.flush()
            time.sleep(0.4)  # 🔹 Small delay to ensure terminal processes it this will not block the main program

            for char in text:
                sys.stdout.write(char)
                sys.stdout.flush()
                time.sleep(delay)  # 🔹 Slow typing effect

            sys.stdout.write("\n")  # 🔹 Ensure newline at the end
            sys.stdout.flush()

    # Run the streaming in a separate thread to prevent blocking
    threading.Thread(target=_stream, daemon=True).start()

def queue_message(message, stream=False):
    """
    Adds a message to the queue for ordered processing.

    Parameters:
    - message (str): The message content.
    - stream (bool, optional): If True, outputs character-by-character; otherwise, prints instantly.
                               Defaults to False if not provided.
    """
    if message and message.strip():
        message_queue.put((format_console_line(message.strip()), stream))  # 🔹 No lock needed here


def queue_live_update(key, message, final=False):
    """Replace one console line as a partial transcript grows."""
    if message and message.strip():
        message_queue.put(("__live_update__", key, message.strip(), final))

def stop_message_processing():
    """ Stops the message processing thread safely. """
    message_queue.put(None)  # Stop signal
    message_thread.join()

# Start the message processing thread
message_thread = threading.Thread(target=process_message_queue, daemon=True)
message_thread.start()
