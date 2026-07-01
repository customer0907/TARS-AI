"""
module_discord.py

Discord Integration Module for GPTARS Application.

This module provides integration with Discord for real-time interaction. 
It allows the bot to respond to messages and mentions in a specified Discord channel.
"""

# === Standard Libraries ===
import asyncio
import discord

# === Custom Modules ===
from modules.module_config import load_config
from modules.module_messageQue import queue_message
from modules.module_diag_log import diag_log

# === Constants and Globals ===
CONFIG = load_config()
process_discord_message_callback = None

# === Initialization ===
intents = discord.Intents.default()  # Initialize Discord client with appropriate intents
intents.message_content = True
intents.voice_states = True  # Enable voice state intents if necessary
client = discord.Client(intents=intents)

def start_discord_bot(callback):
    """
    Starts the Discord bot with a message processing callback.

    Parameters:
    - callback (function): A function that processes user messages and returns a response.
    """
    global process_discord_message_callback
    process_discord_message_callback = callback

    bot_token = CONFIG['DISCORD']['TOKEN']
    client.run(bot_token)
    
async def replace_mentions_with_usernames(content):
    """
    Replace all mentions in the content with their corresponding usernames.

    Args:
        content: The message content containing mentions.

    Returns:
        The content with mentions replaced by usernames.
    """
    words = content.split()
    for i, word in enumerate(words):
        if word.startswith("<@") and word.endswith(">"):
            username = await mention_to_username(word)
            if username:
                words[i] = f"@{username}"  # Replace mention with username
    return " ".join(words)

async def mention_to_username(mention):
    """
    Convert a Discord mention (e.g., <@200301865894805504>) into the username.

    Args:
        mention: The mention string (e.g., <@200301865894805504>).

    Returns:
        The username as a string or None if the user is not found.
    """
    if mention.startswith("<@") and mention.endswith(">"):
        # Extract the user ID from the mention
        user_id = mention.strip("<@!>")  # Handles both <@ID> and <@!ID>
        try:
            user_id = int(user_id)  # Convert the extracted ID to an integer
        except ValueError:
            return None  # Invalid mention format, return None

        try:
            # Use the client to fetch the user by ID
            user = await client.fetch_user(user_id)
            if user:
                return user.name  # Return the user's username
        except discord.NotFound:
            queue_message(f"ERROR: User with ID {user_id} not found.")
        except discord.Forbidden:
            queue_message("ERROR: Insufficient permissions to fetch user details.")
        except discord.HTTPException as e:
            queue_message(f"ERROR: HTTP error occurred: {e}")

    return None  # If the mention format or user is invalid

@client.event
async def on_ready():
    queue_message(f"INFO: Logged in as {client.user}")


def _bot_is_mentioned(message):
    if not client.user:
        return False
    return any(mention.id == client.user.id for mention in message.mentions)


@client.event
async def on_message(message):
    """
    Triggered when a message is sent in a channel the bot can access.
    """
    global process_discord_message_callback

    if message.author.bot:
        return

    diag_log(
        f"discord rx ch={message.channel.id} author={message.author} "
        f"mentions={[m.id for m in message.mentions]} content={message.content[:120]!r}"
    )

    if not _bot_is_mentioned(message):
        return

    user_message = message.content.strip()
    queue_message(f"DISCORD: recv {message.author.name}: {user_message[:120]}")

    if not process_discord_message_callback:
        queue_message("ERROR: Discord callback not defined.")
        await message.channel.send("Error: No processing logic available.")
        return

    bot_id = client.user.id
    try:
        loop = asyncio.get_running_loop()
        reply = await loop.run_in_executor(
            None,
            lambda: process_discord_message_callback(user_message, bot_id),
        )
        queue_message(f"DISCORD: reply -> {message.author.name}: {reply[:120]}")
        await message.channel.send(f"{message.author.mention} {reply}")
    except Exception as e:
        queue_message(f"ERROR: Discord reply failed: {e}")
        diag_log(f"discord reply error: {e}")
        await message.channel.send(f"{message.author.mention} (응답 생성 중 오류가 났어.)")
