import asyncio
import base64
import io
import os
import numpy as np
import logging
from openai_realtime import RealtimeClient, RealtimeUtils
from typing import Optional, Callable
import discord
from discord.ext import commands
from discord.ext import voice_recv
import time
from discord import PCMAudio, SpeakingState
import pyaudio

# Set up logging
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class MySink(voice_recv.AudioSink):
    def __init__(self, input_audio_queue: asyncio.Queue, loop: asyncio.AbstractEventLoop):
        super().__init__()
        self.input_audio_queue = input_audio_queue
        self.loop = loop
        self.last_audio_time = time.time()
        self.total_buffer = []

    def wants_opus(self) -> bool:
        return False

    def write(self, user, data):
        # print()
        # Convert PCM data to numpy array and resample from 48kHz to 24kHz
        # Play the audio data to verify it's correct

        # Ensure the audio data is in the correct format (int16)
        audio_array = np.frombuffer(data.pcm, dtype=np.int16)

        # Resample from 48kHz to 24kHz
        resampled_audio = np.zeros(len(audio_array) // 2, dtype=np.int16)
        resampled_audio[0::2] = audio_array[0::4]
        resampled_audio[1::2] = audio_array[2::4]

        # Optional: Play the audio data to verify it's correct (uncomment if needed)
        # sd.play(resampled_audio, samplerate=24000)
        # sd.wait()
        # Put the audio data into the input queue using run_coroutine_threadsafe
        asyncio.run_coroutine_threadsafe(self.input_audio_queue.put(resampled_audio), self.loop)

    def cleanup(self):
        pass


class DiscordRealtimeAssistant(commands.Cog):
    
    def __init__(self, bot, api_key: str, instructions: str, channel_id: int, debug: bool = False):
        self.bot = bot
        self.api_key = api_key
        self.instructions = instructions
        self.channel_id = channel_id  # New: Store the channel ID to join
        self.debug = debug
        self.client: Optional[RealtimeClient] = None
        self.voice_client: Optional[discord.VoiceClient] = None
        self.audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.input_audio_queue: asyncio.Queue[np.ndarray] = asyncio.Queue()
        self.stop_event = asyncio.Event()
        self.sample_rate = 24000  # Discord's default sample rate
        self.chunk_duration = 0.02  # 20ms chunks
        self.chunk_size = int(self.sample_rate * self.chunk_duration)
        self.last_audio_time = time.time()
        logger.info("DiscordRealtimeAssistant initialized")
        self.pyaudio = pyaudio.PyAudio()
        self.output_stream = None
        self.audio_source = None

    async def initialize(self):
        self.client = RealtimeClient(api_key=self.api_key, debug=self.debug)
        self.client.update_session(
            instructions=self.instructions,
            output_audio_format='pcm16',
            input_audio_format='pcm16',
            turn_detection={
                'type': 'server_vad',
                'threshold': 0.5,
                'prefix_padding_ms': 300,
                'silence_duration_ms': 600,
            }
        )
        self._setup_event_handlers()

    def _setup_event_handlers(self):
        @self.client.realtime.on('server.response.audio.delta')
        def handle_audio_delta(event):
            audio_data = np.frombuffer(base64.b64decode(event['delta']), dtype=np.int16)
            asyncio.create_task(self.audio_queue.put(audio_data))

        @self.client.realtime.on('server.response.text.delta')
        def handle_text_delta(event):
            print(event['delta'], end='', flush=True)

        @self.client.realtime.on('server.input_audio_buffer.speech_started')
        def handle_speech_started(event):
            asyncio.create_task(self.clear_queue(self.audio_queue))
            if self.audio_source:
                self.audio_source.clear_buffer()
            print("\nUser is speaking...")

        @self.client.realtime.on('server.input_audio_buffer.speech_stopped')
        def handle_speech_stopped(event):
            print("\nUser finished speaking.")
            # self.client.create_response()

        # @self.client.on('realtime.event')
        # def handle_server_event(event):
        #     if event['event']['type'] != 'response.audio.delta' and event['event']['type'] != 'input_audio_buffer.append':
        #         print("Server event", event)

    async def clear_queue(self, queue: asyncio.Queue):
        while not queue.empty():
            try:
                queue.get_nowait()
                queue.task_done()
            except asyncio.QueueEmpty:
                break

    class PyAudioSource(discord.AudioSource):
        def __init__(self, audio_queue: asyncio.Queue, sample_rate: int):
            self.audio_queue = audio_queue
            self.sample_rate = sample_rate
            self.buffer = np.array([], dtype=np.int16)
            self.last_data_time = time.time()
            self.packet_size = 960 
        def read(self) -> bytes:
      
            try:
                new_data = self.audio_queue.get_nowait()
                current_time = time.time()
                ms_since_last_data = (current_time - self.last_data_time) * 1000
                print(f"Time since last new data: {ms_since_last_data:.2f} ms")
                self.last_data_time = current_time
                # Upsample from 24kHz to 48kHz
                new_data = np.repeat(new_data, 2) 
                
                self.buffer = np.append(self.buffer, new_data)
                
                
            except asyncio.QueueEmpty:
                pass
            # print(len(self.buffer))

            if len(self.buffer) >= self.packet_size:
                chunk = self.buffer[:self.packet_size]
                self.buffer = self.buffer[self.packet_size:]
                # Convert mono to stereo
                stereo_chunk = np.column_stack((chunk, chunk))
                # time.sleep(0.04)
                return stereo_chunk.tobytes()
            else:
                # print("not enough data")
                return bytes(self.packet_size* 4)  # Return silence if not enough data

        def cleanup(self):
            pass

        def clear_buffer(self):
            self.buffer = np.array([], dtype=np.int16)
            logger.info("Audio output buffer cleared")

    async def audio_playback_worker(self):
        self.audio_source = self.PyAudioSource(self.audio_queue, self.sample_rate)
        
        if self.voice_client and self.voice_client.is_connected():
            self.voice_client.play(self.audio_source, signal_type='voice', after=lambda e: print(f'Player error: {e}') if e else None)
            logger.info("Started audio playback")

        while not self.stop_event.is_set():
            await asyncio.sleep(1)  # Sleep to prevent busy-waiting

        if self.voice_client and self.voice_client.is_playing():
            self.voice_client.stop()
            logger.info("Stopped audio playback")

    async def audio_input_worker(self):
        while not self.stop_event.is_set():
            try:
                current_time = time.time()
                time_since_last_audio = current_time - self.last_audio_time

                try:
                    # Try to get data from the queue, but don't wait
                    data = self.input_audio_queue.get_nowait()
                    self.client.append_input_audio(data.flatten())
                    self.input_audio_queue.task_done()
                    self.last_audio_time = current_time
                except asyncio.QueueEmpty:
                    # If queue is empty, wait for a short time before next iteration
                    await asyncio.sleep(0.001)  # 1ms sleep

            except asyncio.CancelledError:
                break

    @commands.command()
    async def join(self, ctx):
        logger.info(f"Join command received from {ctx.author}")
        await self.join_voice_channel(ctx.message)

    @commands.Cog.listener()
    async def on_message(self, message):
        if self.bot.user.mentioned_in(message) and "join" in message.content.lower():
            logger.info(f"Bot mentioned with 'join' by {message.author}")
            await self.join_voice_channel(message)

    async def join_voice_channel(self, message):
        if message.author.voice:
            channel = message.author.voice.channel
            if self.voice_client and self.voice_client.is_connected():
                await self.voice_client.move_to(channel)
            else:
                
                self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
            logger.info(f"Joined voice channel: {channel.name}")
            await message.channel.send(f"Joined the voice channel: {channel.name}")
            await self.start_listening(message.channel)
        else:
            logger.warning(f"Join attempt failed: {message.author} not in a voice channel")
            await message.channel.send("You need to be in a voice channel for me to join.")

    @commands.command()
    async def leave(self, ctx):
        logger.info(f"Leave command received from {ctx.author}")
        if self.voice_client:
            await self.voice_client.disconnect()
            self.voice_client = None
            self.stop_event.set()
            logger.info("Disconnected from voice channel")
            await ctx.send("Disconnected from voice channel.")
        else:
            logger.warning("Leave command received but not connected to any voice channel")
            await ctx.send("I'm not connected to a voice channel.")

    async def start_listening(self, text_channel):
        logger.info("Starting listening process")
        await self.initialize()
        await self.client.connect()
        logger.info("Connected to RealtimeClient")

        await self.client.wait_for_session_created()
        logger.info("Session created")

        playback_task = asyncio.create_task(self.audio_playback_worker())
        input_task = asyncio.create_task(self.audio_input_worker())

        # Pass the input_audio_queue and the event loop to MySink
        self.voice_client.listen(MySink(self.input_audio_queue, asyncio.get_running_loop()))
        self.last_audio_time = time.time()

        await text_channel.send("Listening to the voice channel...")
        logger.info("Started listening to the voice channel")

        # ... rest of the method ...

    def discord_audio_callback(self, sink, data: bytes):
        # logger.debug("Received audio data from Discord")
        audio_data = np.frombuffer(data, dtype=np.int16)
        asyncio.create_task(self.input_audio_queue.put(audio_data))

    @commands.Cog.listener()
    async def on_ready(self): 
        logger.info("DiscordRealtimeAssistant is ready. Attempting to join voice channel.")
        await self.auto_join_voice_channel()

    async def auto_join_voice_channel(self):
        channel = self.bot.get_channel(self.channel_id)
        if isinstance(channel, discord.VoiceChannel):
            try:
                self.voice_client = await channel.connect(cls=voice_recv.VoiceRecvClient)
                logger.info(f"Automatically joined voice channel: {channel.name}")
                await self.start_listening(channel)
            except Exception as e:
                logger.error(f"Failed to join voice channel: {e}")
        else:
            logger.error(f"Channel with ID {self.channel_id} is not a voice channel or doesn't exist.")

class DiscordBot(commands.Bot):
    def __init__(self):
        intents = discord.Intents.default()
        intents.message_content = True
        intents.voice_states = True
        super().__init__(command_prefix='!', intents=intents)
        logger.info("DiscordBot initialized")

    async def on_ready(self):
        logger.info(f'Logged in as {self.user} (ID: {self.user.id})')
        logger.info('------')

async def main():
    bot = DiscordBot()
    assistant = DiscordRealtimeAssistant(
        bot, 
        api_key=os.getenv("OPENAI_API_KEY"),
        instructions="You are a helpful assistant.",
        channel_id=1266849047314960399,  # New: Pass the channel ID
        debug=False
    )
    await bot.add_cog(assistant)
    logger.info("DiscordRealtimeAssistant added as a cog to the bot")
    
    async with bot:
        await bot.start(os.getenv("DISCORD_BOT_TOKEN"))

if __name__ == "__main__":
    logger.info("Starting the Discord bot")
    asyncio.run(main())