import sys
import uasyncio as asyncio
import aioble
import bluetooth
import struct
import math
import time
from plasma import WS2812
from machine import Pin

# ==========================================
# CONFIGURATION
# ==========================================

# Hardware
NUM_LEDS = 144       
LED_DATA_PIN = 15
BUTTON_A_PIN = 12   
led_strip = WS2812(NUM_LEDS, 0, 0, LED_DATA_PIN)

# Heart Rate Zones
HR_REST = 40    # Blue/Cool
HR_MAX = 175    # Red/Hot

# Pulse Settings - SAFE FOR 5A PSU (RPi 45W/27W Supply)
MIN_BRIGHTNESS = 0.2
MAX_BRIGHTNESS = 0.6  

# Modes
MODE_PULSE = 0
MODE_STEADY = 1
MODE_VU = 2
MODE_WAVE = 3
MODE_NAMES = ["PULSE", "STEADY", "VU METER", "WAVE"]

# Bluetooth Constants
_HR_SERVICE_UUID = bluetooth.UUID(0x180d)
_HR_CHAR_UUID = bluetooth.UUID(0x2a37)

# ==========================================
# FILE I/O (MEMORY) HELPERS
# ==========================================

def load_saved_mode():
    """Reads the last saved mode from flash memory on boot."""
    try:
        with open("saved_mode.txt", "r") as f:
            mode = int(f.read().strip())
            # Ensure the saved number is valid (0 to 3)
            if 0 <= mode < len(MODE_NAMES):
                print(f"Loaded saved mode: {MODE_NAMES[mode]}")
                return mode
    except Exception:
        # If the file doesn't exist yet or is corrupted, default to PULSE
        print("No saved mode found, defaulting to PULSE.")
        pass
    return MODE_PULSE

def save_current_mode(mode_index):
    """Writes the current mode to flash memory."""
    try:
        with open("saved_mode.txt", "w") as f:
            f.write(str(mode_index))
    except Exception as e:
        print(f"Failed to save mode: {e}")

# Global Variables
current_bpm = 0
connected = False
current_mode = load_saved_mode() # Initialize using the loaded memory!

# ==========================================
# COLOR & MATH HELPERS
# ==========================================

def hsv_to_rgb(h, s, v):
    if s == 0.0: return int(v*255), int(v*255), int(v*255)
    i = int(h*6.)
    f = (h*6.)-i
    p, q, t = int(255*(v*(1.-s))), int(255*(v*(1.-s*f))), int(255*(v*(1.-s*(1.-f))))
    v = int(255*v)
    i %= 6
    if i == 0: return v, t, p
    if i == 1: return q, v, p
    if i == 2: return p, v, t
    if i == 3: return p, q, v
    if i == 4: return t, p, v
    if i == 5: return v, p, q

def get_color_for_progress(progress):
    progress = max(0.0, min(1.0, progress))
    hue = 0.6 - (progress * 0.6)
    
    saturation = 1.0
    if progress > 0.9:
        saturation = 1.0 - ((progress - 0.9) * 5)
        
    return hsv_to_rgb(hue, saturation, 1.0)

def get_target_color(bpm):
    if bpm < HR_REST:
        return 0, 0, 255
    progress = (bpm - HR_REST) / (HR_MAX - HR_REST)
    return get_color_for_progress(progress)

# ==========================================
# ASYNC TASKS
# ==========================================

async def handle_button():
    global current_mode
    btn = Pin(BUTTON_A_PIN, Pin.IN, Pin.PULL_UP)
    last_state = 1
    
    while True:
        current_state = btn.value()
        if last_state == 1 and current_state == 0:
            # Change the mode
            current_mode = (current_mode + 1) % len(MODE_NAMES)
            print(f"Mode Switched to: {MODE_NAMES[current_mode]}")
            
            # Save the new mode to the board's memory
            save_current_mode(current_mode)
            
            await asyncio.sleep_ms(300) 
        last_state = current_state
        await asyncio.sleep_ms(50)

