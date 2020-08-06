#!/usr/bin/env python3

from datetime import datetime
import signal
import sys
import threading
from threading import Thread
from time import sleep

import RPi.GPIO as GPIO


INDICATOR_LED_GPIO = 6
KEY_BUTTON_GPIO = 21

# 
MIN_BLINK_PAUSE = 3  # seconds, a blink after at least that time starts a new blinking (phone blinks every 1.2s)
KEY_BUTTON_PRESS_AT_BLINK_COUNT = 4  # 3.6s

KEY_BUTTON_PRESS_DURATION = 5  # seconds, should be < MIN_BLINK_PAUSE + (KEY_BUTTON_PRESS_AT_BLINK_COUNT - 1) * 1.2

# TODO Fix pi time zone
HOUR_MIN = 7  # 8:00
HOUR_MAX = 18  # 19:00


def format_time(time):
    return time.strftime("%d-%m-%Y %H:%M:%S.%f")


def signal_handler(sig, frame):
    global key_button_loop_should_run
    global key_button_thread

    print("Cleaning up...")
    key_button_loop_should_run = False
    key_button_thread.join()
    GPIO.cleanup()
    sys.exit(0)


def key_button_loop():
    global key_button_loop_should_run
    global should_press_key_button

    while key_button_loop_should_run:
        if should_press_key_button:
            should_press_key_button = False
            GPIO.output(KEY_BUTTON_GPIO, GPIO.HIGH)
            print("button down")
            sleep(KEY_BUTTON_PRESS_DURATION)
            GPIO.output(KEY_BUTTON_GPIO, GPIO.LOW)
            print("button up")

        sleep(0.1)


def led_on_handler(channel):
    global should_press_key_button
    global last_led_on_time
    global blink_count

    now = datetime.now()

    sleep(0.05)
    if GPIO.input(INDICATOR_LED_GPIO) != GPIO.LOW:
        print("spurious led event")
        return

    if last_led_on_time is None or (now - last_led_on_time).seconds >= MIN_BLINK_PAUSE:
        last_led_on_time = now
        blink_count = 1
        print(f"new blinking sequence @ {format_time(now)}")
    else:
        last_led_on_time = now
        blink_count += 1
        print(f"blink #{blink_count} @ {format_time(now)}")

    if blink_count == KEY_BUTTON_PRESS_AT_BLINK_COUNT and HOUR_MIN <= now.hour <= HOUR_MAX:
        should_press_key_button = True
        print("setting should_press_key_button = True")


if __name__ == "__main__":
    global key_button_thread
    global key_button_loop_should_run
    global should_press_key_button
    global last_led_on_time
    global blink_count

    key_button_thread = None
    key_button_loop_should_run = True
    should_press_key_button = False
    last_led_on_time = None
    blink_count = 0

    GPIO.setwarnings(False)
    GPIO.setmode(GPIO.BCM)
    GPIO.setup(INDICATOR_LED_GPIO, GPIO.IN, pull_up_down=GPIO.PUD_DOWN)
    GPIO.setup(KEY_BUTTON_GPIO, GPIO.OUT)

    key_button_thread = Thread(target=key_button_loop)
    key_button_thread.start()

    # inverted input, FALLING means LED turned on
    GPIO.add_event_detect(INDICATOR_LED_GPIO, GPIO.FALLING, callback=led_on_handler)

    signal.signal(signal.SIGINT, signal_handler)
    signal.pause()
