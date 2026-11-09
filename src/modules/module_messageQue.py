import os
import sys
import time
import threading
import queue
import re
import shutil
import unicodedata
from datetime import datetime

# Queue for handling message processing
message_queue = queue.Queue()
output_lock = threading.Lock()  # 🔹 Single lock for ALL stdout operations

_TIMESTAMP_PREFIX = re.compile(r'^\[\d{2}:\d{2}:\d{2}\] ')

# Console lines are also kept on disk so conversations can be reviewed later.
CONSOLE_LOG = os.path.join(os.path.dirname(__file__), "..", "logs", "console.log")
CONSOLE_LOG_MAX_BYTES = 20 * 1024 * 1024
_log_lock = threading.Lock()


def _append_console_log(text: str) -> None:
    line = f"[{datetime.now():%Y-%m-%d %H:%M:%S}] {_TIMESTAMP_PREFIX.sub('', text)}\n"
    try:
        with _log_lock:
            if os.path.exists(CONSOLE_LOG) and os.path.getsize(CONSOLE_LOG) > CONSOLE_LOG_MAX_BYTES:
                os.replace(CONSOLE_LOG, CONSOLE_LOG + ".1")
            with open(CONSOLE_LOG, "a", encoding="utf-8") as handle:
                handle.write(line)
    except OSError:
        pass


def _char_width(char: str) -> int:
    """Hangul/CJK glyphs occupy two terminal columns."""
    return 2 if unicodedata.east_asian_width(char) in ("W", "F") else 1


def display_width(text: str) -> int:
    return sum(_char_width(char) for char in text)


def truncate_to_width(text: str, limit: int):
    """Cut text to fit one terminal row; \\r only rewinds the last wrapped row."""
    kept = []
    width = 0
    for char in text:
        char_width = _char_width(char)
        if width + char_width > limit:
            break
        kept.append(char)
        width += char_width
    return "".join(kept), width


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
    live_display = ""
    # Cursor tricks (\r) only work on a real terminal; when stdout is piped or
    # redirected they would leave one stale line per partial update.
    live_capable = sys.stdout.isatty()

    def write_live(display):
        """Redraw the in-progress line, padding over whatever was longer before."""
        nonlocal live_width
        columns = shutil.get_terminal_size(fallback=(80, 24)).columns
        # Stay one column short of the edge so the row never wraps; a wrapped row
        # cannot be rewound by \r and would leave its overflow on screen forever.
        clipped, width = truncate_to_width(display, max(1, columns - 1))
        padding = " " * max(0, live_width - width)
        sys.stdout.write(f"\r{clipped}{padding}")
        live_width = width

    def end_live_line():
        nonlocal live_key, live_width, live_timestamp, live_display
        sys.stdout.write("\n")
        live_key = None
        live_width = 0
        live_timestamp = None
        live_display = ""

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
                    end_live_line()
                display = format_console_line(message)
                timestamp_match = _TIMESTAMP_PREFIX.match(display)
                if live_key == key and live_timestamp and timestamp_match:
                    display = live_timestamp + display[timestamp_match.end():]
                elif timestamp_match:
                    live_timestamp = timestamp_match.group(0)

                if not live_capable:
                    # No in-place updates available; print the finished line only.
                    if final:
                        print(display, flush=True)
                        live_key = None
                        live_timestamp = None
                        live_display = ""
                    message_queue.task_done()
                    continue

                if final:
                    # Completed turns print in full and may wrap freely.
                    sys.stdout.write("\r" + " " * live_width + "\r")
                    print(display, flush=True)
                    live_key = None
                    live_width = 0
                    live_timestamp = None
                    live_display = ""
                else:
                    write_live(display)
                    live_key = key
                    live_display = display
                sys.stdout.flush()
            message_queue.task_done()
            continue

        # Handle cases where stream_text is missing (default to False)
        if isinstance(item, tuple) and len(item) == 2:
            message, stream_text = item
        else:
            message = item
            stream_text = False  # Default if not provided

        if stream_text:
            # Character-by-character output owns the cursor, so close the live line first.
            if live_key is not None:
                with output_lock:
                    end_live_line()
                    sys.stdout.flush()
            # 🔹 Run text streaming in a **separate thread** to avoid blocking
            threading.Thread(target=stream_text_blocking, args=(message,), daemon=True).start()
        else:
            with output_lock:  # 🔹 Lock only while printing
                if live_key is not None:
                    # Print above the in-progress line, then redraw it underneath so the
                    # partial transcript keeps growing in place instead of restarting.
                    sys.stdout.write("\r" + " " * live_width + "\r")
                    print(message, flush=True)
                    live_width = 0
                    write_live(live_display)
                    sys.stdout.flush()
                else:
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
        _append_console_log(message.strip())
        message_queue.put((format_console_line(message.strip()), stream))  # 🔹 No lock needed here


def queue_live_update(key, message, final=False):
    """Replace one console line as a partial transcript grows."""
    if message and message.strip():
        if final:
            _append_console_log(message.strip())
        message_queue.put(("__live_update__", key, message.strip(), final))

def stop_message_processing():
    """ Stops the message processing thread safely. """
    message_queue.put(None)  # Stop signal
    message_thread.join()

# Start the message processing thread
message_thread = threading.Thread(target=process_message_queue, daemon=True)
message_thread.start()