async def animate_leds():
    global current_bpm
    led_strip.start()
    phase = 0.0
    
    peak_led = 0.0
    peak_hang_timer = 0
    
    while True:
        # --- SCENARIO 1: DISCONNECTED (ALARM) ---
        if not connected:
            safe_white = int(255 * 0.5)
            for i in range(NUM_LEDS):
                led_strip.set_rgb(i, safe_white, safe_white, safe_white)
            await asyncio.sleep_ms(100)
            for i in range(NUM_LEDS):
                led_strip.set_rgb(i, 0, 0, 0)
            await asyncio.sleep_ms(400)
            continue

        # --- SCENARIO 2: CONNECTED (RENDER) ---
        if current_bpm > 0:
            bps = current_bpm / 60.0
            phase += (bps * 2 * math.pi) * 0.02
            if phase > 2 * math.pi: phase -= 2 * math.pi
            
            sine_val = math.sin(phase - (math.pi/2))
            norm_sine = (sine_val + 1) / 2
            pulse_brightness = MIN_BRIGHTNESS + (norm_sine * (MAX_BRIGHTNESS - MIN_BRIGHTNESS))
        else:
            pulse_brightness = MIN_BRIGHTNESS

        if current_mode in [MODE_PULSE, MODE_STEADY]:
            brightness = pulse_brightness if current_mode == MODE_PULSE else MAX_BRIGHTNESS
            r, g, b = get_target_color(current_bpm)
            
            final_r = int(r * brightness)
            final_g = int(g * brightness)
            final_b = int(b * brightness)
            
            for i in range(NUM_LEDS):
                led_strip.set_rgb(i, final_r, final_g, final_b)
                
        elif current_mode == MODE_VU:
            base_leds = NUM_LEDS // 3
            
            if current_bpm < HR_REST:
                target_leds = base_leds 
            else:
                progress = min(1.0, (current_bpm - HR_REST) / (HR_MAX - HR_REST))
                target_leds = base_leds + int(progress * (NUM_LEDS - base_leds))
                
            if target_leds >= int(peak_led):
                peak_led = target_leds
                peak_hang_timer = 50 
            else:
                if peak_hang_timer > 0:
                    peak_hang_timer -= 1
                else:
                    peak_led -= 0.5 
                    if peak_led < target_leds:
                        peak_led = target_leds

            for i in range(NUM_LEDS):
                if i < target_leds:
                    led_prog = i / max(1, NUM_LEDS - 1)
                    r, g, b = get_color_for_progress(led_prog)
                    led_strip.set_rgb(i, int(r * pulse_brightness), int(g * pulse_brightness), int(b * pulse_brightness))
                    
                elif i == int(peak_led) and int(peak_led) > 0 and i < NUM_LEDS:
                    white_val = int(255 * MAX_BRIGHTNESS)
                    led_strip.set_rgb(i, white_val, white_val, white_val)
                    
                else:
                    led_strip.set_rgb(i, 0, 0, 0)
                    
        elif current_mode == MODE_WAVE:
            r, g, b = get_target_color(current_bpm)
            center = NUM_LEDS / 2
            
            for i in range(NUM_LEDS):
                dist_norm = abs(i - center) / center
                wave_val = math.sin(phase - (dist_norm * math.pi * 4))
                norm_wave = (wave_val + 1) / 2
                brightness = MIN_BRIGHTNESS + (norm_wave * (MAX_BRIGHTNESS - MIN_BRIGHTNESS))
                led_strip.set_rgb(i, int(r * brightness), int(g * brightness), int(b * brightness))

        await asyncio.sleep_ms(20)

async def handle_bluetooth():
    global current_bpm, connected
    await asyncio.sleep_ms(1000)

    while True:
        try:
            connected = False
            current_bpm = 0
            device = None
            
            print("Scanning for Polar...")
            async with aioble.scan(5000, interval_us=30000, window_us=30000, active=True) as scanner:
                async for result in scanner:
                    if _HR_SERVICE_UUID in result.services():
                        device = result.device
                        break
            
            if not device:
                continue

            print(f"Connecting to {device}...")
            connection = await device.connect(timeout_ms=5000)
            
            async with connection:
                print("Connected!")
                connected = True
                
                service = await connection.service(_HR_SERVICE_UUID)
                char = await service.characteristic(_HR_CHAR_UUID)
                await char.subscribe(notify=True)
                
                while True:
                    data = await char.notified()
                    flags = data[0]
                    if flags & 0x01:
                        bpm = struct.unpack_from("<H", data, 1)[0]
                    else:
                        bpm = struct.unpack_from("<B", data, 1)[0]
                    
                    current_bpm = bpm
                    print(f"HR: {bpm}")
        
        except Exception as e:
            print(f"Bluetooth Error: {e}")
            connected = False
            await asyncio.sleep_ms(1000)

# ==========================================
# MAIN EXECUTION
# ==========================================

async def main():
    t1 = asyncio.create_task(handle_bluetooth())
    t2 = asyncio.create_task(animate_leds())
    t3 = asyncio.create_task(handle_button())
    
    await asyncio.gather(t1, t2, t3)

try:
    asyncio.run(main())
except KeyboardInterrupt:
    for i in range(NUM_LEDS):
        led_strip.set_rgb(i, 0, 0, 0)
    print("Program Stopped")
